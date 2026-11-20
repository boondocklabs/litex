#
# This file is part of LiteX.
#
# Copyright (c) 2018-2026 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2026 edecoux <emilien.decoux@edu.univ-fcomte.fr>
# SPDX-License-Identifier: BSD-2-Clause

from math import ceil

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.build.xilinx.vivado import XilinxVivadoToolchain
from litex.soc.interconnect import stream
from litex.soc.cores.clock.common import *
from litex.soc.cores.clock.xilinx_common import *

# Xilinx / 7-Series PLL ----------------------------------------------------------------------------

class S7PLL(XilinxClocking):
    nclkouts_max = 6
    clkin_freq_range = (19e6, 800e6)

    def __init__(self, speedgrade=-1, name=None):
        self.logger = logging.getLogger("S7PLL")
        self.logger.info("Creating S7PLL, {}.".format(colorer("speedgrade {}".format(speedgrade))))
        XilinxClocking.__init__(self)
        self.name = name
        self.divclk_divide_range = (1, 56+1)
        self.vco_freq_range = {
            -1: (800e6, 1600e6),
            -2: (800e6, 1866e6),
            -3: (800e6, 2133e6),
        }[speedgrade]

    def do_finalize(self):
        XilinxClocking.do_finalize(self)
        config = self.compute_config()
        pll_fb = Signal()
        self.params.update(
            # Global.
            p_STARTUP_WAIT = "FALSE",
            i_RST          = self.reset,
            i_PWRDWN       = self.power_down,
            o_LOCKED       = self.locked,

            # VCO.
            p_REF_JITTER1   = 0.01,
            p_CLKIN1_PERIOD = 1e9/self.clkin_freq,
            p_CLKFBOUT_MULT = config["clkfbout_mult"],
            p_DIVCLK_DIVIDE = config["divclk_divide"],
            i_CLKIN1        = self.clkin,
            i_CLKFBIN       = pll_fb,
            o_CLKFBOUT      = pll_fb,
        )
        for n, clkout in sorted(self.clkouts.items()):
            self.params["p_CLKOUT{}_DIVIDE".format(n)] = config["clkout{}_divide".format(n)]
            self.params["p_CLKOUT{}_PHASE".format(n)] = config["clkout{}_phase".format(n)]
            self.params["o_CLKOUT{}".format(n)] = clkout.clk
        self.specials += Instance("PLLE2_ADV", name=self.name or "", **self.params)

# Xilinx / 7-Series MMCM ---------------------------------------------------------------------------

class S7MMCM(XilinxClocking):
    nclkouts_max         = 7

    def __init__(self, speedgrade=-1, fractional=True, name=None):
        self.logger = logging.getLogger("S7MMCM")
        self.logger.info("Creating S7MMCM, {}.".format(colorer("speedgrade {}".format(speedgrade))))
        XilinxClocking.__init__(self)
        self.name = name
        self.divclk_divide_range = (1, 106+1)
        self.clkin_freq_range = {
            -1: (10e6,  800e6),
            -2: (10e6,  933e6),
            -3: (10e6, 1066e6),
        }[speedgrade]

        self.vco_freq_range = {
            -1: (600e6, 1200e6),
            -2: (600e6, 1440e6),
            -3: (600e6, 1600e6),
        }[speedgrade]
        
        self.clkout0_divide_range = (2, (128 + 1/8), 1/8) # Fractional Divide available on CLKOUT0
        self.clkfbout_mult_frange = (2,  64+1/8, 1/8) # Fractional multiply

        if not fractional:
            # Fractionnal opperation is incompatible with fine phase shift, this constrains the configuration to integers only
            self.clkout0_divide_range = (self.clkout0_divide_range[0], ceil(self.clkout0_divide_range[1]))  
            self.clkfbout_mult_frange = (self.clkfbout_mult_frange[0], ceil(self.clkfbout_mult_frange[1]))  

    def do_finalize(self):
        XilinxClocking.do_finalize(self)
        config = self.compute_config()
        mmcm_fb = Signal()
        self.params.update(
            # Global.
            p_BANDWIDTH = "OPTIMIZED",
            i_RST       = self.reset,
            i_PWRDWN    = self.power_down,
            o_LOCKED    = self.locked,

            # VCO.
            p_REF_JITTER1     = 0.01,
            p_CLKIN1_PERIOD   = 1e9/self.clkin_freq,
            p_CLKFBOUT_MULT_F = config["clkfbout_mult"],
            p_DIVCLK_DIVIDE   = config["divclk_divide"],
            i_CLKIN1          = self.clkin,
            i_CLKFBIN         = mmcm_fb,
            o_CLKFBOUT        = mmcm_fb,
        )
        for n, clkout in sorted(self.clkouts.items()):
            if n == 0:
                self.params["p_CLKOUT{}_DIVIDE_F".format(n)] = config["clkout{}_divide".format(n)]
            else:
                self.params["p_CLKOUT{}_DIVIDE".format(n)] = config["clkout{}_divide".format(n)]
            self.params["p_CLKOUT{}_PHASE".format(n)] = config["clkout{}_phase".format(n)]
            self.params["o_CLKOUT{}".format(n)]       = clkout.clk
        self.specials += Instance("MMCME2_ADV", name=self.name or "", **self.params)

# Xilinx / 7-Series Runtime Clock Mux --------------------------------------------------------------

class S7ClockMux(LiteXModule):
    """Select two or three running clock profiles using dedicated BUFGCTRLs.

    Use with ClockRateControl.from_backend(). Provide the PLL/MMCM outputs
    (e.g. create_clkout with buf=None and with_reset=False) and their optional
    readiness levels. If used, these levels must indicate stable clocks.
    All sources must run during a switch. The consumer must be quiesced and
    the management clock must run independently of the mux output.

    Allow several periods of the slowest source in settle_cycles, including
    readiness synchronization. The platform constrains all source clocks
    and can place the BUFGCTRLs on adjacent sites for their dedicated cascade.
    Pass the Vivado platform and a unique name to register the asynchronous
    select-pin constraint automatically.
    """
    def __init__(self, clocks, rates, initial_hz, *, ready=None, settle_cycles=64, name=None, platform=None):
        clocks, rates = tuple(clocks), tuple(rates)
        if len(rates) not in (2, 3) or len(clocks) != len(rates):
            raise ValueError("BUFGCTRL requires two or three clocks and matching rates.")
        if any(type(hz) is not int or not 0 < hz < 2**32 for hz in rates):
            raise ValueError("Clock rates must be positive 32-bit integers in Hz.")
        if len(set(rates)) != len(rates) or type(initial_hz) is not int or initial_hz not in rates:
            raise ValueError("BUFGCTRL requires distinct rates and a matching initial rate.")
        if type(settle_cycles) is not int or settle_cycles < 2:
            raise ValueError("BUFGCTRL requires at least two settling cycles.")
        ready = (1,) * len(rates) if ready is None else tuple(ready)
        if len(ready) != len(rates):
            raise ValueError("Provide one readiness input per clock.")
        vivado = platform is not None and isinstance(platform.toolchain, XilinxVivadoToolchain)
        if vivado and not name:
            raise ValueError("Pass a unique S7ClockMux name when using a Vivado platform.")

        initial = rates.index(initial_hz)
        width   = (len(rates) - 1).bit_length()
        cascade = len(rates) == 3
        self.rates      = rates
        self.initial_hz = initial_hz
        self.clk        = Signal()
        self.request    = request  = stream.Endpoint([("profile", width)])
        self.response   = response = stream.Endpoint([
            ("profile", width), ("clock_valid", 1), ("error", 1),
        ])

        # # #

        select_pair = Signal(reset=initial == 1)
        select_last = Signal(reset=initial == 2)
        paired_clk  = Signal() if cascade else self.clk
        locked      = Array(Signal() for _ in clocks)
        all_ready   = Reduce("AND", list(locked))
        requested   = Signal(width)
        active      = Signal(width, reset=initial)
        count       = Signal(max=settle_cycles)
        settled     = Signal(reset=1)
        valid       = Signal()
        error       = Signal()

        self.specials += [MultiReg(source, synced) for source, synced in zip(ready, locked)]
        stages = [("pair", clocks[0], clocks[1], paired_clk, select_pair, initial == 1)]
        if cascade:
            stages.append(("output", paired_clk, clocks[2], self.clk, select_last, initial == 2))
        mux_names = []
        for stage, clk0, clk1, output, select, initial_input in stages:
            instance_name = f"{name}_{stage}" if name else ""
            mux_names.append(instance_name)
            self.specials += Instance("BUFGCTRL",
                name           = instance_name,
                p_INIT_OUT     = 0,
                p_PRESELECT_I0 = "FALSE" if initial_input else "TRUE",
                p_PRESELECT_I1 = "TRUE" if initial_input else "FALSE",
                i_I0           = clk0,
                i_I1           = clk1,
                # Use the synchronized select path for glitch-free switching.
                i_S0           = ~select,
                i_S1           = select,
                i_CE0          = 1,
                i_CE1          = 1,
                i_IGNORE0      = 0,
                i_IGNORE1      = 0,
                o_O            = output,
            )
        if vivado:
            # UG472 permits asynchronous S0/S1 changes with CE asserted and
            # IGNORE deasserted. The generated clocks and data paths stay timed.
            cells = " ".join(mux_names)
            platform.add_platform_command(
                "set_false_path -to [get_pins -of_objects "
                "[get_cells {{" + cells + "}}] "
                "-filter {{REF_PIN_NAME == S0 || REF_PIN_NAME == S1}}]"
            )

        self.comb += [
            response.profile.eq(active),
            response.clock_valid.eq(valid),
            response.error.eq(error),
        ]
        lost_lock = [NextValue(valid, 0), NextValue(error, 1), NextState("REPLY")]
        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            request.ready.eq(1),
            If(request.valid,
                If(settled & locked[active] & (request.profile == active),
                    NextValue(valid, 1),
                    NextValue(error, 0),
                    NextState("REPLY"),
                ).Elif(all_ready & (request.profile < len(rates)),
                    NextValue(requested, request.profile),
                    NextValue(settled, 0),
                    NextValue(count, settle_cycles - 1),
                    If(request.profile == 2,
                        NextValue(select_last, 1),
                        NextState("OUTPUT"),
                    ).Else(
                        NextValue(select_pair, request.profile[0]),
                        NextState("PAIR" if cascade else "OUTPUT"),
                    ),
                ).Else(
                    NextValue(valid, settled & locked[active]),
                    NextValue(error, 1),
                    NextState("REPLY"),
                ),
            ),
        )
        if cascade:
            # Settle the upstream mux before selecting it at the output.
            fsm.act("PAIR",
                If(~all_ready,
                    *lost_lock,
                ).Elif(count == 0,
                    NextValue(select_last, 0),
                    NextValue(count, settle_cycles - 1),
                    NextState("OUTPUT"),
                ).Else(
                    NextValue(count, count - 1),
                ),
            )
        fsm.act("OUTPUT",
            If(~all_ready,
                *lost_lock,
            ).Elif(count == 0,
                NextValue(active, requested),
                NextValue(settled, 1),
                NextValue(valid, 1),
                NextValue(error, 0),
                NextState("REPLY"),
            ).Else(
                NextValue(count, count - 1),
            ),
        )
        fsm.act("REPLY",
            response.valid.eq(1),
            If(response.ready, NextState("IDLE")),
        )

# Xilinx / 7-Series IDELAY CTRL --------------------------------------------------------------------

class S7IDELAYCTRL(LiteXModule):
    def __init__(self, cd, reset_cycles=16):
        reset_counter = Signal(log2_int(reset_cycles), reset=reset_cycles - 1)
        ic_reset      = Signal(reset=1)
        sync = getattr(self.sync, cd.name)
        sync += \
            If(reset_counter != 0,
                reset_counter.eq(reset_counter - 1)
            ).Else(
                ic_reset.eq(0)
            )
        self.specials += Instance("IDELAYCTRL", i_REFCLK=cd.clk, i_RST=ic_reset)
