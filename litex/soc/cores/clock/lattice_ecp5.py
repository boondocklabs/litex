#
# This file is part of LiteX.
#
# Copyright (c) 2018-2026 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2021 George Hilliard <thirtythreeforty@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect import stream

from litex.soc.cores.clock.common import *

# Lattice / ECP5 PLL -------------------------------------------------------------------------------

class ECP5PLL(LiteXModule):
    nclkouts_max    = 4
    clki_div_range  = (1, 128+1)
    clkfb_div_range = (1, 128+1)
    clko_div_range  = (1, 128+1)
    clki_freq_range = (    8e6,  400e6)
    clko_freq_range = (3.125e6,  400e6)
    vco_freq_range  = (  400e6,  800e6)
    pfd_freq_range  = (   10e6,  400e6)
    vco_tie_break   = +1 # Prefer the lower VCO on equal output errors.

    def __init__(self, bel=None, name=None):
        self.logger = logging.getLogger("ECP5PLL")
        self.logger.info("Creating ECP5PLL.")
        self.reset      = Signal()
        self.locked     = Signal()
        self.stdby      = Signal()
        self.clkin_freq = None
        self.vcxo_freq  = None
        self.dpa_en     = False
        self.nclkouts   = 0
        self.clkouts    = {}
        self.config     = {}
        self.params     = {}
        self.bel        = bel
        self.name       = name

    def register_clkin(self, clkin, freq):
        check_freq_range(freq, self.clki_freq_range, "Input clock frequency")
        self.clkin = connect_clkin(self, clkin)
        self.clkin_freq = freq
        register_clkin_log(self.logger, clkin, freq)

    def create_clkout(self, cd, freq, phase=0, margin=1e-2, with_reset=True, uses_dpa=True):
        check_freq_range(freq, self.clko_freq_range, "Output clock frequency")
        check_margin(margin)
        check_clkout_cd_unused(self, cd)
        check_clkout_count(self.nclkouts, self.nclkouts_max)
        clkout = Signal()
        self.clkouts[self.nclkouts] = ClkOutDPA(clkout, freq, phase, margin, uses_dpa)
        connect_clkout(self, cd, clkout, reset=~self.locked, with_reset=with_reset)
        create_clkout_log(self.logger, cd.name, freq, margin, self.nclkouts)
        self.nclkouts += 1

    def compute_config(self):
        check_clkin_registered(hasattr(self, "clkin"))
        check_clkouts(self.nclkouts)
        best_config = None
        best_score  = None
        # Iterate on CLKI dividers...
        for clki_div in range(*self.clki_div_range):
            # Check if in PFD range.
            (pfd_freq_min, pfd_freq_max) = self.pfd_freq_range
            if not (pfd_freq_min <= self.clkin_freq/clki_div <= pfd_freq_max):
                continue
            # Iterate on CLKO dividers... (to get us in VCO range)
            for clkofb_div in range(*self.clko_div_range):
                # Iterate on CLKFB dividers...
                for clkfb_div in range(*self.clkfb_div_range):
                    errors   = []
                    config   = {"clki_div": clki_div}
                    vco_freq = (self.clkin_freq/clki_div)*clkfb_div*clkofb_div
                    (vco_freq_min, vco_freq_max) = self.vco_freq_range
                    all_valid = True
                    # If in VCO range, find dividers for all outputs.
                    if vco_freq_min <= vco_freq <= vco_freq_max:
                        config["clkfb"]  = None
                        clkout_configs   = {}
                        feedback_configs = {}
                        for n, clkout in sorted(self.clkouts.items()):
                            # Only the current feedback divider can serve as feedback.
                            # Outputs using dynamic phase adjustment cannot be reused.
                            clk_freq = vco_freq/clkofb_div
                            error    = clkout_freq_error(clk_freq, clkout.freq)
                            if error <= clkout.margin and not (clkout.uses_dpa and self.dpa_en):
                                feedback_configs[n] = (error, clk_freq, clkofb_div, clkout.phase)
                            best_clkout = clkout_best_divider(
                                clkout.freq,
                                clkout.margin,
                                clkdiv_candidates([self.clko_div_range], ideal=vco_freq/clkout.freq),
                                lambda d: vco_freq/d
                            )
                            if best_clkout is None:
                                all_valid = False
                                break
                            error, clk_freq, d = best_clkout
                            clkout_configs[n]  = (error, clk_freq, d, clkout.phase)
                        if all_valid:
                            for n, (error, clk_freq, d, p) in sorted(clkout_configs.items()):
                                if n in feedback_configs and d == feedback_configs[n][2]:
                                    config["clkfb"] = n

                            if config["clkfb"] is None and self.nclkouts == self.nclkouts_max:
                                best_feedback_score  = None
                                best_feedback_clkout = None
                                for n, feedback_config in feedback_configs.items():
                                    candidate_clkouts    = dict(clkout_configs)
                                    candidate_clkouts[n] = feedback_config
                                    candidate_errors     = [clkout[0] for clkout in candidate_clkouts.values()]
                                    candidate_score      = clkout_config_score(
                                        candidate_errors, vco_freq, self.vco_tie_break)
                                    if best_feedback_score is None or candidate_score < best_feedback_score:
                                        best_feedback_score  = candidate_score
                                        best_feedback_clkout = n
                                if best_feedback_clkout is None:
                                    # If there is no output suitable for feedback and no spare, not valid
                                    all_valid = False
                                else:
                                    clkout_configs[best_feedback_clkout] = feedback_configs[best_feedback_clkout]
                                    config["clkfb"] = best_feedback_clkout

                        if all_valid:
                            errors = []
                            for n, (error, clk_freq, d, p) in sorted(clkout_configs.items()):
                                errors.append(error)
                                config["clko{}_freq".format(n)]  = clk_freq
                                config["clko{}_div".format(n)]   = d
                                config["clko{}_phase".format(n)] = p
                    else:
                        all_valid = False
                    if all_valid:
                        # Reserve a spare output for feedback when no requested output is suitable.
                        if config["clkfb"] is None:
                            feedback_clkout = self.nclkouts
                            config["clkfb"] = feedback_clkout
                            config[f"clko{feedback_clkout}_div"] = clkofb_div
                        config["vco"]       = vco_freq
                        config["clkfb_div"] = clkfb_div
                        best_config, best_score = update_best_config(
                            best_config, best_score, config, errors, vco_freq, self.vco_tie_break)
        if best_config is not None:
            compute_config_log(self.logger, best_config)
            return best_config
        raise pll_config_error(self.clkin_freq, self.clkouts)

    def expose_dpa(self):
        self.dpa_en     = True
        self.phase_sel  = Signal(2)
        self.phase_dir  = Signal()
        self.phase_step = Signal()
        self.phase_load = Signal()

        # # #

        self.params.update(
            p_DPHASE_SOURCE = "ENABLED",
            i_PHASESEL0     = self.phase_sel[0],
            i_PHASESEL1     = self.phase_sel[1],
            i_PHASEDIR      = self.phase_dir,
            i_PHASESTEP     = self.phase_step,
            i_PHASELOADREG  = self.phase_load
        )

    def do_finalize(self):
        config = self.compute_config()
        locked = Signal()
        n_to_l = {0: "P", 1: "S", 2: "S2", 3: "S3"}
        self.params.update(
            attr=[
                ("FREQUENCY_PIN_CLKI",     str(self.clkin_freq/1e6)),
                ("ICP_CURRENT",            "6"),
                ("LPF_RESISTOR",          "16"),
                ("MFG_ENABLE_FILTEROPAMP", "1"),
                ("MFG_GMCREF_SEL",         "2")],
            i_RST           = self.reset,
            i_CLKI          = self.clkin,
            i_STDBY         = self.stdby,
            o_LOCK          = locked,
            p_FEEDBK_PATH   = f"INT_O{n_to_l[config['clkfb']]}",
            p_CLKFB_DIV     = config["clkfb_div"],
            p_CLKI_DIV      = config["clki_div"]
        )
        self.comb += self.locked.eq(locked & ~self.reset)
        clkouts = dict(self.clkouts)
        if config["clkfb"] not in clkouts:
            clkouts[config["clkfb"]] = ClkOutDPA(Signal(), 0, 0, 0, False)
        for n, clkout in sorted(clkouts.items()):
            div    = config[f"clko{n}_div"]
            phase  = round(clkout.phase*div/45)
            self.params[f"p_CLKO{n_to_l[n]}_ENABLE"] = "ENABLED"
            self.params[f"p_CLKO{n_to_l[n]}_DIV"]    = div
            self.params[f"p_CLKO{n_to_l[n]}_FPHASE"] = phase & 7
            self.params[f"p_CLKO{n_to_l[n]}_CPHASE"] = (phase >> 3) + (div - 1)
            self.params[f"o_CLKO{n_to_l[n]}"]        = clkout.clk
            if clkout.freq > 0:  # i.e. not a feedback-only clock
                self.params["attr"].append((f"FREQUENCY_PIN_CLKO{n_to_l[n]}", str(clkout.freq/1e6)))
        if self.bel:
            self.params["attr"].append(("BEL", self.bel))
        self.specials += Instance("EHXPLLL", name=self.name or "", **self.params)

# Lattice / ECP5 Dynamic Delay ---------------------------------------------------------------------

class ECP5DynamicDelay(LiteXModule):
    tap_delay = 25e-12
    ntaps     = 128

    def __init__(self, i=None, o=None, taps=None):
        self.i    = Signal() if i is None else i
        self.o    = Signal() if o is None else o
        self.taps = Signal(max=self.ntaps) if taps is None else taps

        # # #

        rst       = Signal()
        move      = Signal()
        done      = Signal()
        change    = Signal()
        curr_taps = Signal(max=self.ntaps)

        # DELAYF Instance.
        self.specials += Instance("DELAYF",
            p_DEL_MODE  = "USER_DEFINED",
            p_DEL_VALUE = self.taps.reset,
            i_A         = self.i,
            o_Z         = self.o,
            i_LOADN     = ~(ResetSignal() | rst),
            i_MOVE      = move,
            i_DIRECTION = 0,
            o_CFLAG     = Signal()
        )

        # FSM.
        self.comb += done.eq(  self.taps == curr_taps)
        self.comb += change.eq(self.taps != curr_taps)
        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(change,
                NextState("DELAYF-RST")
            )
        )
        fsm.act("DELAYF-RST",
            rst.eq(1),
            NextValue(move,      0),
            NextValue(curr_taps, 0),
            NextState("DELAYF-MOVE")
        )
        fsm.act("DELAYF-MOVE",
             If(done,
                NextValue(move, 0),
                NextState("IDLE")
            ).Else(
                NextValue(move, ~move),
                If(move,
                    NextValue(curr_taps, curr_taps + 1)
                )
            )
        )


# Lattice / ECP5 Dedicated Clock Select -------------------------------------------------------------

class ECP5ClockMux(LiteXModule):
    """Select between two continuously running clocks with the ECP5 DCSC.

    The target supplies the two clock signals and optional ready levels, and
    constrains the output for the faster input. The management clock must be
    always running. Set `settle_cycles` to cover several periods of both
    inputs, including the readiness synchronizer delay.
    """
    def __init__(self, clk0, clk1, rates, initial_hz, ready0=1, ready1=1, settle_cycles=16):
        rates = tuple(rates)
        if len(rates) != 2 or any(type(hz) is not int or not 0 < hz < 2**32 for hz in rates):
            raise ValueError("DCSC requires two positive 32-bit integer rates in Hz.")
        if rates[0] == rates[1] or type(initial_hz) is not int or initial_hz not in rates:
            raise ValueError("DCSC requires distinct rates and a matching initial rate.")
        if type(settle_cycles) is not int or settle_cycles < 2:
            raise ValueError("DCSC requires at least two settling cycles.")

        self.rates      = rates
        self.initial_hz = initial_hz
        self.clk        = Signal()
        self.request    = request  = stream.Endpoint([("profile", 1)])
        self.response   = response = stream.Endpoint([
            ("profile", 1), ("clock_valid", 1), ("error", 1),
        ])

        # # #

        selected   = Signal(reset=rates.index(initial_hz))
        locked0    = Signal()
        locked1    = Signal()
        count      = Signal(max=settle_cycles)
        settled    = Signal(reset=1)
        valid      = Signal()
        error      = Signal()
        both_ready = locked0 & locked1

        self.specials += [
            MultiReg(ready0, locked0),
            MultiReg(ready1, locked1),
            Instance("DCSC",
                # The ECP5 bitstream writer uses the attribute and the packer
                # reads the primitive parameter for clock propagation.
                attr      = {("DCSMODE", "NEG")},
                p_DCSMODE = "NEG",
                i_MODESEL = 0,
                i_CLK0    = clk0,
                i_CLK1    = clk1,
                i_SEL0    = ~selected,
                i_SEL1    = selected,
                o_DCSOUT  = self.clk,
            ),
        ]
        self.comb += [
            response.profile.eq(selected),
            response.clock_valid.eq(valid),
            response.error.eq(error),
        ]

        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            request.ready.eq(1),
            If(request.valid,
                If(both_ready,
                    NextValue(selected, request.profile),
                    NextValue(settled, 0),
                    NextValue(count, settle_cycles - 1),
                    NextState("SETTLE"),
                ).Else(
                    NextValue(valid, settled & Mux(selected, locked1, locked0)),
                    NextValue(error, 1),
                    NextState("REPLY"),
                ),
            ),
        )
        fsm.act("SETTLE",
            If(~both_ready,
                NextValue(valid, 0),
                NextValue(error, 1),
                NextState("REPLY"),
            ).Elif(count == 0,
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
            If(response.ready,
                NextState("IDLE"),
            ),
        )
