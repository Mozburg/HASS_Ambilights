# HASS Ambilights

Mirror what's on your screen onto Home Assistant lights — Ambilight style — with a
web UI for every knob, and a colour pipeline that actually matches the panel.

![settings UI](docs/ui.png)

- **3×3 zone mapping** — give each light any subset of nine screen regions (`1,4,7`
  = left column, `5` = centre only, `all` = the whole screen).
- **Colour that matches** — chromaticity is computed in linear light and sent as
  `xy_color`, so the lamp reproduces it to within du'v' 0.0001 of what you measured.
- **Average or dominant** — mirror the whole region, the most prominent *coloured*
  cluster, or a blend of the two.
- **Right-size brightness** — true luminance, perceptual CIELAB L*, or the brightest
  channel; with a gamma curve and no hard clamps.
- **Cheap** — 6.6 ms/frame at 4K (~8 % of one core), and it idles down to 4 fps when
  the screen is static.
- **Fast** — new frames go out immediately, stale ones are dropped, and each cycle's
  lights are dispatched concurrently.
- **Honest about the radio** — pacing, BUSY backoff, coordinator-stall hold-off, and a
  host-down circuit breaker.

## Install

```bash
git clone https://github.com/Mozburg/HASS_Ambilights.git
cd HASS_Ambilights
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

You need a Home Assistant **long-lived access token**: profile → *Security* →
*Long-lived access tokens*.

```bash
export HA_URL=http://192.168.1.10:8123
export HA_TOKEN=eyJ...
python screen_to_lights.py
```

Then open <http://localhost:8000/> — that's the UI. Settings saved there go to
`config.json` next to the script and are applied live.

Windows (PowerShell):

```powershell
$env:HA_URL="http://192.168.1.10:8123"; $env:HA_TOKEN="..."; python screen_to_lights.py
```

## Lights and zones

The screen is split into nine zones, numbered in reading order:

```
1 2 3
4 5 6
7 8 9
```

Set them in the UI, or in `config.json` / env:

```
LIGHT_MAP="light.left_lamp=1,4,7; light.center_lamp=2,5,8; light.right_lamp=3,6,9"
```

Lights that end up with identical colour and brightness are sent in a single service
call, so the Zigbee radio isn't asked to do the same work twice. Only the rectangle
covered by the zones in use is captured at all.

## The colour knobs

| setting | what it does |
|---|---|
| `COLOR_ALGO` | `mean` = linear average of the zones; `dominant` = the most prominent *coloured* cluster, neutral cells ignored (default) |
| `DOMINANT_MIX` | 0–1 slider between those two models, blended in u'v' (default 0.5) |
| `DOMINANT_LUMINANCE` | `cluster` = the dominant object's own brightness, `region` = the zone's mean |
| `SATURATION_BOOST` | off by default: sends the measured chromaticity untouched. On, it pushes chroma away from white as the frame brightens, capped and gamut-clamped |
| `BRIGHTNESS_FROM` | `luminance` (physical), `lstar` (perceptual, default), `value` (brightest channel — hue has no effect) |
| `BRIGHTNESS_GAMMA` | tone curve on the brightness input |
| `SEND_MODE` | `xy` (default, precise) or legacy `rgb` |

Rule of thumb: a white page holding one vivid photo is a pale wash under `mean` and
the photo's colour under `dominant`. `DOMINANT_MIX` decides how far you travel, and
`DOMINANT_LUMINANCE` decides whether the room follows that object's brightness or
keeps the zone's overall level.

## Endpoints

| path | purpose |
|---|---|
| `GET /` | settings UI |
| `GET /api/config` | current settings + metadata + light list |
| `POST /api/config` | validate, apply live, persist (`{"settings":{...}}`, or `{"reset":true}`) |
| `GET /health` | live stats: posts, frame age, cycle time, pace, busy/stalls/timeouts, per-light counters |
| `GET /regions` | the nine zone colours for the current frame |
| `GET /raw_grid` | the 64×36 sample grid |

`python screen_to_lights.py --calibrate` pushes twelve test colours to one light,
measures the chromaticity it reports back (expect du'v' ≈ 0.0001 on a healthy
installation), and restores the previous state.

## Notes from real hardware

These cost the most time, so they're documented up front:

- **Send `xy_color`.** HA converts `rgb_color` with the Philips-Hue wide-gamut matrix
  and clips — measured drift up to du'v' 0.09, where 0.003 is one just-noticeable step.
- **Average in linear light**, not on gamma-encoded bytes: half white + half black is
  188, not 128.
- **Never call `mss`'s `shot.rgb`** — it's a 24 MB strided copy (44 ms/frame at 4K).
  Read the BGRA buffer as a numpy view and let PIL decode `BGRX`.
- **The device ack, not HTTP, is the latency.** Transport is ~4 ms; a single Zigbee
  spotlight takes ~121 ms per command while a group takes ~37 ms on a test ZHA mesh.
  Dispatch a cycle
  concurrently, and don't wait on each backoff.
- **A read timeout is not "HA down".** If the coordinator's serial link resets
  (`bellows … ERROR_EXCEEDED_MAXIMUM_ACK_TIMEOUT_COUNT` → `ControllerError:
  ApplicationController is not running`), commands hang while HA is perfectly
  healthy. The script treats that as a radio stall (drop the frame, back off,
  hold two seconds), not as an outage.
- **A `color_temp` change sent with `transition > 0` can be silently dropped** by a ZHA
  group, so restores are sent with `transition: 0` and verified.

## Development

```bash
python tests/test_spotlight_sync.py      # 13 phases, fake screen + mock HA, no display needed
```

The test suite covers the colour maths (white point, sRGB primaries, linear averaging),
zone splitting, per-light dedupe and rescue, stale-frame dropping, BUSY handling,
concurrent dispatch, the circuit breaker, capture cropping, and dominant mode on
letterboxed frames.

`tools/live_probe.py` sends a synthetic frame through the real sender and prints post
rate, cycle time and per-light counters — useful when tuning against actual hardware.

## Licence

MIT.
