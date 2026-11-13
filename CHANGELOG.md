# Changelog

All notable changes to HASS Ambilights. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — the version lives in
`hass_ambilights.py` as `__version__` and is reported by `GET /health`, `GET /api/config`
and `hass_ambilights.py --version`.

## [Unreleased]

## [2.12.0] — 2026-11-12

### Added
- **A calibration session walks every mapped light.** Calibration is per light — the lamps
  differ, and each is soloed while it is matched — but the panel used to calibrate one and
  close, leaving you to remember which was done. It now queues all mapped lights, says
  `light 2 of 3`, and moves to the next when you finish one. Each light starts from its own
  stored offsets rather than carrying the previous lamp's across.
- The **quick pass covers every light once (4 frames each), then the refinement pass does
  the same circuit** with dark and light frames, rather than refining the first lamp before
  the second has been looked at.
- Every light's original state is saved when the session starts and restored when it ends,
  including the one being calibrated, so finishing puts the room back rather than leaving
  the others off.

### Fixed
- **Dark, mid and light sent the same brightness for colour frames.** Brightness was derived
  from the frame's own luminance with a floor larger than a dim pure colour's luminance, so
  red-dark and red-mid were identical and every blue step matched. The level now drives
  brightness directly: `dark 125 · mid 198 · light 247` for red, green and blue alike, and
  the brightness slider still scales the whole ladder.

## [2.11.1] — 2026-11-12

### Fixed
- **Changing the monitor restarted the program instead of switching to it.** The re-exec
  came up on its default port with none of the saved configuration, still watching the
  previous screen. `MONITOR_INDEX` now applies in place: the capture loop notices the
  change on its next frame, re-resolves the monitor and rebuilds the geometry.
- **Saving could delete a user's settings.** The file was written from the keys this build
  knows, so anything else — a key from an older release, or one written by a newer one —
  disappeared on the first save. Unrecognised keys are now preserved verbatim, and
  translated legacy keys are announced in the log and retired on the next save so a config
  converges on the current schema instead of carrying both indefinitely.
- **Kelvin ranges were guessed at.** A light that reported `color_temp` without a range was
  treated as 2000-6500K, so it could be asked for whites it cannot make. The range now comes
  from the light (`min/max_color_temp_kelvin`, with legacy mireds understood); without one,
  the light stays on `xy`.
- **The calibration white step sent `xy` while the pipeline sent kelvin**, so the white you
  tuned was not the white that ran. It now sends through the same channel and reports the
  kelvin it used.
- **`warmth` moved in opposite directions on its two paths**: warmer on `xy`, cooler on the
  kelvin channel. It now means warmer on both.

### Changed
- `MONITOR_INDEX` no longer requires a restart, and the settings page says so.

## [2.11.0] — 2026-11-12

### Added
- **Neutral frames go out as `color_temp_kelvin`.** A bulb's white is usually a dedicated
  warm/cool LED pair; asking for white in `xy` often engages RGB mixing, which is dimmer and
  less accurate. Coloured frames stay on `xy`, and a frame that used to go out as
  `xy_color` now goes out as `color_temp_kelvin`, clamped to the range the light reports.
- **`WHITE_MODE`** (`auto` | `color_temp` | `xy`, default `auto`) in Advanced → Sending.
- `GET /api/lights` reports `white_channel` and the kelvin range per light; `/health`
  counts `cct_posts`.

### Changed
- **Warmth is now a white-only correction**, applied as a kelvin shift on the white
  channel. It no longer tints coloured frames — colour is corrected by hue and saturation
  in xy, white by temperature on the bulbs' own LEDs.
- "Neutral" is decided by distance from the Planckian locus (duv), not by distance from
  D65: a 2700 K white is nowhere near D65 and is exactly what the kelvin channel is for.

### Fixed
- The mode is decided per measured frame, not per send: on a static white screen dedupe
  means few sends, and the state would never have advanced off `xy`.
- A mode change now counts as a payload change, so the switch is actually sent instead of
  being deduped away.
- Colour frames can never be expressed as a colour temperature, hysteresis or not — an
  earlier draft would have sent a red frame as 1480 K.

## [2.10.2] — 2026-11-12

### Fixed
- **The brightness slider did nothing on colour frames.** The send was hard-wired to
  `level_scale: 1.0` unless the frame was white, so only the white step responded.
  Verified in the UI: 89 → 121 brightness on the red step, and the slider's range widened
  to 0.3–2.5.
- **Positive hue did nothing on a saturated primary.** `calibrate_xy` rotated the
  chromaticity and, when the result fell outside sRGB, returned the *original colour* —
  so +5° looked exactly as dead as +45°. A primary sits on the gamut corner, so positive
  rotation always overflowed. The correction now keeps the requested hue and takes the
  most chroma that hue can hold (binary search to the gamut boundary), so the slider
  travels properly in both directions: on red, +25° sends `#ff9300`, −25° sends `#ff008c`.
- **The colour swatch rendered at the sent level**, so a fully saturated red read
  `#e00000` at brightness 40 and `#ff0000` at 255 — the right colour at a lower level,
  which looks like the wrong colour. The swatch now shows the colour and the brightness
  is its own number, which is how it was always being sent (chromaticity + level, never
  an RGB triple).

### Changed
- Calibration frames are true primaries (`#ff0000`, `#00ff00`, `#0000ff`), dimmed by the
  level and nothing else — the reference has to be the colour you are calibrating against.
  At a full primary the saturation slider has little headroom by definition, because the
  gamut stops there; that is the gamut, not a broken control.

## [2.10.1] — 2026-11-12

### Fixed
- **Warm/cool did nothing.** It was implemented as a chroma scale, and a white frame sits
  *exactly* on D65 — scaling a distance of zero moves the point nowhere, so the slider was
  inert by construction. Warmth is now its own term: a displacement along the Planckian
  locus (a full slider is about 1300K), stored as `warmth` per light, applied to every
  frame — a lamp's white-point bias is not limited to white. Existing `calibration.json`
  files load with `warmth: 0` and change nothing.
- **The slider sent every step it passed through.** A drag fired one POST per `input`
  event, so the light chased a dozen intermediate colours before settling. Sends are now
  debounced (220 ms, trailing edge) and coalesced: if one is in flight the newest value
  waits and goes next, so the final position always wins. Measured: 12 input events in a
  burst produced **one** POST carrying the last value. Step changes still send at once.

### Changed
- The white frame shows **Warmth (− cool, + warm)** instead of a saturation slider that
  could not affect it, and the label says which way it goes.

## [2.10.0] — 2026-11-12

### Added
- **Manual calibration: you are the sensor.** The Calibrate button now opens a panel
  instead of running a sweep. It takes one light at a time, switches the others off,
  shows an exact reference patch on screen and sets that light to the same colour, then
  gives you **hue**, **saturation** and **brightness** sliders until the two match. What
  you accept is stored per light in `calibration.json` and applied to every frame that
  light is sent afterwards.
- **4 → 10 → 12 frames.** A quick pass is white plus red/green/blue at one level (4). If
  you keep going it refines itself with dark and light for each colour (10). **Full
  calibration (12)** also gives white three levels, which is what pins down the level
  curve. White only offers brightness and warm/cool — no hue control fixes a colour
  temperature.
- **A Calibration card.** The button, the current offsets per light with a **Clear** on
  each, and `SATURATION_BOOST`, `BRIGHTNESS_FROM`, `BRIGHTNESS_GAMMA` under **Advanced** —
  the numeric half of the same job, in one place.
- `GET`/`POST /api/calibration` (`start`, `apply`, `commit`, `clear`, `end`).

### Changed
- **`SEND_BRIGHTNESS` moved into the Colour card, directly above "Brightness follows
  colour"** — the thing it gates. The old Brightness card had nothing left and is gone.
- `SATURATION_BOOST`, `BRIGHTNESS_FROM` and `BRIGHTNESS_GAMMA` are now listed under the
  Calibration card rather than Colour and Brightness.

### Fixed
- Lights that could not be switched off during a calibration session no longer abort it;
  they are logged and counted, and restore still runs for the rest.
- Removed the dead automatic-probe UI path now that the panel replaces it.

### Upgrading
Take this if you were on 2.9.3 or 2.9.4 for the save fix alone, but the feature here is
the manual calibration. Existing setups need no action: no calibration is stored until
you run one, and `--calibrate` behaves exactly as before.

## [2.9.5] — 2026-11-12

### Fixed
- **Save was broken in 2.9.3 and 2.9.4: `visible is not defined`.** Building the save
  payload from "only the settings the page shows" introduced a reference to `visible`,
  which was declared inside `applyLive()` and not in `save()` — so every Save threw
  before it sent anything. The set is now one `visibleNames()` helper used by both paths,
  and the suite asserts that `save()` builds its payload from the helper and never reaches
  for a local that belongs to another function.
- The suite also checks the outcome, not just the request: the server must report
  `persisted: true` and the value must come back in `saved_settings` with `unsaved: false`.

## [2.9.4] — 2026-11-11

### Changed
- **The test suite runs in sections.** It was one 1,800-line script of top-level asserts:
  the first failure aborted everything after it, a test runner collected nothing, and every
  failure looked the same from outside. The body is now `tests/suite_body.py`, split at its
  existing `# --- name ---` markers and executed section by section in one shared namespace
  — the shared state is deliberate (one fake Home Assistant, one fake screen), so sections
  still run in order, but a failure names its section and the rest still run.

  ```
  ✖ mean vs dominant: AssertionError: deliberate failure in the colour section
  61 passed, 2 failed in 63.8s
  ```

  `python tests/test_hass_ambilights.py` behaves as before (exit code, `ALL CHECKS PASSED`),
  and now takes `--list`, `-k <name>` and `--failfast`. If the setup section fails the
  runner says so once instead of drowning the real cause in `NameError`s. `pytest tests`
  collects 62 per-section tests in file order, and CI runs both entry points.

## [2.9.3] — 2026-11-11

### Security
- **Test connection is no longer a LAN port scanner.** `/api/test-connection` fetched any
  URL it was given, so with `BIND_HOST=0.0.0.0` anyone who could reach the settings page
  could probe arbitrary hosts through it. It now refuses targets that do not resolve to a
  private, loopback or link-local address — unless the host is the one already configured —
  and only accepts `http(s)`.
- **Credentials cannot ride in `HA_URL`.** A URL with `user:password@` is rejected on save,
  and error text from `requests` (which echoes the whole URL) is redacted before it is
  returned or logged.

### Fixed
- **A refresh no longer steals the caret.** Every settings refresh rebuilt the controls, so
  a half-typed value lost focus and selection. Focus is captured by row and position and
  restored afterwards — verified by focusing the pacing field, forcing a refresh, and
  checking the same field still has it.

## [2.9.2] — 2026-11-11

Wave four: the last of the audit's user-visible items.

### Fixed
- **A save no longer rewrites a light mapping you did not touch.** The page re-serialised
  `LIGHT_MAP` from its own editor every time, so saving an unrelated setting reformatted
  the file (spacing, zone order) and could renumber a hand-written map. The mapping is only
  rebuilt when the lights editor itself changed.
- **The page adopts the values the server stored.** `applyLive` discarded the `settings`
  block in the response, so a field could disagree with the running app until the next
  reload — a trailing slash on `HA_URL`, for instance.
- **`config.example.json` was stale**: it still listed only the legacy colour keys, so
  copying it produced a config whose colour model was silently ignored. It is now generated
  from the settings table (38 keys), and a test fails if it falls behind.
- **The systemd unit can actually work**: it sets `User=`, exports `DISPLAY=:0` for the
  capture thread, runs `--server --no-browser` (it used to open a browser from a service),
  reads credentials from a 0600 `EnvironmentFile` instead of baking a placeholder token into
  a world-readable file, drops the `Environment=BIND_HOST` that `config.json` silently
  overrode, and has `StartLimitBurst` so a port clash cannot restart forever.

## [2.9.1] — 2026-11-11

Wave three of the audit: the failure modes that made a broken install look healthy.

### Fixed
- **One bad entity id no longer pauses every light.** Any non-BUSY 4xx fed the *host*
  circuit breaker, so a single misconfigured `entity_id` opened a 10 s global pause every
  10 s while the recovery probe answered 200. Only 5xx and connection failures open the
  breaker now; a 4xx is counted as `refused` and logged once.
- **The sender restarts after an exception** instead of ending the thread (the incident
  that froze the lights with `/health` still at 200). It backs off 0.5 s and counts
  `sender_restarts`.
- **The capture prologue is guarded.** A monitor list that did not match `MONITOR_INDEX`
  killed capture silently; it now logs, counts `capture_errors`, closes the grabber and
  stops cleanly.
- **Concurrent sends keep their connections.** Threads were created per request, so the
  thread-local `requests.Session` — and its keep-alive — died with them. A persistent pool
  keeps the workers, and a worker exception can no longer be swallowed.
- **`BIND_HOST` must be bindable.** Saving an address this machine cannot listen on used to
  succeed, then kill the restart and leave the settings page unreachable. IPv6 literals are
  bracketed in URLs too, which they never were.
- **The restart guard clears as soon as the app serves**, so a legitimate second change
  inside 30 s is no longer refused as a suspected crash loop.
- **UI: clearing a light's zones saved as "all"** — the exact opposite — and is now dropped;
  the calibrate poll gives up after five missed replies instead of spinning forever with
  the button disabled; a page load no longer fires a live POST; and saving no longer
  writes the hidden legacy keys back into `config.json`.
- **`build.ps1` fails when a step fails.** Windows PowerShell 5.1 ignores
  `$ErrorActionPreference` for native commands, so a failed build still printed the green
  "Built dist\HassAmbilights.exe" line.
- **The Windows CI job now runs the test suite**, so `os.name == "nt"` branches are
  exercised at all.

## [2.9.0] — 2026-11-11

The colour path, second pass. The first fix below is a correction: 2.7.1 claimed to fix
"mean colour + dominant brightness" and did not — the value was computed and then dropped
by the consumer.

### Fixed
- **The brightness blend reaches the sender.** `dominant_luminance` was applied inside
  `if "dominant" in colour:`, which is exactly the branch that does not exist when the
  colour is mean and only the brightness blend wants the cluster. Measured before/after on
  a half-dark frame: brightness 126 → 191.
- **The 3×3 panel previews what the lights get.** The grid was drawn from the un-blended
  aggregate while the light for that zone received the blended dominant — measured ΔE 36
  between the two chips at the default blend. One `_apply_dominant()` now feeds both.
- **Saturated colours are previewed, not washed out.** The chip solved the chromaticity
  *at* `brightness/255`, which clips any channel that needs more than 1.0 in linear light:
  orange previewed as `#ffc03a`, red as `#ff4f4f`. It is now rendered at full chrome and
  scaled afterwards — `#e87f23` and `#e82525` — and the rgb payload uses the same path, so
  `rgb_color` no longer sends pink where `xy_color` sends red.
- **A black frame asks for black.** With brightness off the rgb payload fell back to
  `[255, 255, 255]`, i.e. a black screen commanded full white.

### Changed
- **The colour stage is ~4× cheaper.** One `aggregate()` per frame instead of 2 + N (a
  full-grid light reused the same value it had just computed), and the 3×3 panel is only
  built while something is actually polling `/regions` — it exists for the settings page,
  so nine aggregates per frame were pure waste. Measured 6.25 ms → ~1.6 ms per frame.

## [2.8.0] — 2026-11-11

Five read-only auditors went over disjoint slices of this file; every finding was
reproduced here before anything changed. The fixes below are the ones that lost data or
lied about it. The rest — preview clipping, the un-blended grid preview, the circuit
breaker, thread recovery, keep-alive, capture copies, packaging and test coverage — are
recorded with evidence in [docs/audit-2026-11-11.md](docs/audit-2026-11-11.md).

### Fixed
- **A plain save wiped every setting.** `{"persist": true}` reset the whole config,
  light map included, because one loop variable held both the `reset` and `persist` flags.
  Reported by an auditor with the exact request, reproduced, then fixed.
- **Types are checked, not repaired.** `{"MONITOR_INDEX": 2.6}` silently became monitor 3,
  and `{"HA_URL": null}` blanked the URL (every later call then failed). Ints must be
  integral; strings must not be null, lists or objects.
- **A failed config write is no longer reported as saved.** The API said
  `persisted: true` for a write that never happened.
- **The legacy colour keys really do migrate now.** `_translate_legacy_colour` existed
  since 2.7.0 but nothing called it, so `COLOR_ALGO=mean` never reached `COLOUR_BLEND` —
  the migration this file claimed to ship. Two auditors found it independently.
- **Two saves can no longer lose one.** `apply_settings` and the file write are under one
  re-entrant lock.
- **A frame is only recorded as attempted if it is actually queued.** Frames were stamped
  before the auto-tune gate, so every frame during a tune suppressed its colour for three
  seconds afterwards; the sender also re-read shared state to decide what it had sent, and
  treated a BUSY frame as confirmed. Refused frames are now retried immediately.
- **A failed light restore no longer strands the others** on the test colours.
- **Startup stops lying to systemd**: another instance in `--server` mode exits 3 instead
  of 0, and the restart-guard timer is a daemon thread (it used to keep the process alive
  for 31 s after an early return).
- **Windowed builds log properly**: `basicConfig()` ran before the stream redirect, so
  every record wrote a "Logging error" traceback instead of the message.

### Added
- `/health` reports `capture_beat`, `sender_beat` and `pipeline_ok`, so a dead thread is
  visible instead of answering 200 forever, and `restart_failed` says why a restart did
  not happen.
- The test suite runs in a temp directory: it used to rewrite the repository's own
  `config.json`.

## [2.7.1] — 2026-11-11

### Fixed
- **Mean colour with dominant brightness did nothing.** The dominant cluster was computed
  only when the *colour* blend was above 0, so turning on *Brightness follows colour* off
  and dragging the brightness slider to 1 (keeping the colour at mean) silently stayed at
  the zone mean — the combination the new controls invite. The cluster is now computed when
  either slider asks for it, found by an audit subagent's measurement rather than by the
  suite, which had no test for it.

## [2.7.0] — 2026-11-11

### Changed
- **The colour model is one slider.** `COLOR_ALGO` (an enum) plus `DOMINANT_MIX` (a
  second slider that only did anything in one of its two states) were two controls for one
  decision. They are now `COLOUR_BLEND`: **0 = mean, 1 = dominant**, 0.5 by default, with
  both ends labelled under the slider and a live readout of the mix. Old keys are still
  accepted — a config with `COLOR_ALGO=mean` loads as 0, `dominant` as 1 — and are hidden
  from the page rather than deleted.
- **Brightness blend with a follow flag.** `DOMINANT_LUMINANCE` (cluster/region) became
  `BRIGHTNESS_BLEND` (0–1) plus `BRIGHTNESS_FOLLOW_COLOUR`, on by default so brightness
  tracks the colour slider; turn it off to pick a brightness mix independently.
- **Capture rate moved into Advanced** in the Capture card. Auto tune sets it, so it is a
  read-it-when-curious knob, not an everyday one.
- The dominant-only knobs (`HUE_BINS`, `CHROMA_FLOOR`) now appear whenever
  `COLOUR_BLEND` is above 0, instead of keying off the old enum.

### Fixed
- A `ReferenceError: paintEnds is not defined` in the new slider legend: the painter was
  declared inside the `if` block that built the legend, so the input handlers (outside that
  block) could not see it. The legend now tracks the slider, verified in a browser.

## [2.6.0] — 2026-11-11

### Added
- **The light picker is grouped by Home Assistant area.** Each area is an `<optgroup>`
  with its light count, lights with no area come last without a heading, and the row shows
  how many lights and areas were found. Areas come from one `/api/template` render
  (`area_name()`), because the REST API exposes no area registry — and it resolves device
  areas as well as entity ones. If that call is refused the picker still lists every
  light, just ungrouped.

### Changed
- Within an area, colour-capable lights still sort first; across areas, the order is
  alphabetical so a room's lights stay together.

## [2.5.0] — 2026-11-11

### Added
- **Settings that need a restart now restart the app.** Saving `MONITOR_INDEX`,
  `BIND_HOST` or `BIND_PORT` used to leave a "restart required" tag and nothing else, so
  the setting quietly did nothing until you closed and reopened the exe. The process now
  re-execs itself with the same arguments once the save is confirmed, and the settings
  page follows it — to the new port, if the port is what changed.
- The restart refuses to fire twice within 30 seconds. A wrong `BIND_HOST` that stops the
  app starting would otherwise turn into a restart loop you have to kill from Task
  Manager; the guard file records the attempt and the log says what to fix.

### Fixed
- **Auto tune's chosen pacing rendered as invalid in its own field.** It writes three
  decimals (`0.057`) while the Fastest pacing input stepped in `0.01`, so the number it
  had just chosen failed the browser's own step validation. Floor now steps by `0.001`,
  the ceiling by `0.01`, and capture/idle rates by `0.1`.
- The Auto tune result names the running version, so a stale build is obvious where the
  number appears rather than only in the footer.

## [2.4.0] — 2026-11-11

### Changed
- **Auto tune no longer stops at 0.1 s.** The tuner had its own floor of 100 ms, so a
  controller that would happily take 40 ms was still paced at 100 ms. The clamp is now
  20 ms (a sanity limit, not a design one) and the probe ladder runs down to 25 ms, so the
  pacing reflects what the radio actually accepts.
- **The capture rate follows the measurement.** One frame is one command per light, so the
  pacing the radio accepted *is* a frame rate: it now sets `CAPTURE_FPS` (and lowers
  `IDLE_FPS` if needed) instead of leaving capture at 12 fps feeding a radio that could
  take 20 — or capturing 12 fps that a 4 fps radio throws away. Capped at 30 fps, because
  capture, colour work and the UI share one core.
- `POST_INTERVAL_FLOOR` accepts down to 0.02 s (was 0.05), so it and the tuner agree on
  what "fast" means.

### Fixed
- **Auto tune measured itself.** Capture kept posting frames during the probe, so the
  first round could collide with the app's own traffic and the run failed with "even the
  slowest probe was rejected" against a healthy radio. The capture loop now stays off the
  radio while the tuner owns it, and `/health` reports `tuning`.

## [2.3.0] — 2026-11-11

### Added
- **A splash screen while the exe boots.** PyInstaller's splash window shows the name,
  the four-dot mark and the version while the archive unpacks and the settings server
  comes up, then disappears the moment there is a window to look at — and it is closed on
  a failed start too, so it can never be left behind. The image is generated from
  `__version__` at build time (`packaging/make_splash.py`), not committed.

### Changed
- **The project is called HASS Ambilights everywhere it is read by a person**: window
  title, tray tooltip, settings page, log lines, splash, and the Windows version
  resource. `FileDescription` is now `HASS Ambilights`, which is what Task Manager shows
  in its Name column — previously it read "Mirror the screen onto Home Assistant lights".
  The executable is still `HassAmbilights.exe` and settings still live in
  `%APPDATA%\HassAmbilights`, so nothing moves for existing installs.

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
