# Runtime Clock Rates

The target chooses the clock backend, reference clock, and supported
profiles. The peripheral receives the generated clock and asks for rates
in Hz. For example, video modesetting can request a pixel clock while
LiteSDCard uses a separate instance for its PHY clock.

Use `ClockRateControl` and the backend's control endpoints in an
always-running management domain. The clock being changed can stop during
a request. The client drains and parks its interface before submitting a
request. It updates rate-dependent settings and requalifies the peripheral
before resuming traffic.

## Binding a Target Backend

A backend exposes `clk`, `rates`, `initial_hz`, and two stream endpoints.
The request contains `profile`. The response contains `profile`,
`clock_valid`, and `error`. Profile numbers are indices into `rates`, with
a width of `max(1, (len(rates) - 1).bit_length())`.

The target constructs the backend with its reference clock and
vendor-specific options, then binds it to the controller. The controller
owns the backend submodule.

```python
from litex.soc.cores.clock import ClockRateControl

# The target has already constructed pixel_backend with its clock source
# and the configurations qualified for this board.
self.pixel_clock = ClockRateControl.from_backend(pixel_backend)
self.comb += self.cd_video.clk.eq(self.pixel_clock.clk)

# The modesetter runs in the management domain and parks video before a switch.
self.comb += [
    video_modesetter.clock_request.connect(self.pixel_clock.request),
    self.pixel_clock.response.connect(video_modesetter.clock_response),
]
```

For a dedicated clock mux, the profile selects a preconfigured source.
For a PLL or MMCM with runtime reconfiguration, the profile selects
the vendor register settings. The backend confirms the clock is usable
before replying and handles reset, locking, and timeouts. Constrain every
clock profile in the target and ensure reset establishes the initial rate.

Give each independent consumer its own clock controller. If consumers
share a PLL whose outputs change together, the target must coordinate
quiescing those consumers and applying a common profile.

## Peripheral Interface

The client submits the requested `hz` through `request`. Unsupported
rates return an error without a backend request. Only one request is in
flight, and the response stays valid until the client accepts it. The
`response` gives the active
`hz` and an `error` flag. An error can report a surviving clock,
such as the previous rate after rollback. Zero Hz indicates that no clock
is usable. `active_hz` starts at `initial_hz` and tracks the last reported
rate. A target that needs continuous fault detection must monitor its clock
sources separately.

LiteSDCard can use the same interface to request its internal PHY clock.

```python
self.phy_clock = ClockRateControl.from_backend(sd_backend)
self.comb += [
    self.cd_sdcard.clk.eq(self.phy_clock.clk),
    sd_clock.pll_request.connect(self.phy_clock.request),
    self.phy_clock.response.connect(sd_clock.pll_response),
]
```

## ECP5 Dedicated Clock Select

`ECP5ClockMux` takes two preconfigured clock sources and their rates in
input order. The target may also provide a ready signal for each source.
The backend synchronizes these signals into the management domain. It uses
the ECP5 `DCSC` in falling-edge, glitchless mode. Both sources must keep
running during a switch. A successful response follows the settling
interval with both sources ready. Set `settle_cycles` in management-clock
cycles to cover several periods of the slower source and readiness
synchronization. A loss of readiness during a switch invalidates the
output until a new switch settles with both sources ready. Constrain the
output for the fastest source.
