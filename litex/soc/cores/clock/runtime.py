#
# This file is part of LiteX.
#
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import LiteXModule

from litex.soc.interconnect import stream


# Runtime Clock Rate Control -----------------------------------------------------------------------

class ClockRateControl(LiteXModule):
    """Translate rate requests in Hz to platform-defined clock profiles.

    All endpoints run in an always-on management domain. The caller must
    quiesce consumers before requesting a change. The backend owns switching
    or reprogramming the clock, timeouts, and confirming when it is usable.

    Targets bind a platform clock backend with `from_backend()`. Clients
    use `clk`, `request`, `response` and `active_hz`.

    Profile numbers are zero-based indices into rates. A backend reports the
    profile that actually survived, including on a failed change. If no
    clock is usable, it clears clock_valid. The client then receives zero Hz
    and an error. The platform must establish `initial_hz` at startup.
    """
    def __init__(self, rates, initial_hz):
        rates = tuple(rates)
        if not rates or any(type(hz) is not int or not 0 < hz < 2**32 for hz in rates):
            raise ValueError("Clock rates must be positive 32-bit integers in Hz.")
        if len(set(rates)) != len(rates):
            raise ValueError("Clock rates must be unique.")
        if type(initial_hz) is not int or initial_hz not in rates:
            raise ValueError("The initial clock rate must match a supported profile.")

        profile_width = max(1, (len(rates) - 1).bit_length())
        self.rates = rates
        self.request    = request    = stream.Endpoint([("hz", 32)])
        self.response   = response   = stream.Endpoint([("hz", 32), ("error", 1)])
        self.configure  = configure  = stream.Endpoint([("profile", profile_width)])
        self.configured = configured = stream.Endpoint([
            ("profile", profile_width), ("clock_valid", 1), ("error", 1),
        ])
        self.active_hz = Signal(32, reset=initial_hz)

        # # #

        supported      = Signal()
        requested      = Signal(profile_width)
        pending        = Signal(profile_width)
        returned_hz    = Signal(32)
        returned_valid = Signal()
        error          = Signal()

        self.comb += [
            Case(request.hz, {
                hz: [supported.eq(1), requested.eq(index)]
                for index, hz in enumerate(rates)
            }),
            returned_valid.eq(configured.clock_valid & (configured.profile < len(rates))),
            returned_hz.eq(Array(Constant(hz, 32) for hz in rates)[configured.profile]),
            configure.profile.eq(pending),
            response.hz.eq(self.active_hz),
            response.error.eq(error),
        ]

        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            request.ready.eq(1),
            If(request.valid,
                If(supported,
                    NextValue(pending, requested),
                    NextState("CONFIGURE"),
                ).Else(
                    NextValue(error, 1),
                    NextState("REPLY"),
                )
            ),
        )
        fsm.act("CONFIGURE",
            configure.valid.eq(1),
            If(configure.ready,
                NextState("WAIT"),
            ),
        )
        fsm.act("WAIT",
            configured.ready.eq(1),
            If(configured.valid,
                NextValue(self.active_hz, Mux(returned_valid, returned_hz, 0)),
                NextValue(error, configured.error | ~returned_valid | (configured.profile != pending)),
                NextState("REPLY"),
            ),
        )
        fsm.act("REPLY",
            response.valid.eq(1),
            If(response.ready,
                NextState("IDLE"),
            ),
        )

    @classmethod
    def from_backend(cls, backend):
        """Bind a target-owned backend and expose its clock to the client.

        A backend provides clk, rates, initial_hz, and request/response
        endpoints with the profile layouts used by configure/configured.
        The target supplies the reference clock and any vendor parameters
        when constructing the backend.
        """
        control = cls(backend.rates, backend.initial_hz)
        for expected, actual in (
            (control.configure, backend.request),
            (control.configured, backend.response),
        ):
            if expected.description.payload_layout != actual.description.payload_layout:
                raise ValueError("Clock backend endpoint does not match the profile layout.")
        control.backend = backend
        control.clk = backend.clk
        control.comb += [
            control.configure.connect(backend.request),
            backend.response.connect(control.configured),
        ]
        return control
