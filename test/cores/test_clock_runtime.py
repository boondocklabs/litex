#
# This file is part of LiteX.
#
# SPDX-License-Identifier: BSD-2-Clause

import unittest
from unittest.mock import Mock

from migen import ClockDomain, Instance, Signal, run_simulation
from migen.fhdl import verilog

from litex.build.xilinx.vivado import XilinxVivadoToolchain
from litex.gen import LiteXModule
from litex.soc.interconnect import stream

from litex.soc.cores.clock import ClockRateControl, ECP5ClockMux, ECP5ClockMux3
from litex.soc.cores.clock import S7ClockDRP, S7ClockMux, S7MMCM, S7PLL


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


class TestS7ClockMux(unittest.TestCase):
    def test_vivado_timing_constraint_targets_only_this_mux(self):
        for count, names in [(2, "runtime_pair"), (3, "runtime_pair runtime_output")]:
            with self.subTest(count=count):
                clocks = tuple(Signal() for _ in range(count))
                rates = (25_000_000, 100_000_000, 200_000_000)[:count]
                platform = Mock(toolchain=XilinxVivadoToolchain())
                S7ClockMux(clocks, rates, initial_hz=rates[0], name="runtime", platform=platform)
                platform.add_platform_command.assert_called_once()
                command, = platform.add_platform_command.call_args.args
                self.assertEqual(command.format(),
                    "set_false_path -to [get_pins -of_objects "
                    "[get_cells {" + names + "}] -filter {REF_PIN_NAME == S0 || REF_PIN_NAME == S1}]")
        platform = Mock(toolchain=object())
        S7ClockMux(clocks, rates, initial_hz=rates[0], platform=platform)
        platform.add_platform_command.assert_not_called()

    def test_invalid_configuration(self):
        clocks = (Signal(), Signal(), Signal())
        for inputs, rates, initial, options in [
            (clocks, (25, 100), 25, {}),
            (clocks[:1], (25,), 25, {}),
            (clocks, (25, 25, 200), 25, {}),
            (clocks, (0, 100, 200), 100, {}),
            (clocks, (25, 100, 2**32), 25, {}),
            (clocks, (25, 100, 200), 50, {}),
            (clocks, (25, 100, 200), 25, {"ready": (1,)}),
            (clocks, (25, 100, 200), 25, {"settle_cycles": 1}),
        ]:
            with self.subTest(rates=rates, initial=initial, options=options):
                with self.assertRaises(ValueError):
                    S7ClockMux(inputs, rates, initial, **options)

    def test_two_way_controller_and_startup_selection(self):
        clocks = (Signal(), Signal())
        rates = (25_000_000, 100_000_000)
        backend = S7ClockMux(clocks, rates, initial_hz=rates[1], settle_cycles=6)
        dut = ClockRateControl.from_backend(backend)
        self.assertIs(dut.clk, backend.clk)
        fragment = dut.get_fragment()
        primitive, = [s for s in fragment.specials if isinstance(s, Instance)]
        self.assertEqual(primitive.of, "BUFGCTRL")
        self.assertIs(primitive.get_io("I0"), clocks[0])
        self.assertIs(primitive.get_io("I1"), clocks[1])
        self.assertIs(primitive.get_io("O"), dut.clk)
        fragment.specials.remove(primitive)

        def stimulus():
            self.assertEqual((yield primitive.get_io("S1")), 1)
            for _ in range(4):
                yield
            yield dut.request.hz.eq(rates[0])
            yield dut.request.valid.eq(1)
            yield
            yield dut.request.valid.eq(0)
            for _ in range(6):
                self.assertFalse((yield dut.response.valid))
                yield
            for _ in range(6):
                if (yield dut.response.valid):
                    break
                yield
            self.assertTrue((yield dut.response.valid))
            self.assertEqual((yield primitive.get_io("S1")), 0)
            self.assertEqual((yield dut.response.hz), rates[0])
            self.assertFalse((yield dut.response.error))

        run_simulation(fragment, stimulus())

    def test_three_way_cascade_and_lock_loss(self):
        clocks = tuple(Signal() for _ in range(3))
        ready = tuple(Signal(reset=1) for _ in clocks)
        rates = (25_000_000, 100_000_000, 200_000_000)
        settle_cycles = 8
        mux = S7ClockMux(clocks, rates, initial_hz=rates[2], ready=ready,
            settle_cycles=settle_cycles)
        fragment = mux.get_fragment()
        inner, = [s for s in fragment.specials if isinstance(s, Instance) and s.get_io("I0") is clocks[0]]
        outer, = [s for s in fragment.specials if isinstance(s, Instance) and s.get_io("O") is mux.clk]
        self.assertEqual((inner.of, outer.of), ("BUFGCTRL", "BUFGCTRL"))
        self.assertIs(outer.get_io("I0"), inner.get_io("O"))
        self.assertIs(outer.get_io("I1"), clocks[2])
        fragment.specials.difference_update({inner, outer})
        select_pair, select_last = inner.get_io("S1"), outer.get_io("S1")

        def request(profile, expected, valid, error=0, check_cascade=False, lose_lock=False):
            self.assertTrue((yield mux.request.ready))
            yield mux.request.profile.eq(profile)
            yield mux.request.valid.eq(1)
            yield
            yield mux.request.valid.eq(0)
            if check_cascade:
                yield
                for _ in range(settle_cycles):
                    self.assertEqual((yield select_pair), profile)
                    self.assertTrue((yield select_last))
                    self.assertFalse((yield mux.response.valid))
                    yield
                for _ in range(settle_cycles - 1):
                    self.assertFalse((yield select_last))
                    self.assertFalse((yield mux.response.valid))
                    yield
            if lose_lock:
                yield
                yield ready[1].eq(0)
            for _ in range(32):
                if (yield mux.response.valid):
                    break
                yield
            else:
                self.fail("missing clock mux response")
            for _ in range(3):
                self.assertTrue((yield mux.response.valid))
                self.assertFalse((yield mux.request.ready))
                self.assertEqual((yield mux.response.profile), expected)
                self.assertEqual((yield mux.response.clock_valid), valid)
                self.assertEqual((yield mux.response.error), error)
                yield
            yield mux.response.ready.eq(1)
            yield
            yield mux.response.ready.eq(0)
            yield

        def stimulus():
            for _ in range(4):
                yield
            yield from request(3, 2, 1, error=1)
            yield ready[0].eq(0)
            for _ in range(4):
                yield
            yield from request(0, 2, 1, error=1)
            yield from request(2, 2, 1)
            yield ready[0].eq(1)
            for _ in range(4):
                yield
            yield from request(1, 1, 1, check_cascade=True)
            yield from request(0, 1, 0, error=1, lose_lock=True)
            yield from request(2, 1, 0, error=1)
            yield ready[1].eq(1)
            for _ in range(4):
                yield
            yield from request(2, 2, 1)
            yield from request(0, 0, 1, check_cascade=True)

        run_simulation(fragment, stimulus())

    def test_verilog_uses_dedicated_cascade(self):
        clocks = tuple(Signal() for _ in range(3))
        rates = (25_000_000, 100_000_000, 200_000_000)
        mux = S7ClockMux(clocks, rates, initial_hz=rates[2])
        source = verilog.convert(mux, ios={*clocks, mux.clk}).main_source
        self.assertEqual(source.count("BUFGCTRL #("), 2)
        self.assertEqual(source.count('.PRESELECT_I0("TRUE")'), 1)
        self.assertEqual(source.count('.PRESELECT_I1("TRUE")'), 1)
        self.assertEqual(source.count(".IGNORE0(1'd0)"), 2)
        self.assertEqual(source.count(".IGNORE1(1'd0)"), 2)


class TestS7ClockDRP(unittest.TestCase):
    rates = (25_000_000, 100_000_000, 200_000_000)
    profiles = {
        25_000_000:  [(0x08, 0xf000, 0x0048)],
        100_000_000: [(0x08, 0xf000, 0x0104), (0x09, 0xff00, 0x0020)],
        200_000_000: [(0x08, 0xf000, 0x0082)],
    }

    def test_fixed_vco_clkout0_profiles(self):
        profiles = S7ClockDRP.clkout0_divider_profiles(1_200_000_000, (24, 35, 126))
        self.assertEqual(profiles, {
            50_000_000: [(0x28, 0x0000, 0xffff), (0x08, 0xf000, 0x030c), (0x09, 0xff3f, 0x0000)],
            34_285_714: [(0x28, 0x0000, 0xffff), (0x08, 0xf000, 0x0452), (0x09, 0xff3f, 0x0080)],
             9_523_810: [(0x28, 0x0000, 0xffff), (0x08, 0xf000, 0x0fff), (0x09, 0xff3f, 0x0000)],
        })
        for dividers in [(1,), (127,), (24, 24), (24.0,)]:
            with self.subTest(dividers=dividers), self.assertRaises(ValueError):
                S7ClockDRP.clkout0_divider_profiles(1_200_000_000, dividers)

    def test_drp_ownership_and_validation(self):
        clk = Signal()
        generator = S7MMCM()
        generator.expose_drp()
        with self.assertRaisesRegex(ValueError, "already connected"):
            S7ClockDRP(generator, clk, self.profiles, self.rates[0])
        generator = S7PLL()
        S7ClockDRP(generator, clk, self.profiles, self.rates[0])
        with self.assertRaisesRegex(ValueError, "already connected"):
            generator.expose_drp()
        for profiles in ({}, {1: []}, {1: [(0x80, 0, 0)]}, {1: [(0, 0, 0x10000)]}):
            with self.subTest(profiles=profiles), self.assertRaises(ValueError):
                S7ClockDRP(S7PLL(), clk, profiles, 1)

    def make_controller(self, generator, primitive):
        generator.register_clkin(Signal(), 100e6)
        output = ClockDomain("output", reset_less=True)
        generator.create_clkout(output, self.rates[0], buf=None, margin=0, with_reset=False)
        backend = S7ClockDRP(generator, output.clk, self.profiles, self.rates[0],
            drp_timeout=8, lock_timeout=32)
        dut = ClockRateControl.from_backend(backend)
        self.assertIs(dut.clk, output.clk)
        fragment = dut.get_fragment()
        fragment.clock_domains.append(ClockDomain("sys"))
        clock_cell, = [s for s in fragment.specials if isinstance(s, Instance) and s.of == primitive]
        self.assertIs(clock_cell.get_io("RST"), generator.reset)
        fragment.specials.remove(clock_cell)
        # Model the generator's reference-clock reset pipeline on the test
        # clock, with the clock primitive itself driven by the test bench.
        for cell in list(fragment.specials):
            if isinstance(cell, Instance) and cell.of == "FDCE":
                fragment.sync["sys"].append(cell.get_io("Q").eq(cell.get_io("D")))
                fragment.specials.remove(cell)
        return dut, fragment, clock_cell

    def request(self, dut, hz):
        self.assertTrue((yield dut.request.ready))
        yield dut.request.hz.eq(hz)
        yield dut.request.valid.eq(1)
        yield
        yield dut.request.valid.eq(0)

    def reply(self, dut, hz, error=0):
        for _ in range(64):
            if (yield dut.response.valid):
                break
            yield
        else:
            self.fail("missing clock response")
        for _ in range(3):
            self.assertEqual((yield dut.response.hz), hz)
            self.assertEqual((yield dut.response.error), error)
            self.assertFalse((yield dut.request.ready))
            yield
        yield dut.response.ready.eq(1)
        yield
        yield dut.response.ready.eq(0)
        yield

    def transfer(self, cell, address, write=None, data=None):
        den, drdy, do = (cell.get_io(port) for port in ("DEN", "DRDY", "DO"))
        for _ in range(32):
            if (yield den):
                break
            yield
        else:
            self.fail("missing DRP transfer")
        self.assertTrue((yield cell.get_io("RST")))
        self.assertEqual((yield cell.get_io("DADDR")), address)
        self.assertEqual((yield cell.get_io("DWE")), write is not None)
        if write is not None:
            self.assertEqual((yield cell.get_io("DI")), write)
        yield
        self.assertFalse((yield den))
        if data is not None:
            yield do.eq(data)
        yield drdy.eq(1)
        yield
        yield drdy.eq(0)

    def boot_initial_profile(self, dut, cell):
        yield cell.get_io("LOCKED").eq(0)
        yield from self.transfer(cell, 0x08, data=0xaabb)
        yield from self.transfer(cell, 0x08, write=0xa048)
        for _ in range(16):
            self.assertFalse((yield dut.backend.request.ready))
            yield
        yield cell.get_io("LOCKED").eq(1)
        for _ in range(16):
            if (yield dut.backend.request.ready):
                return
            yield
        self.fail("initial DRP profile did not settle")

    def test_mmcm_and_pll_reconfigure_and_relock(self):
        for generator, primitive in [(S7MMCM, "MMCME2_ADV"), (S7PLL, "PLLE2_ADV")]:
            with self.subTest(primitive=primitive):
                dut, fragment, cell = self.make_controller(generator(), primitive)

                def stimulus():
                    locked = cell.get_io("LOCKED")
                    yield from self.boot_initial_profile(dut, cell)
                    yield from self.request(dut, self.rates[1])
                    for _ in range(16):
                        yield
                    self.assertTrue((yield cell.get_io("RST")))
                    self.assertFalse((yield cell.get_io("DEN")))
                    yield locked.eq(0)
                    yield from self.transfer(cell, 0x08, data=0xaabb)
                    yield from self.transfer(cell, 0x08, write=0xa104)
                    yield from self.transfer(cell, 0x09, data=0x93ff)
                    yield from self.transfer(cell, 0x09, write=0x9320)
                    for _ in range(16):
                        self.assertFalse((yield dut.response.valid))
                        yield
                    self.assertFalse((yield cell.get_io("RST")))
                    yield locked.eq(1)
                    yield from self.reply(dut, self.rates[1])
                    yield from self.request(dut, 50_000_000)
                    yield from self.reply(dut, self.rates[1], error=1)

                    reset = fragment.clock_domains["sys"].rst
                    yield reset.eq(1)
                    yield
                    yield reset.eq(0)
                    yield from self.boot_initial_profile(dut, cell)
                    self.assertEqual((yield dut.active_hz), self.rates[0])

                run_simulation(fragment, stimulus())

    def test_timeout_drains_late_response_and_requires_relock(self):
        dut, fragment, cell = self.make_controller(S7MMCM(), "MMCME2_ADV")

        def stimulus():
            yield from self.boot_initial_profile(dut, cell)
            yield from self.request(dut, self.rates[2])
            yield cell.get_io("LOCKED").eq(0)
            yield from self.reply(dut, 0, error=1)
            self.assertTrue((yield cell.get_io("RST")))
            yield from self.request(dut, self.rates[0])
            yield from self.reply(dut, 0, error=1)
            self.assertFalse((yield cell.get_io("DEN")))
            yield cell.get_io("DRDY").eq(1)
            yield
            yield cell.get_io("DRDY").eq(0)
            yield

            yield from self.request(dut, self.rates[2])
            yield from self.transfer(cell, 0x08, data=0x7000)
            yield from self.transfer(cell, 0x08, write=0x7082)
            yield from self.reply(dut, 0, error=1)
            for _ in range(8):
                yield
            self.assertTrue((yield cell.get_io("RST")))

            yield from self.request(dut, self.rates[0])
            yield from self.transfer(cell, 0x08, data=0x7082)
            yield from self.transfer(cell, 0x08, write=0x7048)
            for _ in range(16):
                yield
            yield cell.get_io("LOCKED").eq(1)
            yield from self.reply(dut, self.rates[0])

        run_simulation(fragment, stimulus())


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


class TestECP5ClockMux3(unittest.TestCase):
    def test_parked_edge_switch_and_recovery(self):
        edge_ready = Signal(reset=1)
        rates = (12_500_000, 100_000_000, 208_000_000)
        settle_cycles = 8
        mux = ECP5ClockMux3(Signal(), Signal(), Signal(), rates, initial_hz=rates[2],
            bels=("bridge", "sync", "buffer", "divider"),
            edge_ready=edge_ready, settle_cycles=settle_cycles)
        fragment = mux.get_fragment()
        primitives = {s.of: s for s in fragment.specials if isinstance(s, Instance)}
        self.assertEqual(set(primitives), {"DCSC", "ECLKBRIDGECS", "ECLKSYNCB",
            "TRELLIS_ECLKBUF", "CLKDIVF"})
        for primitive in primitives.values():
            fragment.specials.remove(primitive)
        select_park = primitives["DCSC"].get_io("SEL1")
        select_edge = primitives["ECLKBRIDGECS"].get_io("SEL")
        stop_edge   = primitives["ECLKSYNCB"].get_io("STOP")
        self.assertIs(primitives["CLKDIVF"].get_io("RST"), stop_edge)
        self.assertIs(primitives["CLKDIVF"].get_io("CLKI"), mux.edge_clk)

        def request(profile, expected, valid, error, drop_lock=False):
            assert (yield mux.request.ready)
            yield mux.request.profile.eq(profile)
            yield mux.request.valid.eq(1)
            yield
            yield mux.request.valid.eq(0)
            prev_edge = (yield select_edge)
            prev_stop = (yield stop_edge)
            released = None
            for cycle in range(64):
                edge = (yield select_edge)
                stop = (yield stop_edge)
                if edge != prev_edge:
                    assert prev_stop and stop
                if stop or edge != prev_edge:
                    assert (yield select_park)
                if stop and drop_lock:
                    yield edge_ready.eq(0)
                    drop_lock = False
                if prev_stop and not stop:
                    released = cycle
                if released is not None and not (yield select_park):
                    assert cycle - released >= settle_cycles
                prev_edge, prev_stop = edge, stop
                if (yield mux.response.valid):
                    break
                yield
            else:
                raise AssertionError("missing mux response")
            for _ in range(3):
                assert (yield mux.response.valid)
                assert (yield mux.response.profile) == expected
                assert (yield mux.response.clock_valid) == valid
                assert (yield mux.response.error) == error
                assert not (yield mux.request.ready)
                yield
            yield mux.response.ready.eq(1)
            yield
            yield mux.response.ready.eq(0)
            yield

        def bench():
            for _ in range(4):
                yield
            for profile in (0, 1, 0, 2):
                yield from request(profile, profile, 1, 0)
                assert (yield select_park) == (profile == 2)
                if profile != 2:
                    assert (yield select_edge) == profile
            yield from request(3, 2, 1, 1)
            yield from request(1, 2, 0, 1, drop_lock=True)
            assert not (yield stop_edge)
            yield from request(2, 2, 0, 1)
            yield edge_ready.eq(1)
            for _ in range(4):
                yield
            yield from request(1, 1, 1, 0)

        run_simulation(fragment, bench())
