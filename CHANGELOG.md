# Changelog

All notable changes to Hass Ambilights. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — the version lives in
`hass_ambilights.py` as `__version__` and is reported by `GET /health`, `GET /api/config`
and `hass_ambilights.py --version`.

## [Unreleased]

## [2.0.0] — 2026-11-10

Renamed to **Hass Ambilights**, and shipped as a Windows desktop app.

### Changed (breaking)
- The project, entry file and executable are now **Hass Ambilights**:
  `screen_to_lights.py` → `hass_ambilights.py`, `ScreenToLights.exe` →
  `HassAmbilights.exe`, and the settings UI, window title and tray all use the new name.
- Packaged builds keep config in `%APPDATA%\HassAmbilights` (was `ScreenToLights`).
  Running the script still keeps `config.json` beside the file, so a script install only
  needs the new file name.

## [1.1.0] — 2026-11-10

Windows desktop build. The colour pipeline, server and settings page are unchanged; what
changes is how the app starts and where it lives.

### Added
- **`--window`** — the settings page in a native window (pywebview/WebView2); closing the
  window quits. A double-clicked exe runs this by default.
- **`--tray`** — tray icon with *Open settings* and *Quit*, for running at login.
- **Start with Windows** — opt-in setting that adds/removes the login entry and applies
  immediately.
- **Per-monitor DPI awareness** on Windows, so the captured rectangle and the monitor
  picker agree with Windows on mixed-DPI desktops.
- **Single instance** — launching again focuses the running app instead of fighting for
  the port; a busy port picks the next free one (GUI modes only).
- **`START_WITH_WINDOWS`** setting, and `?version`-carrying health checks used for the
  instance probe.

### Changed
- Packaged builds keep `config.json` and `secrets.json` in `%APPDATA%\HassAmbilights` and
  a rotating log in `%LOCALAPPDATA%\HassAmbilights\logs`. Running the script still keeps
  everything beside the file.

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
- **One-file install** — running `hass_ambilights.py` creates its own `.venv`, installs
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
