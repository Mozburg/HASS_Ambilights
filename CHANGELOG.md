# Changelog

All notable changes to Hass Ambilights. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — the version lives in
`hass_ambilights.py` as `__version__` and is reported by `GET /health`, `GET /api/config`
and `hass_ambilights.py --version`.

## [Unreleased]

## [2.2.2] — 2026-11-11

### Fixed
- **Tray → Quit now actually exits the process.** pywebview closes a window through
  the same `FormClosing` event that *Close to tray* vetoes, so `destroy()` hid the
  window again and left `HassAmbilights.exe` running with no window and no tray icon —
  only Task Manager could end it. Quit now releases the veto, stops the tray icon and
  the embedded server, and arms a short backstop that exits the process if a GUI
  backend refuses to unwind. The `--tray`-only mode gets the same backstop.
- **The pointer no longer flickers to the top-left corner while capturing.** mss's
  BitBlt always sets `CAPTUREBLT`, which makes the hardware cursor blink and jump in
  step with the capture loop on many Windows drivers (python-mss issue #179). The flag
  is cleared before the first grab, so the cursor stays put; layered overlays may no
  longer show up in the mirror.

## [2.2.1] — 2026-11-10

### Changed
- **Auto tune probes every configured light**, not just the first. Each round sends a
  command to all of them (concurrently, exactly as the sender does) and only counts as
  clean if every light answered, so the pacing it picks is the one your whole setup can
  keep up with. All lights are restored afterwards, and the result says how many were
  measured: `✔ 0.23–0.92 s · 3 lights (rejected at 150 ms)`.
- The Auto tune button sits at the top of the Sending card, above the pacing knobs it
  changes, and its confirmation names the lights it is about to flash.

## [2.2.0] — 2026-11-10

### Added
- **Close to tray.** Closing the window hides it and keeps mirroring; the tray icon
  reopens it (*Open settings*) or quits for real. On by default in the desktop app, with a
  *Close to tray* switch in the Server card for anyone who wants closing to mean quitting.
  Without a tray backend the close goes through as before.
- **Zone grids beyond 3×3.** `ZONE_ROWS` / `ZONE_COLS` (Capture card, up to 8×8) split the
  screen into any number of zones. Zone buttons, the live strip and `/regions` all follow
  the configured grid, and zone numbers still read left to right, top to bottom.
  Shrinking the grid is refused — with a reason — while a light is mapped to a zone the
  smaller grid would not have.

## [2.1.1] — 2026-11-10

### Removed
- The banner announcing that `config.json` overrides environment settings. It fired on
  normal use (any saved file differs from the environment), named settings the UI cannot
  change, and its advice — delete the file or reset everything — was worse than the
  problem. The precedence is documented instead, in the README and in the help for the
  settings it affects.

## [2.1.0] — 2026-11-10

### Added
- **Auto tune** in the Sending card (and `--autotune`): sends a short burst at
  decreasing intervals, finds the fastest pacing the radio accepts without
  rejecting commands, and applies `POST_INTERVAL_FLOOR`/`CEILING` live. The light is
  put back afterwards, and the result says where the queue gave up.

### Fixed
- The live panel and the 3×3 strip rebuilt their DOM on every poll, which made the
  mouse cursor flicker over the page (worst in the WebView2 window). They now update
  in place and only write values that changed.
- `restore_light` only recognised xy and colour-temperature read-backs, so an
  `rgb_color` restore was reported as failed and retried three times.
- Auto tune (and any live edit) triggered the "config.json is overriding environment
  settings" banner, which is only true for values actually saved to the file.
- The new screenshot shows the current UI.

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
