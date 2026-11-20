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

# Xilinx / 7-Series Runtime DRP --------------------------------------------------------------------

class S7ClockDRP(LiteXModule):
    """Reprogram an S7MMCM or S7PLL using target-supplied DRP profiles.

    Configure the generator and its output clock before passing them here.
    This backend owns the generator, its reset, and DRP and loads initial_hz
    before accepting requests. Constrain the output for the fastest profile.
    Profiles map Hz to ordered (address, preserve_mask, bits_to_set) tuples
    in the XAPP888 format. Supply complete tables for the chosen primitive,
    reference clock and outputs, including any required lock/filter values.
    A table must be safe to apply after any previous or partial profile.

    Bind with ClockRateControl.from_backend(). The caller must keep all
    consumers of the generator quiesced through reset and relock and run
    the FSM from an independent, always-on DCLK within the device limits.
    The profile request and response use valid/ready in the DCLK domain.
    Responses may be backpressured. LOCKED and the applied reset are
    synchronized back to DCLK. The initial table is reapplied at management
    reset because the DRP registers retain their contents. Timeouts are
    expressed in DCLK cycles.
    A failed switch leaves the generator in reset until a new profile can
    be applied successfully.
    """
    def __init__(self, generator, clk, profiles, initial_hz, *, drp_timeout=256, lock_timeout=100_000):
        if not isinstance(generator, (S7MMCM, S7PLL)):
            raise ValueError("S7ClockDRP requires an S7MMCM or S7PLL.")
        if "i_DEN" in generator.params:
            raise ValueError("The clock generator DRP is already connected.")
        rates = tuple(profiles)
        if not rates or any(type(hz) is not int or not 0 < hz < 2**32 for hz in rates):
            raise ValueError("Clock rates must be positive 32-bit integers in Hz.")
        if type(initial_hz) is not int or initial_hz not in profiles:
            raise ValueError("The initial rate must match a DRP profile.")
        if any(type(timeout) is not int or timeout < 2 for timeout in (drp_timeout, lock_timeout)):
            raise ValueError("DRP timeouts must be at least two cycles.")

        starts, ends, entries = [], [], []
        for hz in rates:
            starts.append(len(entries))
            for address, preserve, bits in profiles[hz]:
                if any(type(value) is not int or not 0 <= value < 1 << width
                       for value, width in [(address, 7), (preserve, 16), (bits, 16)]):
                    raise ValueError("DRP entries require a 7-bit address and two 16-bit values.")
                entries.append((bits << 23) | (preserve << 7) | address)
            if starts[-1] == len(entries):
                raise ValueError("Each DRP profile must have at least one register entry.")
            ends.append(len(entries) - 1)

        initial       = rates.index(initial_hz)
        profile_width = max(1, (len(rates) - 1).bit_length())
        self.generator  = generator
        self.clk        = clk
        self.rates      = rates
        self.initial_hz = initial_hz
        self.request    = request  = stream.Endpoint([("profile", profile_width)])
        self.response   = response = stream.Endpoint([
            ("profile", profile_width), ("clock_valid", 1), ("error", 1),
        ])

        # # #

        den, dwe, drdy = Signal(), Signal(), Signal()
        read_data      = Signal(16)
        write_data     = Signal(16)
        index          = Signal(max=max(2, len(entries)))
        last           = Signal.like(index)
        entry          = Signal(39)
        address        = entry[:7]
        preserve_mask  = entry[7:23]
        bits_to_set    = entry[23:]
        timer          = Signal(max=max(drp_timeout, lock_timeout))
        locked         = Signal()
        reset_applied  = self._reset_applied = Signal()
        held           = Signal(reset=1)
        outstanding    = Signal(reset_less=True)
        startup        = Signal(reset=1)
        configured     = Signal()
        active         = Signal(profile_width, reset=initial)
        requested      = Signal(profile_width, reset=initial)
        valid          = Signal()
        error          = Signal()

        generator.params.update(
            i_DCLK  = ClockSignal(),
            i_DEN   = den & ~ResetSignal(),
            i_DWE   = dwe,
            i_DADDR = address,
            i_DI    = write_data,
            o_DO    = read_data,
            o_DRDY  = drdy,
        )
        self.specials += MultiReg(generator.locked, locked)
        self.comb += [
            generator.reset.eq(held | ResetSignal()),
            entry.eq(Array(Constant(word, 39) for word in entries)[index]),
            response.profile.eq(active),
            response.clock_valid.eq(valid),
            response.error.eq(error),
        ]
        # Drain a late response after a timeout before starting another
        # transfer. DRDY does not carry an address or transaction identifier.
        self.sync += If(den & ~ResetSignal(),
            outstanding.eq(1),
        ).Elif(drdy,
            outstanding.eq(0),
        )

        self.fsm = fsm = FSM(reset_state="STARTUP")

        def wait_for(condition, *on_ready):
            return If(condition, *on_ready).Elif(timer == 0,
                NextValue(held, 1),
                NextValue(valid, 0),
                NextValue(error, 1),
                NextState("COMPLETE"),
            ).Else(NextValue(timer, timer - 1))

        fsm.act("STARTUP",
            NextValue(index, starts[initial]),
            NextValue(last, ends[initial]),
            NextValue(timer, lock_timeout - 1),
            NextState("RESET"),
        )
        fsm.act("IDLE",
            request.ready.eq(1),
            If(request.valid,
                If((request.profile < len(rates)) & ~outstanding & ~drdy,
                    NextValue(requested, request.profile),
                    NextValue(index, Array(starts)[request.profile]),
                    NextValue(last, Array(ends)[request.profile]),
                    NextValue(configured, 0),
                    NextValue(held, 1),
                    NextValue(timer, lock_timeout - 1),
                    NextState("RESET"),
                ).Else(
                    NextValue(valid, configured & locked),
                    NextValue(error, 1),
                    NextState("REPLY"),
                ),
            ),
        )
        fsm.act("RESET",
            wait_for(reset_applied & ~locked & ~outstanding & ~drdy, NextState("READ")),
        )
        fsm.act("READ",
            den.eq(1),
            NextValue(timer, drp_timeout - 1),
            NextState("READ_WAIT"),
        )
        fsm.act("READ_WAIT",
            wait_for(drdy,
                NextValue(write_data, (read_data & preserve_mask) | bits_to_set),
                NextState("WRITE"),
            ),
        )
        fsm.act("WRITE",
            den.eq(1),
            dwe.eq(1),
            NextValue(timer, drp_timeout - 1),
            NextState("WRITE_WAIT"),
        )
        fsm.act("WRITE_WAIT",
            wait_for(drdy,
                If(index == last,
                    NextValue(held, 0),
                    NextValue(timer, lock_timeout - 1),
                    NextState("LOCK"),
                ).Else(
                    NextValue(index, index + 1),
                    NextState("READ"),
                ),
            ),
        )
        fsm.act("LOCK",
            wait_for(~reset_applied & locked,
                NextValue(active, requested),
                NextValue(configured, 1),
                NextValue(valid, 1),
                NextValue(error, 0),
                NextState("COMPLETE"),
            ),
        )
        fsm.act("COMPLETE",
            If(startup,
                NextValue(startup, 0),
                NextState("IDLE"),
            ).Else(NextState("REPLY")),
        )
        fsm.act("REPLY",
            response.valid.eq(1),
            If(response.ready, NextState("IDLE")),
        )

    def do_finalize(self):
        # The Xilinx generator adds a reference-clock pipeline to RST when
        # it finalizes. Observe the actual pin before starting DRP transfers.
        self.specials += MultiReg(self.generator.reset, self._reset_applied)

    @staticmethod
    def clkout0_divider_profiles(vco_hz, dividers):
        """Build fixed-VCO profiles for CLKOUT0 on an S7MMCM or S7PLL.

        Use an integer, zero-phase CLKOUT0 configuration. The reference,
        feedback, and all other outputs keep their bitstream settings. The
        caller selects divisors that satisfy the generator and output limits.
        """
        if type(vco_hz) is not int or vco_hz <= 0:
            raise ValueError("The VCO rate must be a positive integer in Hz.")

        profiles = {}
        for divider in dividers:
            if type(divider) is not int or not 2 <= divider <= 126:
                raise ValueError("CLKOUT0 divider must be an integer from 2 through 126.")
            rate = (vco_hz + divider // 2) // divider
            if not 0 < rate < 2**32 or rate in profiles:
                raise ValueError("CLKOUT0 dividers must produce distinct positive 32-bit rates.")
            high = divider // 2
            low  = divider - high
            # XAPP888: PowerReg, CLKOUT0 counts, then EDGE/NO_COUNT. For
            # odd divides, EDGE moves half a VCO cycle from low to high.
            profiles[rate] = [
                (0x28, 0x0000, 0xffff),
                (0x08, 0xf000, (high << 6) | low),
                (0x09, 0xff3f, (divider & 1) << 7),
            ]
        return profiles

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
