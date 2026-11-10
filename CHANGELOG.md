# Changelog

All notable changes to Screen → Lights. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — the version lives in
`screen_to_lights.py` as `__version__` and is reported by `GET /health`, `GET /api/config`
and `screen_to_lights.py --version`.

## [Unreleased]

## [1.0.0] — 2026-11-10

First release: one file that mirrors a screen onto Home Assistant lights.

### Added
- **3×3 zone mapping** — each light takes any subset of nine screen regions (reading
  order), with only the rectangle those zones cover actually captured.
- **Colour that matches** — chromaticity computed in linear light and sent as `xy_color`,
  which lands within du'v' 0.0001 of the measured colour on real hardware; `rgb_color`
  remains available.
- **Dominant or average colour** — chroma-weighted hue clustering with neutral cells
  ignored, blended with the mean at a chosen ratio.
- **Brightness models** — physical luminance, perceptual L\*, or brightest channel, with a
  gamma curve and no hard clamps.
- **Settings UI** — a single page served by the app: every knob, a light picker that reads
  the Home Assistant instance, a monitor picker that reads the machine, live 3×3 zone
  preview showing the payload colour at the brightness actually sent, live per-light
  counters, and in-browser calibration.
- **Live edits, explicit saves** — changes reach the running process in about a third of a
  second; `config.json` is only written when you press Save, and the badge tells you when
  what is running differs from the file.
- **Credentials in the UI** — URL and long-lived token entered in the browser; the token
  goes to `secrets.json` (mode 600), is never returned to the page, and is probed on save.
- **Radio-aware sending** — busy-queue backoff, coordinator-stall hold-off, host circuit
  breaker, newest-frame-wins mailbox, merged payloads for identical lights, concurrent
  dispatch, and pacing tuned to what a Zigbee mesh accepts.
- **`--calibrate`** — twelve measured colours, reports worst du'v' and dE76, restores the
  light afterwards.
- **One-file install** — running `screen_to_lights.py` creates its own `.venv`, installs
  its dependencies and serves the UI; the settings page is embedded in the file.

### Fixed before first release
- A JSON `null` could switch the dry-run safety flag off; out-of-range and non-finite
  values were silently converted instead of rejected.
- A light mapped across two columns of a ragged 3×3 split crashed every frame.
- Turning brightness sending off raised on every frame and froze the lights.
- Saturation boost could leave the CIE xy domain, which Home Assistant rejects, or turn a
  saturated colour black in `rgb` mode.
- A malformed transition value could kill the sender thread while the UI still reported
  healthy.
