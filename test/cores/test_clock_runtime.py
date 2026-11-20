#
# This file is part of LiteX.
#
# SPDX-License-Identifier: BSD-2-Clause

import unittest

from migen import Instance, Signal, run_simulation

from litex.gen import LiteXModule
from litex.soc.interconnect import stream

from litex.soc.cores.clock import ClockRateControl, ECP5ClockMux


class TestClockRateControl(unittest.TestCase):
    def test_target_binds_backend_for_video_modesetting(self):
        class PixelClockBackend(LiteXModule):
            def __init__(self, reference):
                self.rates      = (74_250_000, 148_500_000)
                self.initial_hz = self.rates[0]
                self.clk        = reference
                self.request    = stream.Endpoint([("profile", 1)])
                self.response   = stream.Endpoint([
                    ("profile", 1), ("clock_valid", 1), ("error", 1),
                ])

        reference = Signal()
        backend = PixelClockBackend(reference)
        dut = ClockRateControl.from_backend(backend)
        self.assertIs(dut.clk, reference)

        def stimulus():
            yield dut.request.hz.eq(148_500_000)
            yield dut.request.valid.eq(1)
            yield
            yield dut.request.valid.eq(0)
            yield
            assert (yield backend.request.valid)
            assert (yield backend.request.profile) == 1
            yield backend.request.ready.eq(1)
            yield
            yield backend.request.ready.eq(0)
            yield
            assert (yield backend.response.ready)
            yield backend.response.profile.eq(1)
            yield backend.response.clock_valid.eq(1)
            yield backend.response.valid.eq(1)
            yield
            yield backend.response.valid.eq(0)
            yield
            assert (yield dut.response.valid)
            assert (yield dut.response.hz) == 148_500_000
            assert not (yield dut.response.error)
            yield dut.response.ready.eq(1)
            yield

        run_simulation(dut, stimulus())

    def test_invalid_profiles(self):
        for rates, initial in [
            ((), 1), ((0,), 0), ((2**32,), 2**32), ((1, 1), 1),
            ((1,), 2), ((1.0,), 1),
        ]:
            with self.subTest(rates=rates, initial=initial):
                with self.assertRaises(ValueError):
                    ClockRateControl(rates, initial)

    def test_incompatible_backend_layout(self):
        backend = ECP5ClockMux(Signal(), Signal(), (100_000_000, 208_000_000),
            initial_hz=100_000_000)
        backend.request = stream.Endpoint([("profile", 2)])
        with self.assertRaisesRegex(ValueError, "profile layout"):
            ClockRateControl.from_backend(backend)

    def test_rate_changes_with_backpressure_and_rollback(self):
        dut = ClockRateControl((52_000_000, 104_000_000, 208_000_000), initial_hz=52_000_000)

        def request(hz):
            yield dut.request.hz.eq(hz)
            yield dut.request.valid.eq(1)
            yield
            assert (yield dut.request.ready)
            yield dut.request.valid.eq(0)
            # The client may change its request payload once it was accepted.
            yield dut.request.hz.eq(0)
            yield

        def configure(profile, survivor, valid=1, error=0):
            for _ in range(3):
                assert (yield dut.configure.valid)
                assert (yield dut.configure.profile) == profile
                assert not (yield dut.request.ready)
                yield

            yield dut.configure.ready.eq(1)
            yield
            yield dut.configure.ready.eq(0)
            yield
            assert not (yield dut.configure.valid)
            assert not (yield dut.response.valid)
            yield dut.configured.profile.eq(survivor)
            yield dut.configured.clock_valid.eq(valid)
            yield dut.configured.error.eq(error)
            yield dut.configured.valid.eq(1)
            yield
            assert (yield dut.configured.ready)
            yield dut.configured.valid.eq(0)
            yield

        def response(hz, error):
            for _ in range(3):
                assert (yield dut.response.valid)
                assert not (yield dut.configure.valid)
                assert not (yield dut.request.ready)
                assert (yield dut.response.hz) == hz
                assert (yield dut.active_hz) == hz
                assert (yield dut.response.error) == error
                yield

            yield dut.response.ready.eq(1)
            yield
            yield dut.response.ready.eq(0)
            yield
            assert (yield dut.request.ready)

        def stimulus():
            yield from request(100_000_000)
            yield from response(52_000_000, 1)

            yield from request(208_000_000)
            yield from configure(2, 2)
            yield from response(208_000_000, 0)

            yield from request(104_000_000)
            yield from configure(1, 2, error=1)
            yield from response(208_000_000, 1)

            yield from request(104_000_000)
            yield from configure(1, 0, valid=0)
            yield from response(0, 1)

            yield from request(104_000_000)
            yield from configure(1, 3)
            yield from response(0, 1)

            yield from request(52_000_000)
            yield from configure(0, 0)
            yield from response(52_000_000, 0)

        run_simulation(dut, stimulus())


class TestECP5ClockMux(unittest.TestCase):
    def test_switch_and_lock_loss(self):
        locked0 = Signal(reset=1)
        locked1 = Signal(reset=1)
        backend = ECP5ClockMux(Signal(), Signal(), (100_000_000, 208_000_000),
            initial_hz=208_000_000, ready0=locked0, ready1=locked1, settle_cycles=8)
        dut = ClockRateControl.from_backend(backend)

        # The primitive is a vendor black box. Simulate the management protocol.
        fragment = dut.get_fragment()
        primitive, = [special for special in fragment.specials if isinstance(special, Instance)]
        self.assertEqual(primitive.of, "DCSC")
        fragment.specials.remove(primitive)

        def request(hz, expected, error, minimum_wait=0, drop_lock=None):
            assert (yield dut.request.ready)
            yield dut.request.hz.eq(hz)
            yield dut.request.valid.eq(1)
            yield
            yield dut.request.valid.eq(0)
            if drop_lock is not None:
                for _ in range(4):
                    yield
                yield drop_lock.eq(0)
            for _ in range(minimum_wait):
                assert not (yield dut.response.valid)
                yield
            for _ in range(32):
                if (yield dut.response.valid):
                    break
                yield
            else:
                raise AssertionError("missing response")
            for _ in range(3):
                assert (yield dut.response.valid)
                assert (yield dut.response.hz) == expected
                assert (yield dut.response.error) == error
                yield
            yield dut.response.ready.eq(1)
            yield
            yield dut.response.ready.eq(0)
            yield

        def bench():
            for _ in range(4):
                yield
            yield from request(100_000_000, 100_000_000, 0, 8)
            yield locked1.eq(0)
            for _ in range(4):
                yield
            yield from request(208_000_000, 100_000_000, 1)
            yield locked1.eq(1)
            for _ in range(4):
                yield
            yield from request(208_000_000, 208_000_000, 0, 8)
            yield from request(100_000_000, 0, 1, drop_lock=locked1)
            yield from request(208_000_000, 0, 1)
            yield locked1.eq(1)
            for _ in range(4):
                yield
            yield from request(100_000_000, 100_000_000, 0, 8)

        run_simulation(fragment, bench())
