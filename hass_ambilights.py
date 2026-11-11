#!/usr/bin/env python3
"""HASS Ambilights - mirror chosen parts of the screen onto Home Assistant lights.

Zones: the screen is split into a 3x3 grid, numbered in reading order

     1 2 3
     4 5 6
     7 8 9

Each light gets its own zone list (1 to all 9), e.g.

     LIGHT_MAP="light.left_lamp=1,4,7; light.right_lamp=3,6,9"
     LIGHT_MAP="light.desk=all"              # everything
     LIGHT_MAP="light.a=5; light.b=1-3"      # centre only / whole top row

Lights that resolve to identical colour+brightness are sent in ONE service call
with an entity_id list, so the Zigbee radio isn't asked to do the same work twice.

Colour accuracy: chromaticity (xy) is sent directly, computed from a LINEAR-light
average with the sRGB D65 matrix - on a physical lamp the round trip lands within
about du'v' 0.0001 (0.003 ~= one just-noticeable step). Sending rgb_color instead
lets HA apply its Hue-wide-gamut matrix and drift by up to du'v' 0.09.

Colour modes: COLOR_ALGO=mean mirrors the average radiance of the chosen zones;
COLOR_ALGO=dominant (default) mirrors their most prominent COLOURED cluster, with
neutral/white cells ignored. DOMINANT_MIX blends the two in u'v'.

Brightness: mapped from screen relative luminance across the light's full
1-255 range with no clamps and no dead band (0 = off in HA, so 1 is the floor).

Speed/smoothness: capture and send are decoupled. The capture thread samples at
CAPTURE_FPS into a one-slot mailbox; a sender thread always posts the NEWEST
colour and drops stale frames. Posting is paced to what the Zigbee radio can
take (a few commands per second on a typical mesh) and backs off on "queue BUSY";
the lamp transition auto-tracks the achieved interval so ramps chain smoothly.

Install and run (one file, one command):
    python hass_ambilights.py

The first run creates .venv beside this file and installs the dependencies, then
serves the settings UI on http://127.0.0.1:8000 and opens it, where the Home
Assistant URL, token and lights can be entered. Nothing else needs configuring,
and an environment that already has the packages is used as-is.

Check accuracy on your hardware (12 test colours, restores the light):
    python hass_ambilights.py --calibrate

Endpoints: GET /  GET /api/config  GET /health  GET /regions  GET /raw_grid
"""


import argparse
import json
import logging
import logging.handlers
import math
import socket
from typing import Any
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

# --------------------------------------------------------- dependencies -----
# Running this file is the whole install: if the packages below are missing,
# create .venv next to this script, install them, and re-execute. An existing
# environment with the packages present is used as-is, and installing by hand
# still works (nothing here runs in that case).
REQUIRED_MODULES = ("fastapi", "uvicorn", "mss", "PIL", "numpy", "requests")
BOOTSTRAP_PACKAGES = ("fastapi", "uvicorn", "mss", "pillow", "numpy", "requests")


def _missing_modules() -> list:
    import importlib.util

    return [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]


def _requirements() -> tuple:
    """requirements.txt next to the script, else the embedded list."""
    req = Path(__file__).resolve().with_name("requirements.txt")
    try:
        lines = [line.split("#")[0].strip() for line in req.read_text().splitlines()]
    except OSError:
        lines = []
    lines = [line for line in lines if line]
    return tuple(lines) if lines else BOOTSTRAP_PACKAGES


def _deps_import(python) -> bool:
    """Does that interpreter actually import everything? Cheap enough per run."""
    import subprocess

    try:
        result = subprocess.run([str(python), "-c",
                                 "import " + ", ".join(REQUIRED_MODULES)],
                                capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _bootstrap_venv():
    """Create .venv beside this file, install the dependencies, return its python."""
    import subprocess
    import venv

    root = Path(__file__).resolve().parent
    venv_dir = root / ".venv"
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    packages = _requirements()

    marker = venv_dir / ".deps-ok"
    wanted = " ".join(packages)
    if python.exists() and marker.exists() and marker.read_text().strip() == wanted \
            and _deps_import(python):
        return python                     # provisioned and actually importable
    if venv_dir.exists():
        # the marker said ready but the packages are not: an interrupted install
        # or a copied/restored directory. Start the environment again.
        print("%s is incomplete - rebuilding it" % venv_dir, flush=True)
        import shutil

        try:
            shutil.rmtree(venv_dir)
        except OSError as exc:
            raise SystemExit("cannot rebuild %s (%s).\nDelete that directory by hand "
                             "and re-run." % (venv_dir, exc))
    print("first run: creating %s" % venv_dir, flush=True)
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(str(venv_dir))
    except OSError as exc:
        raise SystemExit("cannot create %s: %s\nRun this from a directory you can write "
                         "to, or install the dependencies yourself and re-run."
                         % (venv_dir, exc))
    print("installing %s (once)" % ", ".join(packages), flush=True)
    try:
        subprocess.check_call([str(python), "-m", "pip", "install", "--quiet",
                               "--disable-pip-version-check", *packages])
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit(
            "could not install the dependencies (see the message above).\n"
            "Check your network and requirements.txt, then run this again.\n"
            "To install by hand:\n    %s -m pip install %s"
            % (python, " ".join(packages)))
    marker.write_text(wanted + "\n")
    return python


def _ensure_dependencies() -> None:
    missing = _missing_modules()
    if not missing or os.getenv("S2L_BOOTSTRAPPED"):
        return
    python = _bootstrap_venv()
    try:
        # hand over to the environment that now has everything
        os.execve(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]],
                  {**os.environ, "S2L_BOOTSTRAPPED": "1"})
    except OSError as exc:
        raise SystemExit("could not re-launch under %s: %s\nRun: %s %s" % (
            python, exc, python, Path(__file__).resolve()))


if __name__ != "__main__":      # importing the module (tests, tools) never installs
    pass
else:
    _ensure_dependencies()


import numpy as np
import requests
import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
try:                       # mss >= 10: mss.mss is deprecated in favour of MSS
    from mss import MSS as mss
except ImportError:        # older releases only ship mss.mss
    from mss import mss
from PIL import Image

# ----------------------------------------------------------------- version ---
# Semantic versioning (https://semver.org): MAJOR.MINOR.PATCH
#   MAJOR - a change that needs the user to do something (config/API break)
#   MINOR - a new capability, backwards compatible
#   PATCH - a bug fix
# Bump this before tagging a release; CI checks the format and the CHANGELOG.
__version__ = "2.3.0"


# ---------------------------------------------------------------- config ----
def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


HA_URL = _env("HA_URL", "http://homeassistant.local:8123").rstrip("/")
if HA_URL.endswith("/api"):  # tolerate the old "http://host/api" style
    HA_URL = HA_URL[: -len("/api")]
HA_TOKEN = _env("HA_TOKEN", "")

# Default light when LIGHT_MAP is empty: the whole screen onto this entity.
LIGHT_ENTITY_ID = _env("LIGHT_ENTITY_ID", "light.desk_lights")
LIGHT_MAP = _env("LIGHT_MAP", "")

CAPTURE_FPS = float(_env("CAPTURE_FPS", "12"))
# The Zigbee mesh is the bottleneck, not the network: a ZHA group takes
# roughly five commands/sec before ZHA starts answering 500 "Failed to enqueue
# message: BUSY". Pacing below whatever your mesh sustains is what keeps the ramps
# smooth.
POST_INTERVAL_FLOOR = float(_env("POST_INTERVAL_FLOOR", "0.15"))
POST_INTERVAL_CEILING = float(_env("POST_INTERVAL_CEILING", "0.6"))
FIXED_POST_INTERVAL = float(_env("POST_INTERVAL", "0"))
# Issue each cycle's lights at once instead of awaiting each device ack in turn.
SEND_CONCURRENCY = _env("SEND_CONCURRENCY", "1") == "1"
# Circuit breaker: after N consecutive failures stop sending for a while instead
# of burning a request timeout on every frame.
FAIL_THRESHOLD = int(_env("FAIL_THRESHOLD", "3"))
FAIL_COOLDOWN = float(_env("FAIL_COOLDOWN", "10"))
# Per-command HTTP timeout. A ZHA service call answers only after the Zigbee
# exchange, so this doubles as "how long to wait for the radio".
REQUEST_TIMEOUT = float(_env("REQUEST_TIMEOUT", "3"))
# How long to hold off after the Zigbee controller reports a stall (bellows NCP
# reset, "ApplicationController is not running", device did not respond).
RADIO_PAUSE = float(_env("RADIO_PAUSE", "2.0"))

# "the radio is unhappy" is not one condition:
#   queue busy  = ZHA's outbound queue is full, the frame was dropped -> pace backoff
#   controller  = the coordinator/NCP is restarting, commands hang -> pace + hold off
QUEUE_BUSY_MARKERS = ("failed to enqueue", "enqueue message", "sl_status.busy")
STALL_MARKERS = (
    "applicationcontroller is not running",
    "device did not respond",
    "no aps ack",
    "ncp",
    "ash",
    "controllererror",
)


def is_queue_busy(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in QUEUE_BUSY_MARKERS)


def is_radio_stall(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in STALL_MARKERS)


# "auto" = transition follows the measured post interval; a number pins it (s).
TRANSITION = _env("TRANSITION", "auto")
MIN_TRANSITION = float(_env("MIN_TRANSITION", "0.12"))
MAX_TRANSITION = float(_env("MAX_TRANSITION", "0.9"))

# 64x36 cells: fine enough that the gamma value inside a cell is nearly uniform
# (so the per-cell mean is honest) and that thirds land on real boundaries.
GRID_SIZE = (64, 36)                              # (width, height)
MONITOR_INDEX = int(_env("MONITOR_INDEX", "1"))   # mss: 0 = all, 1 = primary

# Capture cost at 4K is dominated by converting the BGRA frame to RGB. Instead of
# mss's `shot.rgb` (a 24 MB strided copy, ~44 ms/frame measured) the buffer is
# read as a zero-copy BGRA view and every Nth pixel is used, then PIL decodes
# BGRA natively ('BGRX') - 6 ms/frame measured. 1 = every pixel (lossless,
# ~27 ms), 4 = 1/16 of the pixels: measured max zone-level colour error dE76 2.3
# on an edge-dense synthetic page, mean 1.45.
CAPTURE_STRIDE = max(1, int(_env("CAPTURE_STRIDE", "4")))
# When the sampled grid stops changing, sample slower instead of burning CPU on
# identical frames (the colour pipeline is skipped between checks).
IDLE_FPS = float(_env("IDLE_FPS", "4"))
IDLE_AFTER = int(_env("IDLE_AFTER", "3"))

# "xy" = send chromaticity directly (most precise). "rgb" = legacy rgb_color.
SEND_MODE = _env("SEND_MODE", "xy")

SEND_BRIGHTNESS = _env("SEND_BRIGHTNESS", "1") == "1"
# What "screen brightness" means, and therefore how much the colour moves it:
#   luminance = true light output (Y). Pure red is only 21% of white's Y and pure
#               blue 7%, so saturated frames come out much dimmer - physically
#               correct, but it reads as "brightness changes with colour".
#   lstar     = CIELAB L*, the perceptual lightness of that same light: white
#               100, red 53, blue 32. Much flatter across hues.
#   value     = brightest channel (HSV V). Colour has no effect at all; white,
#               red and blue at 255 all get full brightness.
# The bulb adds its own small effect: at equal HA brightness a deeply saturated
# colour emits less luminous flux, so `value` is the only mode that keeps
# apparent room brightness constant across hues.
BRIGHTNESS_FROM = _env("BRIGHTNESS_FROM", "lstar")   # luminance | lstar | value
BRIGHTNESS_GAMMA = float(_env("BRIGHTNESS_GAMMA", "0.8"))

# What "the colour of the screen" means.
#   mean     = linear average of the whole region (faithful to radiance)
#   dominant = the most prominent COLOURED cluster, white/neutral cells ignored
COLOR_ALGO = _env("COLOR_ALGO", "dominant")        # mean | dominant
DOMINANT_MIX = float(_env("DOMINANT_MIX", "0.5"))   # 0 = mean, 1 = pure dominant
# Which brightness the light gets in dominant mode:
#   cluster = the dominant object's own luminance (a bright photo on a dark page
#             lights the room properly: measured 114 -> 191 on the same frame)
#   region  = the whole region's mean luminance (the old behaviour)
DOMINANT_LUMINANCE = _env("DOMINANT_LUMINANCE", "cluster")   # cluster | region
CHROMA_FLOOR = float(_env("CHROMA_FLOOR", "0.010"))  # du'v' below this = neutral
HUE_BINS = int(_env("HUE_BINS", "24"))

# A bright frame averages toward its white content, and a bright lamp looks
# washed out next to a bright screen, so chroma is pushed away from white as
# luminance rises: k = 1 + GAIN * lum^CURVE, capped at MAX, radially in CIE
# 1976 u'v' and binary-searched back into sRGB (no clipping, no hue shift).
SATURATION_BOOST = _env("SATURATION_BOOST", "0") == "1"   # off by default = measured colour, unboosted
SATURATION_GAIN = float(_env("SATURATION_GAIN", "0.8"))
SATURATION_CURVE = float(_env("SATURATION_CURVE", "0.6"))
SATURATION_MAX = float(_env("SATURATION_MAX", "2.2"))

# Perceptual dedupe: skip frames the eye cannot tell apart.
MIN_DELTA_E = float(_env("MIN_DELTA_E", "1.0"))
MIN_BRIGHTNESS_DELTA = int(_env("MIN_BRIGHTNESS_DELTA", "4"))
# A failed command is NOT retried: the next slot ships whatever is on screen
# then. This only re-sends if a light never confirmed the current colour for
# this long (a lost command behind a frozen screen).
RESEND_AFTER = float(_env("RESEND_AFTER", "3.0"))

DRY_RUN = _env("DRY_RUN", "0") == "1"   # log payloads, never touch the lights

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def redirect_std_streams() -> None:
    """A --windowed exe has sys.stdout/stderr set to None.

    Everything that writes to them (print, uvicorn's default log config, a
    traceback) then fails or goes nowhere, which is how a packaged app ends up
    "starting" and never serving with no usable log.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = log_file_path()
    stream = None
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(path, "a", encoding="utf-8", buffering=1)
        except OSError:
            stream = None
    if stream is None:
        stream = open(os.devnull, "a", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def setup_file_logging() -> None:
    """Packaged apps have no console: keep a rotating log we can ask for."""
    path = log_file_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000,
                                                       backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(handler)
        log.info("logging to %s", path)
    except OSError as exc:
        log.warning("could not open %s: %s", path, exc)
log = logging.getLogger("spotlight-sync")

# D65 reference white
WN = (0.95047, 1.00000, 1.08883)
D65_XY = (0.3127, 0.3290)

# --------------------------------------------------------------- targets ----
class Target:
    """One light: which zones feed it, plus its own dedupe/telemetry state."""

    __slots__ = ("entity_id", "zones", "last_attempted", "last_sent",
                 "last_success_mono", "last_attempt_mono", "posts", "busy", "errors",
                 "zone_label")

    def __init__(self, entity_id: str, zones: frozenset[int]):
        self.entity_id = entity_id
        self.zones = zones
        self.last_attempted = None
        self.last_sent = None
        self.last_success_mono = 0.0
        self.last_attempt_mono = 0.0
        self.posts = 0
        self.busy = 0
        self.errors = 0
        self.zone_label = ",".join(str(z) for z in sorted(zones))

    def as_dict(self, colour=None) -> dict:
        return {
            "entity": self.entity_id,
            "zones": sorted(self.zones),
            "posts": self.posts,
            "busy": self.busy,
            "errors": self.errors,
            "last_sent": {"rgb": list(self.last_sent[0]), "brightness": self.last_sent[1]}
            if self.last_sent else None,
            "colour": colour,
        }


def parse_zones(text: str) -> frozenset[int]:
    text = (text or "").strip().lower()
    if text in ("", "all", "*") or text == f"1-{ZONE_COUNT}":
        return frozenset(range(1, ZONE_COUNT + 1))
    zones: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-", 1)
            zones.update(range(int(low), int(high) + 1))
        else:
            zones.add(int(part))
    bad = sorted(z for z in zones if not 1 <= z <= ZONE_COUNT)
    if bad:
        raise ValueError(f"zones out of range 1-{ZONE_COUNT}: {bad}")
    return frozenset(zones)


def parse_light_map(spec: str) -> list[Target]:
    spec = (spec or "").strip()
    if not spec:
        return [Target(LIGHT_ENTITY_ID, frozenset(range(1, ZONE_COUNT + 1)))]
    targets: list[Target] = []
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        entity, _, zones = chunk.partition("=")
        entity = entity.strip()
        if not entity.startswith("light."):
            raise ValueError(f"LIGHT_MAP entry is not a light entity_id: {chunk!r}")
        targets.append(Target(entity, parse_zones(zones)))
    if not targets:
        raise ValueError("LIGHT_MAP is empty after parsing")
    return targets


ZONE_ROWS = int(_env("ZONE_ROWS", "3"))
ZONE_COLS = int(_env("ZONE_COLS", "3"))


def _build_zone_index(width: int, height: int, rows: int = 3, cols: int = 3):
    """(row_idx, col_idx) per zone, reading order, splits as even as possible."""
    row_blocks = np.array_split(np.arange(height), max(1, rows))
    col_blocks = np.array_split(np.arange(width), max(1, cols))
    return [(r, c) for r in row_blocks for c in col_blocks]


def rebuild_zone_index() -> int:
    """Re-split the grid for the configured rows/cols. Returns the zone count."""
    global ZONE_INDEX, ZONE_COUNT
    ZONE_INDEX = _build_zone_index(GRID_SIZE[0], GRID_SIZE[1], ZONE_ROWS, ZONE_COLS)
    ZONE_COUNT = len(ZONE_INDEX)
    return ZONE_COUNT


ZONE_INDEX: list = []
ZONE_COUNT = ZONE_ROWS * ZONE_COLS
rebuild_zone_index()


# --------------------------------------------------------------- settings ----
# Everything the web UI can change. `env` is only the fallback default; config.json
# (next to this file, or $CONFIG_FILE) wins, so the UI is authoritative once you
# save something.
FROZEN = bool(getattr(sys, "frozen", False))


def app_data_dir() -> Path:
    """Where a packaged app keeps its config; script mode stays beside the file.

    A one-file exe unpacks into a temporary directory, so writing next to
    __file__ would lose the settings on every launch.
    """
    if not FROZEN:
        return Path(__file__).resolve().parent
    if os.name == "nt":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData/Roaming")
    else:
        base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    folder = Path(base) / "HassAmbilights"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return folder


def log_file_path() -> Path | None:
    """A log file when there is no terminal to read (packaged app)."""
    explicit = os.getenv("LOG_FILE")
    if explicit:
        return Path(explicit)
    if not FROZEN:
        return None
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
    else:
        base = os.getenv("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "HassAmbilights" / "logs" / "app.log"


CONFIG_PATH = Path(os.getenv("CONFIG_FILE") or app_data_dir() / "config.json")
# The settings page is embedded: this file is the whole application, so copying it
# somewhere and running it is a complete install. Edit here, not in a separate asset.
UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230d1117'/%3E%3Ccircle cx='11' cy='11' r='6' fill='%23ff5c5c'/%3E%3Ccircle cx='21' cy='11' r='6' fill='%2344d17a'/%3E%3Ccircle cx='11' cy='21' r='6' fill='%234493f8'/%3E%3Ccircle cx='21' cy='21' r='6' fill='%23ffd226'/%3E%3C/svg%3E">
<title>HASS Ambilights</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2129; --line:#2b333d;
    --text:#e6edf3; --muted:#8b949e; --accent:#4493f8; --ok:#3fb950; --warn:#d29922; --bad:#f85149;
    --radius:10px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.45 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
  a{color:var(--accent)}
  :focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  header{position:sticky;top:0;z-index:10;display:flex;gap:14px;align-items:center;
    padding:12px 20px;background:rgba(13,17,23,.94);border-bottom:1px solid var(--line);
    backdrop-filter:blur(8px);flex-wrap:wrap}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.2px}
  header .live{display:flex;gap:14px;align-items:center;margin-left:auto;font-size:12px;color:var(--muted)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block}
  .dot.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}
  .dot.bad{background:var(--bad)}
  main{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:20px;padding:20px;max-width:1200px;margin:0 auto}
  @media(max-width:940px){main{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);margin-bottom:18px}
  .card h2{margin:0;padding:12px 16px;font-size:12px;text-transform:uppercase;letter-spacing:.08em;
    color:var(--muted);border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
  .card .body{padding:14px 16px}
  .row{display:grid;grid-template-columns:210px 1fr;gap:16px;align-items:start;padding:10px 0;
    border-top:1px dashed rgba(255,255,255,.06)}
  .row:first-child{border-top:0}
  .adv{margin-top:10px;border-top:1px solid var(--line)}
  .adv>summary{cursor:pointer;padding:9px 0 0;font-size:12px;letter-spacing:.04em;
    text-transform:uppercase;color:var(--muted);list-style:none;user-select:none}
  .adv>summary::-webkit-details-marker{display:none}
  .adv>summary:before{content:"▸  ";font-size:11px}
  .adv[open]>summary:before{content:"▾  "}
  .adv>summary:hover{color:var(--text)}
  .adv .row:first-of-type{margin-top:8px}
  .row[hidden]{display:none}
  .row.restart-required .label:after{content:"restart";margin-left:8px;font-size:10px;letter-spacing:.06em;
    text-transform:uppercase;color:var(--warn);border:1px solid rgba(210,153,34,.5);border-radius:4px;
    padding:1px 4px;vertical-align:middle}
  .label{font-weight:500;display:block}
  .help{color:var(--muted);font-size:12px;margin-top:2px}
  .ctl{display:flex;gap:10px;align-items:center}
  input[type=range]{width:100%;accent-color:var(--accent);background:transparent}
  input[type=number],input[type=text],select{background:var(--panel2);color:var(--text);
    border:1px solid var(--line);border-radius:7px;padding:6px 8px;font:inherit}
  input[type=number]{width:84px}
  input[type=text]{width:100%;font-family:ui-monospace,monospace;font-size:12px}
  select{min-width:150px}
  .switch{position:relative;display:inline-block;width:44px;height:24px;flex:none}
  .switch input{opacity:0;width:0;height:0;position:absolute}
  .switch span{position:absolute;inset:0;background:#30363d;border-radius:99px;transition:.15s;cursor:pointer}
  .switch span:before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;
    background:#e6edf3;border-radius:50%;transition:.15s}
  .switch input:checked + span{background:var(--accent)}
  .switch input:checked + span:before{transform:translateX(20px)}
  .light{display:grid;grid-template-columns:minmax(0,1fr) auto auto auto;gap:12px;align-items:center;
    padding:10px;border:1px solid var(--line);border-radius:9px;margin-bottom:10px;background:var(--panel2)}
  .light:hover{border-color:#3d4b5c}
  .chip{width:34px;height:34px;border-radius:8px;border:1px solid var(--line);flex:none}
  .zones{display:grid;gap:3px;grid-template-columns:repeat(var(--zone-cols,3),26px)}
  .zones button{background:#222932;border:1px solid var(--line);border-radius:5px;color:var(--muted);
    font-size:11px;cursor:pointer;padding:0}
  .zones button.on{background:var(--accent);border-color:var(--accent);color:#0d1117;font-weight:700}
  .zones button:hover{border-color:var(--accent)}
  .btn{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:7px;
    padding:7px 12px;font:inherit;cursor:pointer}
  .btn:hover:not(:disabled){border-color:var(--accent)}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:#0b1118;font-weight:600}
  .btn.danger:hover:not(:disabled){border-color:var(--bad);color:var(--bad)}
  .actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:14px 0 26px}
  aside .card{position:sticky;top:70px}
  .swatch{height:82px;border-radius:9px;border:1px solid var(--line);background:#000;
    display:flex;align-items:flex-end;justify-content:space-between;padding:8px;font-size:11px;
    font-family:ui-monospace,monospace;text-shadow:0 1px 3px #000}
  .kvs{display:grid;gap:2px;margin-top:10px}
  .kv{display:flex;justify-content:space-between;gap:10px;padding:4px 0;font-size:12px;
    border-top:1px dashed rgba(255,255,255,.06)}
  .kv:first-child{border-top:0}
  .kv b{font-weight:500;color:var(--muted)}
  .kv span{font-variant-numeric:tabular-nums}
  .zonebits{display:grid;grid-template-columns:repeat(var(--zone-cols,3),1fr);gap:3px;margin:8px 0}
  .zonebits div{height:16px;border-radius:3px;background:#222932;position:relative}
  .zonebits div.used{box-shadow:inset 0 0 0 1px var(--accent)}
  .lightlive{padding:8px 0;border-top:1px dashed rgba(255,255,255,.06)}
  .lightlive:first-child{border-top:0;padding-top:0}
  .lightlive .name{font-size:12px;font-family:ui-monospace,monospace;color:var(--muted);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .lightlive .bar{display:flex;align-items:center;gap:8px;margin-top:4px}
  .lightlive .mini{width:26px;height:26px;border-radius:6px;border:1px solid var(--line);flex:none}
  .lightlive .meta{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
  .badge{font-size:11px;padding:1px 6px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
  .badge.dirty{color:var(--warn);border-color:rgba(210,153,34,.5)}
  .banner{border:1px solid var(--bad);background:rgba(248,81,73,.08);color:#ffb4ae;
    border-radius:9px;padding:10px 14px;margin-bottom:16px;font-size:13px}
  #toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(90px);
    background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px 16px;
    transition:.2s;max-width:min(90vw,620px);z-index:20;box-shadow:0 8px 24px rgba(0,0,0,.45)}
  #toast.show{transform:translateX(-50%) translateY(0)}
  #toast.err{border-color:var(--bad);color:#ffb4ae}
  small.mono{font-family:ui-monospace,monospace;color:var(--muted)}
  .visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
    clip:rect(0 0 0 0);white-space:nowrap;border:0}
</style>
</head>
<body>
<header>
  <h1>HASS Ambilights</h1>
  <span class="badge" id="dirtybadge" hidden>unsaved changes</span>
  <span class="live">
    <span id="dot" class="dot"></span><span id="conn">connecting…</span>
    <span id="hapill" class="mono"></span>
  </span>
</header>

<main>
  <div>
    <div id="loaderror"></div>
    <div id="settings"></div>

    <div class="card">
      <h2>Lights</h2>
      <div class="body">
        <div id="lights"></div>
        <div class="actions" style="padding:6px 0 0">
          <button class="btn" id="addlight" type="button">+ Add light</button>
          <button class="btn" id="refreshlights" type="button"
                  title="Reload the light list from Home Assistant">↻ From Home Assistant</button>
          <small class="mono" id="lightsource">Zones are a 3×3 grid in reading order:
            1 2 3 / 4 5 6 / 7 8 9. Leave empty to use <code>LIGHT_ENTITY_ID</code>
            for the whole screen.</small>
        </div>
      </div>
    </div>

    <div class="actions">
      <button class="btn primary" id="save" type="button">Save &amp; apply</button>
      <button class="btn" id="revert" type="button">Revert</button>
      <button class="btn" id="calibrate" type="button">Calibrate…</button>
      <button class="btn danger" id="reset" type="button">Reset to defaults</button>
      <small class="mono" id="version"></small>
      <small class="mono" id="cfgpath"></small>
    </div>
  </div>

  <aside>
    <div class="card">
      <h2>Live</h2>
      <div class="body">
        <div id="lightlive"></div>
        <div class="zonebits" id="zonebits" aria-hidden="true"></div>
        <div class="kvs">
          <div class="kv"><b>posts</b><span id="s-posts">–</span></div>
          <div class="kv"><b>per second</b><span id="s-rate">–</span></div>
          <div class="kv"><b>frame age</b><span id="s-age">–</span></div>
          <div class="kv"><b>prep</b><span id="s-prep">–</span></div>
          <div class="kv"><b>cycle</b><span id="s-cycle">–</span></div>
          <div class="kv"><b>pace / transition</b><span id="s-pace">–</span></div>
          <div class="kv"><b>busy · stalls · timeouts</b><span id="s-err" title="">–</span></div>
          <div class="kv"><b>idle</b><span id="s-idle">–</span></div>
          <div class="kv"><b>calibration</b><span id="s-cal">–</span></div>
        </div>
      </div>
    </div>
  </aside>
</main>

<div id="toast" role="status" aria-live="polite"></div>

<script>
let ZONE_ROWS = 3, ZONE_COLS = 3, ZONE_COUNT = 9;
let META = [], CFG = {}, LIGHTS = [], dirty = false, lastPosts = null, lastPostT = null, rate = 0;
let LASTCONFIG = null;
let SAVED = {};               // what is on disk, for Revert and the badge
let liveTimer = null, liveSeq = 0;
const NOT_LIVE = ["HA_URL", "HA_TOKEN"];   // typing these must not reconnect mid-word
const ADV_OPEN = {};          // remembered "Advanced" disclosure state per card

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const toast = (msg, err) => {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(t._t);
  t._t = setTimeout(() => { t.className = ""; }, err ? 7000 : 2600);
};

function setDirty(v, live) {
  dirty = v;
  if (v && live !== false) applyLive();
  refreshVisibility();
  $("dirtybadge").hidden = !v;
  $("save").disabled = !v;
  $("revert").disabled = !v;
}

/* ---------------------------------------------------------------- lights -- */
function parseMap(text) {
  const out = [];
  (text || "").split(";").forEach((chunk) => {
    chunk = chunk.trim();
    if (!chunk) return;
    const [entity, zones] = chunk.split("=");
    const set = new Set();
    const spec = (zones ?? "all").trim().toLowerCase();
    if (spec === "all" || spec === "*" || spec === "") { for (let i = 1; i <= 9; i++) set.add(i); }
    else spec.replace(/\s/g, "").split(",").forEach((p) => {
      if (p.includes("-")) { const [a, b] = p.split("-"); for (let i = +a; i <= +b; i++) set.add(i); }
      else if (p) set.add(+p);
    });
    out.push({ entity: (entity || "").trim(), zones: set });
  });
  return out;
}

const serializeMap = (list) => list
  .filter((l) => l.entity.trim())
  .map((l) => {
    const z = [...l.zones].sort((a, b) => a - b);
    return l.entity.trim() + "=" + (z.length === ZONE_COUNT ? "all" : (z.join(",") || "all"));
  }).join("; ");

function renderLights() {
  const box = $("lights");
  box.innerHTML = "";
  if (!LIGHTS.length) {
    box.innerHTML = '<small class="mono">No lights mapped — the whole screen will drive '
      + "<code>LIGHT_ENTITY_ID</code>. Add one to split the screen into zones.</small>";
  }
  LIGHTS.forEach((light, i) => {
    const row = document.createElement("div");
    row.className = "light";

    let name;
    if (ENTITIES && ENTITIES.ok && ENTITIES.lights.length) {
      name = document.createElement("select");
      name.id = `light-${i}`;
      name.setAttribute("aria-label", `Light ${i + 1} entity`);
      const options = ENTITIES.lights.slice();
      if (light.entity && !options.some((l) => l.entity_id === light.entity)) {
        options.unshift({ entity_id: light.entity, name: `${light.entity} (not reported by HA)`,
                          color_capable: true });
      }
      const blank = document.createElement("option");
      blank.value = ""; blank.textContent = "— pick a light —";
      name.appendChild(blank);
      options.forEach((l) => {
        const o = document.createElement("option");
        o.value = l.entity_id;
        o.textContent = `${l.name} — ${l.entity_id}` + (l.color_capable ? "" : " (no colour)");
        if (l.entity_id === light.entity) o.selected = true;
        name.appendChild(o);
      });
      if (!light.entity) name.value = "";
      name.onchange = () => { LIGHTS[i].entity = name.value; setDirty(true, true); markUsedZones(); };
    } else {
      name = document.createElement("input");
      name.type = "text";
      name.value = light.entity;
      name.placeholder = "light.left_lamp";
      name.id = `light-${i}`;
      name.setAttribute("aria-label", `Light ${i + 1} entity id`);
      name.oninput = () => { LIGHTS[i].entity = name.value; setDirty(true, true); };
    }

    const zones = document.createElement("div");
    zones.className = "zones";
    zones.style.setProperty("--zone-cols", ZONE_COLS);
    zones.setAttribute("role", "group");
    zones.setAttribute("aria-label", `Light ${i + 1} zones`);
    for (let z = 1; z <= ZONE_COUNT; z++) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = z;
      b.className = light.zones.has(z) ? "on" : "";
      b.setAttribute("aria-pressed", light.zones.has(z) ? "true" : "false");
      b.title = `Zone ${z}`;
      b.onclick = () => {
        light.zones.has(z) ? light.zones.delete(z) : light.zones.add(z);
        b.className = light.zones.has(z) ? "on" : "";
        b.setAttribute("aria-pressed", light.zones.has(z) ? "true" : "false");
        setDirty(true, true);
        markUsedZones();
      };
      zones.appendChild(b);
    }

    const chip = document.createElement("div");
    chip.className = "chip";
    chip.dataset.chip = light.entity || `light-${i}`;
    chip.title = "current colour";

    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn danger";
    del.textContent = "✕";
    del.title = `Remove ${light.entity || "light"}`;
    del.setAttribute("aria-label", `Remove light ${light.entity || i + 1}`);
    del.onclick = () => { LIGHTS.splice(i, 1); setDirty(true, true); renderLights(); markUsedZones(); };

    row.append(name, zones, chip, del);
    box.appendChild(row);
  });
  markUsedZones();
}

function addLight() {
  LIGHTS.push({ entity: "", zones: new Set(Array.from({length: ZONE_COUNT}, (_, i) => i + 1)) });
  setDirty(true, true);
  renderLights();
}

function markUsedZones() {
  const used = new Set();
  LIGHTS.forEach((l) => l.zones.forEach((z) => used.add(z)));
  [...$("zonebits").children].forEach((cell, i) => {
    cell.classList.toggle("used", used.has(i + 1));
  });
}

/* -------------------------------------------------------------- settings -- */
function control(meta, value) {
  const wrap = document.createElement("div");
  wrap.className = "ctl";
  const id = `set-${meta.name}`;
  if (meta.monitors) {
    const sel = document.createElement("select");
    sel.id = id;
    sel.setAttribute("aria-label", meta.label);
    const options = (MONITORS && MONITORS.ok) ? MONITORS.monitors : [];
    if (!options.length) {
      const only = document.createElement("option");
      only.value = "", only.textContent = "monitor list unavailable - press Save to reload";
      sel.appendChild(only);
    }
    options.forEach((mon) => {
      const o = document.createElement("option");
      o.value = mon.index; o.textContent = mon.label;
      if (String(mon.index) === String(value)) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = () => { CFG[meta.name] = Number(sel.value); setDirty(true, true); };
    return sel;
  }


  if (meta.type === "bool") {
    const label = document.createElement("label");
    label.className = "switch";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!value; cb.id = id;
    const sp = document.createElement("span");
    cb.onchange = () => { CFG[meta.name] = cb.checked; setDirty(true, true); };
    label.append(cb, sp);
    wrap.appendChild(label);
    return wrap;
  }
  if (meta.type === "enum") {
    const sel = document.createElement("select");
    sel.id = id;
    meta.values.forEach((v) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = v;
      if (v === value) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = () => { CFG[meta.name] = sel.value; setDirty(true, true); };
    wrap.appendChild(sel);
    return wrap;
  }
  if (meta.type === "float" || meta.type === "int") {
    if (meta.type === "int" && (meta.max ?? 100) - (meta.min ?? 0) > 2000) {
      // a 1..65535 slider is unusable (ports), so number-only
      const only = document.createElement("input");
      only.type = "number"; only.id = id;
      only.min = meta.min ?? 0; only.max = meta.max ?? 100; only.step = 1; only.value = value;
      only.setAttribute("aria-label", meta.label);
      only.onchange = () => {
        const v = Math.min(+only.max, Math.max(+only.min, Math.round(+only.value)));
        only.value = v; CFG[meta.name] = v; setDirty(true, true);
      };
      wrap.appendChild(only);
      return wrap;
    }
    const range = document.createElement("input");
    range.type = "range";
    range.min = meta.min ?? 0; range.max = meta.max ?? 100;
    range.step = meta.step ?? (meta.type === "int" ? 1 : 0.01);
    range.value = value; range.id = id;
    range.setAttribute("aria-label", meta.label);
    const num = document.createElement("input");
    num.type = "number";
    num.min = range.min; num.max = range.max; num.step = range.step; num.value = value;
    num.setAttribute("aria-label", `${meta.label} value`);
    const sync = (v) => {
      v = Math.min(+range.max, Math.max(+range.min, +v));
      CFG[meta.name] = v; range.value = v; num.value = v; setDirty(true, true);
    };
    range.oninput = () => sync(range.value);
    num.onchange = () => sync(num.value);
    wrap.append(range, num);
    return wrap;
  }
  if (meta.type === "secret") {
    const secret = document.createElement("input");
    const isSet = value === "<set>";
    secret.type = "password";
    secret.id = id;
    secret.autocomplete = "new-password";
    secret.value = "";
    secret.placeholder = isSet ? "●●●● saved — type to replace" : "paste a long-lived token";
    secret.oninput = () => { CFG[meta.name] = secret.value; setDirty(true, true); };
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "btn";
    clear.textContent = "Clear";
    clear.disabled = !isSet;
    clear.title = "Remove the saved token";
    clear.onclick = () => {
      CFG[meta.name] = "--clear--";
      secret.value = ""; secret.placeholder = "cleared on save"; clear.disabled = true;
      setDirty(true, true);
    };
    wrap.append(secret, clear);
    return wrap;
  }
  const text = document.createElement("input");
  text.type = "text"; text.value = value ?? ""; text.id = id;
  text.oninput = () => { CFG[meta.name] = text.value; setDirty(true, true); };
  wrap.appendChild(text);
  return wrap;
}

function settingRow(m) {
  const row = document.createElement("div");
  row.className = "row" + (m.restart ? " restart-required" : "");
  row.dataset.setting = m.name;
  if (m.show_if) row.dataset.showIf = JSON.stringify(m.show_if);
  const left = document.createElement("div");
  const lab = document.createElement("label");
  lab.className = "label";
  lab.htmlFor = `set-${m.name}`;
  lab.textContent = m.label;
  const help = document.createElement("div");
  help.className = "help";
  help.textContent = m.help || "";
  left.append(lab, help);
  row.append(left, control(m, CFG[m.name]));
  return row;
}

// a setting that the current mode doesn't use is hidden, not disabled:
// disabled controls look broken and still invite a click
function refreshVisibility() {
  document.querySelectorAll("#settings .row[data-show-if]").forEach((row) => {
    let rule;
    try { rule = JSON.parse(row.dataset.showIf); } catch (e) { return; }
    const visible = Object.entries(rule).every(([name, want]) => {
      const got = CFG[name];
      return String(got) === String(want) || (typeof want === "boolean" && !!got === want);
    });
    row.hidden = !visible;
  });
  document.querySelectorAll("#settings details.adv").forEach((box) => {
    const shown = [...box.querySelectorAll(".row[data-setting]")].filter((r) => !r.hidden).length;
    const total = box.querySelectorAll(".row[data-setting]").length;
    box.querySelector("summary").textContent =
      "Advanced" + (shown === total ? ` (${total})` : ` (${shown} of ${total})`);
  });
}

function renderSettings() {
  const groups = {};
  META.filter((m) => m.name !== "LIGHT_MAP").forEach((m) => (groups[m.group] ||= []).push(m));
  const host = $("settings");
  host.innerHTML = "";
  Object.entries(groups).forEach(([group, items]) => {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `<h2>${esc(group)}</h2>`;
    const body = document.createElement("div");
    body.className = "body";
    items.filter((m) => !m.advanced).forEach((m) => body.appendChild(settingRow(m)));
    if (group === "Sending") {                 // the tuning button belongs at the top
      const tune = document.createElement("div");
      tune.className = "actions";
      tune.style.padding = "0 0 10px";
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "btn"; btn.id = "autotune";
      btn.textContent = "Auto tune…";
      btn.title = "Measure the fastest pacing these lights accept, using every light";
      const status = document.createElement("span");
      status.className = "mono"; status.id = "autotunestatus";
      status.style.fontSize = "12px";
      btn.onclick = autotuneNow;
      tune.append(btn, status);
      body.insertBefore(tune, body.firstChild);     // top of the card, above the knobs
    }
    const advanced = items.filter((m) => m.advanced);
    if (advanced.length) {
      const box = document.createElement("details");
      box.className = "adv";
      box.open = !!ADV_OPEN[group];
      box.ontoggle = () => { ADV_OPEN[group] = box.open; };
      const summary = document.createElement("summary");
      summary.textContent = "Advanced";
      const inner = document.createElement("div");
      inner.className = "advbody";
      advanced.forEach((m) => inner.appendChild(settingRow(m)));
      box.append(summary, inner);
      body.appendChild(box);
    }
    if (group === "Home Assistant") {
      // test the URL/token where they are edited, not in a separate strip
      const conn = document.createElement("div");
      conn.className = "actions";
      conn.style.padding = "10px 0 0";
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "btn"; btn.id = "testconn";
      btn.textContent = "Test connection";
      const status = document.createElement("span");
      status.className = "mono"; status.id = "connstatus";
      status.style.fontSize = "12px";
      btn.onclick = testConnection;
      conn.append(btn, status);
      body.appendChild(conn);
    }
    card.appendChild(body);
    host.appendChild(card);
  });
  refreshVisibility();
}

/* ------------------------------------------------------------------ data -- */
let ENTITIES = null;
let MONITORS = null;

async function loadMonitors() {
  try {
    MONITORS = await fetch("/api/monitors").then((r) => r.json());
  } catch (e) {
    MONITORS = { ok: false, monitors: [], detail: e.message };
  }
  return MONITORS;
}

async function loadEntities(force) {
  const note = $("lightsource");
  try {
    const r = await fetch("/api/lights" + (force ? "?refresh=1" : ""));
    ENTITIES = await r.json();
  } catch (e) {
    ENTITIES = { ok: false, lights: [], detail: e.message };
  }
  if (note) {
    if (ENTITIES.ok) {
      note.textContent = `${ENTITIES.count} light(s) from Home Assistant — pick one per row.`;
    } else {
      note.textContent = `Typing entity ids by hand: ${ENTITIES.detail}.`;
    }
  }
  return ENTITIES;
}

async function reload() {
  let data;
  try {
    const r = await fetch("/api/config");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    data = await r.json();
  } catch (e) {
    $("loaderror").innerHTML =
      `<div class="banner">Could not load settings from the server (${esc(e.message)}).
       The backend may still be starting — retry in a moment.</div>`;
    return;
  }
  LASTCONFIG = data;
  $("loaderror").innerHTML = "";
  META = data.meta;
  CFG = data.settings;
  if (Array.isArray(data.zone_grid)) {
    ZONE_ROWS = data.zone_grid[0];
    ZONE_COLS = data.zone_grid[1];
    ZONE_COUNT = data.zone_count || ZONE_ROWS * ZONE_COLS;
  }
  SAVED = data.saved_settings || {};
  LIGHTS = parseMap(CFG.LIGHT_MAP || "");
  if (ENTITIES === null) await loadEntities(false);   // first paint only
  if (MONITORS === null) await loadMonitors();
  $("version").textContent = data.version ? "v" + data.version : "";
  $("version").title = "HASS Ambilights " + (data.version || "");
  $("cfgpath").textContent = data.config_path;
  $("hapill").textContent = data.ha_url;
  renderSettings();
  renderLights();
  setDirty(!!data.unsaved);        // live values that the file doesn't have yet
}

function applyLive() {
  clearTimeout(liveTimer);
  liveTimer = setTimeout(async () => {
    const seq = ++liveSeq;
    CFG.LIGHT_MAP = serializeMap(LIGHTS);      // lights/zones are live too
    const settings = {};
    Object.keys(CFG).forEach((k) => { if (!NOT_LIVE.includes(k)) settings[k] = CFG[k]; });
    try {
      const r = await fetch("/api/config", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings, persist: false }),
      });
      if (seq !== liveSeq) return;                 // a newer edit won
      const data = await r.json();
      if (!data.ok) { toast("Ignored: " + data.errors.join(" · "), true); return; }
      LASTCONFIG = LASTCONFIG || {};
      LASTCONFIG.restart_required = data.restart_required || [];
    } catch (e) {
      toast("Live apply failed: " + e.message, true);
    }
  }, 350);
}

async function save() {
  CFG.LIGHT_MAP = serializeMap(LIGHTS);
  $("save").disabled = true;
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: CFG }),
    });
    const data = await r.json();
    if (!data.ok) {
      toast("Rejected: " + data.errors.join(" · "), true);
      $("save").disabled = false;
      return;
    }
    let msg = data.persisted === false ? "Applied (not saved)" : "Saved & applied";
    if (data.restart_required.length) msg += " — restart to apply " + data.restart_required.join(", ");
    if (data.connection && data.connection.detail !== "unchanged") {
      msg += " · HA: " + data.connection.detail;
      if (!data.connection.ok) { toast(msg, true); await reload(); return; }
    }
    toast(msg);
    await reload();
  } catch (e) {
    toast("Save failed: " + e.message, true);
    $("save").disabled = false;
  }
}

async function revertToSaved() {
  if (SAVED && Object.keys(SAVED).length) {
    await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: SAVED, persist: false }),
    }).catch(() => {});
  }
  await reload();
  toast("Reverted to the saved file");
}

async function resetDefaults() {
  if (!confirm("Reset every setting to its default and rewrite config.json?")) return;
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reset: true }),
    });
    const data = await r.json();
    if (!data.ok) { toast("Rejected: " + data.errors.join(" · "), true); return; }
    toast("Defaults restored");
    await reload();
  } catch (e) {
    toast("Reset failed: " + e.message, true);
  }
}

async function calibrate() {
  const entity = (LIGHTS.find((l) => l.entity.trim()) || {}).entity
    || (LASTCONFIG && LASTCONFIG.lights[0] && LASTCONFIG.lights[0].entity) || "the light";
  if (!confirm(`Measure colour accuracy on ${entity}?\n\nIt will flash twelve test colours `
    + "for about seven seconds, then restore the light's previous state.")) return;
  $("calibrate").disabled = true;
  try {
    const r = await fetch("/api/calibrate", { method: "POST" });
    const data = await r.json();
    if (!data.ok) { toast("Calibration: " + data.error, true); $("calibrate").disabled = false; return; }
    toast(`Calibrating ${data.entity}…`);
    const started = Date.now();
    const poll = setInterval(async () => {
      const h = await fetch("/health").then((x) => x.json()).catch(() => null);
      if (!h) return;
      if (h.stats && h.stats.calibrating) {
        if (Date.now() - started > 30000) { clearInterval(poll); $("calibrate").disabled = false; }
        return;
      }
      clearInterval(poll);
      $("calibrate").disabled = false;
      const rep = (h.stats || {}).calibration;
      if (!rep) { toast("Calibration finished without a report", true); return; }
      if (rep.error) { toast("Calibration failed: " + rep.error, true); return; }
      if (rep.dry_run) { toast("Dry run: payloads logged, nothing sent"); return; }
      toast(`Calibration: worst du'v' ${rep.worst_duv} (dE76 ${rep.worst_de}) over `
        + `${rep.samples.length} colours · light ${rep.restored ? "restored" : "NOT restored"}`,
        !rep.restored);
    }, 1000);
  } catch (e) {
    toast("Calibration request failed: " + e.message, true);
    $("calibrate").disabled = false;
  }
}

/* ------------------------------------------------------------------ live -- */
function hexFromXy(x, y) {
  if (x == null || y == null || y <= 0) return null;
  const X = x / y, Y = 1, Z = (1 - x - y) / y;
  const lin = [
    3.2406 * X - 1.5372 * Y - 0.4986 * Z,
    -0.9689 * X + 1.8758 * Y + 0.0415 * Z,
    0.0557 * X - 0.2040 * Y + 1.0570 * Z,
  ];
  const enc = (v) => {
    v = Math.max(0, Math.min(1, v));
    return v <= 0.0031308 ? v * 12.92 : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
  };
  return "#" + lin.map((v) => Math.round(enc(v) * 255).toString(16).padStart(2, "0")).join("");
}

// Write only when the value actually differs: a value that is set to what it
// already was still costs a style recalculation, and on WebView2 a page that is
// rebuilt every second makes the mouse cursor flicker as hit-testing resets.
function setText(node, value) {
  const text = String(value);
  if (node.textContent !== text) node.textContent = text;
}

function setBackground(node, colour) {
  // compare what we last set: the browser normalises hex to rgb() when read back,
  // so reading style.backgroundColor never matches the value we passed in
  if (node.dataset.bg === colour) return;
  node.dataset.bg = colour;
  node.style.background = colour;
}

function setTitle(node, text) {
  if (node.title !== text) node.title = text;
}

const LIVE_ROWS = new Map();          // entity id -> the row built for it

function liveRow(entity) {
  let row = LIVE_ROWS.get(entity);
  if (row) return row;
  const block = document.createElement("div");
  block.className = "lightlive";
  block.dataset.entity = entity;
  const name = document.createElement("div");
  name.className = "name";
  const bar = document.createElement("div");
  bar.className = "bar";
  const mini = document.createElement("span");
  mini.className = "mini";
  const meta = document.createElement("span");
  meta.className = "meta";
  bar.append(mini, meta);
  const counters = document.createElement("div");
  counters.className = "meta";
  block.append(name, bar, counters);
  row = { block, name, mini, meta, counters };
  LIVE_ROWS.set(entity, row);
  return row;
}

function renderLightLive(health) {
  const host = $("lightlive");
  const lights = (health && health.lights) || [];
  if (!lights.length) {
    if (!host.querySelector(".lightlive.empty")) {
      host.innerHTML = '<div class="lightlive empty"><div class="name">no lights configured</div></div>';
    }
    return;
  }
  const placeholder = host.querySelector(".lightlive.empty");
  if (placeholder) placeholder.remove();

  const seen = new Set();
  lights.forEach((l, index) => {
    const colour = l.colour || null;
    const hex = colour && colour.preview ? colour.preview.hex
      : (colour && colour.xy ? hexFromXy(colour.xy[0], colour.xy[1]) : null);
    const sent = l.last_sent;
    const row = liveRow(l.entity);
    seen.add(l.entity);

    if (row.name.textContent !== l.entity) {
      row.name.textContent = l.entity;
    }
    setTitle(row.name, l.entity);
    setBackground(row.mini, hex || "#111");
    const brightness = colour && colour.preview ? colour.preview.brightness
      : (sent && sent.brightness != null ? sent.brightness : null);
    setText(row.meta, (hex || "no frame yet")
      + (brightness != null ? " · bri " + brightness : "")
      + " · " + ((l.zones || []).join(",") || "all"));
    setText(row.counters, `${l.posts} posts · ${l.busy} busy · ${l.errors} errors`);

    // keep order without moving nodes that are already in place
    if (host.children[index] !== row.block) host.insertBefore(row.block, host.children[index] || null);

    const chip = document.querySelector(`.chip[data-chip="${CSS.escape(l.entity)}"]`);
    if (chip && hex) setBackground(chip, hex);
  });

  [...LIVE_ROWS.keys()].forEach((entity) => {
    if (seen.has(entity)) return;
    LIVE_ROWS.get(entity).block.remove();
    LIVE_ROWS.delete(entity);
  });
}

async function tick() {
  try {
    const h = await fetch("/health").then((r) => r.json());
    $("dot").className = "dot on";
    $("conn").textContent = "connected";
    const s = h.stats || {};
    if (lastPosts !== null && lastPostT !== null && s.posts >= lastPosts) {
      const dt = (performance.now() - lastPostT) / 1000;
      if (dt > 0.3) { rate = (s.posts - lastPosts) / dt; lastPosts = s.posts; lastPostT = performance.now(); }
    } else { lastPosts = s.posts ?? 0; lastPostT = performance.now(); }
    $("s-posts").textContent = s.posts ?? "–";
    $("s-rate").textContent = rate ? rate.toFixed(1) + " /s" : "–";
    $("s-age").textContent = s.age_ms != null ? s.age_ms.toFixed(0) + " ms" : "–";
    $("s-prep").textContent = s.prep_ms != null ? s.prep_ms.toFixed(1) + " ms" : "–";
    $("s-cycle").textContent = s.cycle_ms != null ? s.cycle_ms.toFixed(0) + " ms" : "–";
    $("s-pace").textContent = (s.pace_ms ?? "–") + " ms / " + (s.transition ?? "–") + " s";
    $("s-err").textContent = `${s.busy ?? 0} · ${s.radio_stalls ?? 0} · ${s.timeouts ?? 0}`
      + (s.frame_errors ? ` · ${s.frame_errors} frame errors` : "");
    $("s-err").title = s.last_frame_error ? "last frame error: " + s.last_frame_error : "";
    $("s-idle").textContent = s.idle ? "yes" : "no";
    if (s.calibrating) $("s-cal").textContent = "running…";
    else if (s.calibration) {
      $("s-cal").textContent = s.calibration.error
        ? "failed"
        : s.calibration.dry_run ? "dry run"
        : `du'v' ${s.calibration.worst_duv} · dE ${s.calibration.worst_de}`;
    } else $("s-cal").textContent = "–";

    renderLightLive(h);

    const zones = await fetch("/regions").then((r) => r.json()).catch(() => null);
    const zb = $("zonebits");
    zb.style.setProperty("--zone-cols", ZONE_COLS);
    if (zb.children.length !== ZONE_COUNT) {   // draw the grid before the first frame
      zb.innerHTML = "";
      for (let i = 1; i <= ZONE_COUNT; i++) {
        const d = document.createElement("div");
        d.title = `zone ${i}`;
        zb.appendChild(d);
      }
      markUsedZones();
    }
    if (zones && Array.isArray(zones.zones) && zones.zones.length === ZONE_COUNT) {
      zones.zones.forEach((z, i) => {
        const cell = zb.children[i];
        const p = z && z.preview;
        const hex = p ? p.hex : (z && z.xy ? hexFromXy(z.xy[0], z.xy[1]) : null);
        setBackground(cell, hex || "#222932");
        // the swatch is the payload colour at the brightness that gets sent
        setTitle(cell, `zone ${i + 1}` + (p
          ? ` · ${p.hex} · brightness ${p.brightness} · xy ${p.xy[0]}, ${p.xy[1]}`
          : " · no data"));
      });
      markUsedZones();
    }
  } catch (e) {
    $("dot").className = "dot bad";
    $("conn").textContent = "offline";
  }
}

/* ----------------------------------------------------------------- wiring -- */
// Bind defensively: a missing element used to throw here and kill the whole
// script before the first render, leaving a blank page instead of a hint.
function on(id, event, handler) {
  const el = $(id);
  if (el) el.addEventListener(event, handler);
  else console.error(`UI element #${id} is missing - not binding ${event}`);
  return el;
}
on("save", "click", save);
on("revert", "click", revertToSaved);
on("reset", "click", resetDefaults);
on("calibrate", "click", calibrate);
on("addlight", "click", addLight);
on("refreshlights", "click", async () => {
  await loadEntities(true);
  renderLights();
  toast((ENTITIES.ok ? `${ENTITIES.count} light(s) loaded` : "Could not load lights: " + ENTITIES.detail),
        !ENTITIES.ok);
});
async function autotuneNow() {
  const status = $("autotunestatus");
  const howMany = LIGHTS.filter((l) => l.entity.trim()).length || 1;
  if (!confirm(`Auto tune sends a short burst of commands to all ${howMany} light`
             + `${howMany === 1 ? "" : "s"}, brightening and dimming them for about `
             + "ten seconds, then picks the fastest pacing they accepted.\n\nContinue?")) return;
  $("autotune").disabled = true;
  status.textContent = "measuring…";
  try {
    const r = await fetch("/api/autotune", { method: "POST" });
    const data = await r.json();
    if (!data.ok) { status.textContent = "✖ " + (data.error || "could not start"); return; }
    for (let i = 0; i < 120; i++) {
      await new Promise((res) => setTimeout(res, 500));
      const h = await fetch("/health").then((x) => x.json());
      const s = h.stats || {};
      if (s.autotuning) continue;
      const report = s.autotune || {};
      if (report.ok) {
        const lights = report.light_count || 0;
        const message = `✔ ${report.floor.toFixed(2)}–${report.ceiling.toFixed(2)} s`
          + (lights ? ` · ${lights} light${lights === 1 ? "" : "s"}` : "")
          + (report.busy_at_ms ? ` (rejected at ${report.busy_at_ms} ms)` : " (no rejections)");
        await reload();                 // applied live; the badge asks you to save
        const fresh = $("autotunestatus");   // reload rebuilds the card
        if (fresh) { fresh.textContent = message; fresh.style.color = "var(--ok)"; }
      } else {
        status.textContent = "✖ " + (report.error || "no result");
        status.style.color = "var(--bad)";
      }
      return;
    }
    status.textContent = "✖ timed out";
  } catch (e) {
    status.textContent = "✖ " + e.message;
    status.style.color = "var(--bad)";
  } finally {
    $("autotune").disabled = false;
  }
}

async function testConnection() {
  const body = { HA_URL: CFG.HA_URL };
  const typed = (CFG.HA_TOKEN && CFG.HA_TOKEN !== "<set>" && CFG.HA_TOKEN !== "--clear--")
    ? CFG.HA_TOKEN : "";
  if (typed) body.HA_TOKEN = typed;
  $("connstatus").textContent = "testing…";
  try {
    const r = await fetch("/api/test-connection", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then((x) => x.json());
    $("connstatus").textContent = (r.ok ? "✔ " : "✖ ") + r.detail;
    $("connstatus").style.color = r.ok ? "var(--ok)" : "var(--bad)";
  } catch (e) {
    $("connstatus").textContent = "✖ " + e.message;
    $("connstatus").style.color = "var(--bad)";
  }
}
window.addEventListener("beforeunload", (e) => {
  if (dirty) { e.preventDefault(); e.returnValue = ""; }
});
reload()
  .then(() => { tick(); setInterval(tick, 1000); })
  .catch((e) => {
    $("loaderror").innerHTML =
      `<div class="banner">The settings page failed to start (${esc(e.message)}).`
      + " Reload the page; if it stays empty, check the server log.</div>";
    console.error(e);
  });
</script>
</body>
</html>
"""
GEOMETRY_REV = 0            # bumped when the light/zone map changes at runtime



def _spec(name, kind, default, group, label, help_text="", **kw):
    return {"name": name, "type": kind, "default": default, "group": group,
            "label": label, "help": help_text, **kw}


SETTINGS = [
    _spec("HA_URL", "string", "http://homeassistant.local:8123", "Home Assistant", "URL",
          "Base URL of your Home Assistant, e.g. http://homeassistant.local:8123"),
    _spec("HA_TOKEN", "secret", "", "Home Assistant", "Long-lived token",
          "Create one in Home Assistant: profile -> Security -> Long-lived access tokens. "
          "Stored in secrets.json (chmod 600), never sent back to the browser."),
    _spec("COLOR_ALGO", "enum", "dominant", "Colour", "Colour model",
          "mean = linear average of the zones. dominant = the most prominent coloured "
          "cluster, neutral/white cells ignored.", values=["mean", "dominant"]),
    _spec("DOMINANT_MIX", "float", 0.5, "Colour", "Dominant mix",
          "0 = pure average, 1 = pure dominant colour. Blended in u'v' (perceptually uniform).",
          min=0.0, max=1.0, step=0.05, show_if={"COLOR_ALGO": "dominant"}),
    _spec("DOMINANT_LUMINANCE", "enum", "cluster", "Colour", "Dominant brightness",
          "cluster = the dominant object's own luminance; region = the zone's mean. "
          "Only matters when dominant mix > 0.", values=["cluster", "region"], advanced=True, show_if={"COLOR_ALGO": "dominant"}),
    _spec("CHROMA_FLOOR", "float", 0.010, "Colour", "Neutral threshold",
          "du'v' below this counts as neutral when picking the dominant colour.",
          min=0.001, max=0.05, step=0.001, advanced=True, show_if={"COLOR_ALGO": "dominant"}),
    _spec("HUE_BINS", "int", 24, "Colour", "Hue bins",
          "Dominant mode's hue resolution (24 = 15° buckets, averaged with their "
          "neighbours). Lower merges similar hues; higher separates them but can "
          "flicker.",
          min=6, max=72, advanced=True, show_if={"COLOR_ALGO": "dominant"}),
    _spec("SATURATION_BOOST", "bool", False, "Colour", "Saturation boost",
          "Push chroma away from white as the frame gets brighter. Off sends the "
          "measured colour untouched."),
    _spec("SATURATION_GAIN", "float", 0.8, "Colour", "Boost strength",
          "k = 1 + gain * luminance^curve, capped at max. Only used when boost is on.",
          min=0.0, max=2.0, step=0.05, advanced=True, show_if={"SATURATION_BOOST": True}),
    _spec("SATURATION_CURVE", "float", 0.6, "Colour", "Boost curve",
          "Lower = the boost kicks in at darker frames.", min=0.2, max=2.0, step=0.05, advanced=True, show_if={"SATURATION_BOOST": True}),
    _spec("SATURATION_MAX", "float", 2.2, "Colour", "Boost cap",
          "Upper bound for k. Gamut clamping applies on top.", min=1.0, max=4.0, step=0.1, advanced=True, show_if={"SATURATION_BOOST": True}),
    _spec("SEND_MODE", "enum", "xy", "Colour", "Payload colour space",
          "xy = chromaticity (precise, recommended). rgb = legacy rgb_color.",
          values=["xy", "rgb"], advanced=True),
    _spec("SEND_BRIGHTNESS", "bool", True, "Brightness", "Send brightness",
          "Turn brightness updates off to leave the lamp's level alone."),
    _spec("BRIGHTNESS_FROM", "enum", "lstar", "Brightness", "Brightness source",
          "luminance = physical light, lstar = perceptual CIELAB, value = brightest channel "
          "(hue has no effect).", values=["luminance", "lstar", "value"]),
    _spec("BRIGHTNESS_GAMMA", "float", 0.8, "Brightness", "Brightness gamma",
          "Tone curve on the 0-1 input: <1 lifts dim frames, >1 crushes them.",
          min=0.3, max=2.5, step=0.05),

    _spec("ZONE_ROWS", "int", 3, "Capture", "Zone rows",
          "How many rows the screen is split into. Changing this re-splits every "
          "light's zones, so a mapping that uses zones beyond the new grid is rejected.",
          min=1, max=8),
    _spec("ZONE_COLS", "int", 3, "Capture", "Zone columns",
          "How many columns the screen is split into (3x3 by default, up to 8x8).",
          min=1, max=8),
    _spec("CAPTURE_FPS", "float", 12.0, "Capture", "Capture rate",
          "Screen samples per second. Sends are paced separately.",
          min=1.0, max=60.0, step=1.0),
    _spec("CAPTURE_STRIDE", "int", 4, "Capture", "Pixel stride",
          "Use every Nth pixel: 1 = lossless and expensive, 4 = 1/16 of the pixels.",
          min=1, max=16, restart=False, advanced=True),
    _spec("IDLE_FPS", "float", 4.0, "Capture", "Idle rate",
          "Sampling rate while the screen is unchanged.", min=0.5, max=30.0, step=0.5, advanced=True),
    _spec("IDLE_AFTER", "int", 3, "Capture", "Idle after",
          "Identical frames before dropping to the idle rate.", min=0, max=60, advanced=True),
    _spec("MONITOR_INDEX", "int", 1, "Capture", "Monitor",
          "Which screen to follow: 0 combines every monitor onto one canvas, 1 is "
          "the primary, 2+ are the others in the order mss reports them. Needs a restart.",
          min=0, max=8, restart=True, monitors=True),

    _spec("TRANSITION", "string", "auto", "Sending", "Transition",
          "'auto' matches the ramp to the achieved post interval; a number (seconds) "
          "pins it; 0 snaps.", advanced=True),
    _spec("POST_INTERVAL_FLOOR", "float", 0.15, "Sending", "Fastest pacing",
          "Minimum time between commands. Raise it if the Zigbee controller keeps resetting.",
          min=0.05, max=1.0, step=0.01),
    _spec("POST_INTERVAL_CEILING", "float", 0.6, "Sending", "Backoff ceiling",
          "Pacing is allowed to grow to this when the radio reports BUSY.",
          min=0.1, max=3.0, step=0.05),
    _spec("SEND_CONCURRENCY", "bool", True, "Sending", "Concurrent sends",
          "Issue each cycle's lights at once instead of waiting for each device ack.", advanced=True),
    _spec("MIN_DELTA_E", "float", 1.0, "Sending", "Colour deadband",
          "Skip a frame unless the colour moves this far (CIELAB dE76). Higher = fewer commands.",
          min=0.0, max=10.0, step=0.1, advanced=True),
    _spec("MIN_BRIGHTNESS_DELTA", "int", 4, "Sending", "Brightness deadband",
          "Skip a frame unless brightness moves this much (0-255).", min=0, max=64, advanced=True),
    _spec("RESEND_AFTER", "float", 3.0, "Sending", "Rescue interval",
          "Re-send an unconfirmed colour after this many seconds if nothing landed.",
          min=0.0, max=60.0, step=0.5, advanced=True),
    _spec("REQUEST_TIMEOUT", "float", 3.0, "Sending", "HA request timeout",
          "Seconds to wait for a command. A ZHA call answers only after the Zigbee exchange.",
          min=0.5, max=30.0, step=0.5, advanced=True),
    _spec("RADIO_PAUSE", "float", 2.0, "Sending", "Radio stall hold-off",
          "How long to stop sending after the coordinator reports a stall.",
          min=0.0, max=30.0, step=0.5, advanced=True),
    _spec("FAIL_THRESHOLD", "int", 3, "Sending", "Breaker threshold",
          "Consecutive host-level failures before pausing sends.", min=1, max=20, advanced=True),
    _spec("FAIL_COOLDOWN", "float", 10.0, "Sending", "Breaker cooldown",
          "Seconds to pause when Home Assistant is unreachable.", min=1.0, max=300.0, step=1.0, advanced=True),
    _spec("DRY_RUN", "bool", False, "Sending", "Dry run",
          "Log payloads instead of sending them."),
    _spec("LIGHT_MAP", "string", "", "Lights", "Light map",
          "entity=zones; entity=zones - zones are 1-9 in reading order, or 'all'."),
]
SETTINGS.extend([
    _spec("BIND_HOST", "string", "127.0.0.1", "Server", "Listen address",
          "Interface for the settings UI. 127.0.0.1 = this machine only; 0.0.0.0 = anyone "
          "who can reach the port, and the UI has no login. Needs a restart.", restart=True),
    _spec("CLOSE_TO_TRAY", "bool", True, "Server", "Close to tray",
          "Closing the window hides it and keeps mirroring; use the tray icon to "
          "reopen it or quit. Turn off to quit when the window closes."),
    _spec("START_WITH_WINDOWS", "bool", False, "Server", "Start with Windows",
          "Adds a login entry that starts the app in the tray. Windows only; takes "
          "effect as soon as you save.",),
    _spec("BIND_PORT", "int", 8000, "Server", "Listen port",
          "TCP port for the settings UI. Needs a restart.", min=1, max=65535, restart=True),
])

SETTINGS_BY_NAME = {s["name"]: s for s in SETTINGS}
SECRETS_PATH = Path(os.getenv("SECRETS_FILE") or CONFIG_PATH.with_name("secrets.json"))
def valid_bind_host(value: str) -> bool:
    """An address uvicorn can actually bind: wildcard, IP literal or hostname."""
    import ipaddress
    import re

    host = (value or "").strip()
    if not host or "://" in host or "/" in host or " " in host:
        return False
    if host in ("0.0.0.0", "::", "localhost"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        if re.fullmatch(r"[0-9.]+", host):
            return False        # looks like an IP but isn't one (999.999.999.999)
    return bool(re.fullmatch(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*",
                            host))


def _env_bind_host() -> str:
    raw = _env("BIND_HOST", "127.0.0.1")
    if valid_bind_host(raw):
        return raw
    print(f"BIND_HOST {raw!r} is not an address this machine can bind - using 127.0.0.1",
          file=sys.stderr)
    return "127.0.0.1"


def _env_bind_port() -> int:
    raw = _env("BIND_PORT", "8000")
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        print(f"BIND_PORT {raw!r} must be between 1 and 65535 - using 8000", file=sys.stderr)
        return 8000
    return port


CLOSE_TO_TRAY = _env("CLOSE_TO_TRAY", "1") == "1"
BIND_HOST = _env_bind_host()
BIND_PORT = _env_bind_port()
SECRET_SET = "<set>"
SECRET_UNSET = "<unset>"


TRUEISH = {"true", "1", "yes", "on"}
FALSEISH = {"false", "0", "no", "off"}


def _coerce(spec, value):
    """Type-check, range-check, then accept. Never guess at a bad value.

    A JSON null used to become False (so DRY_RUN could be switched off by a
    null), a bool became 1.0, and out-of-range numbers were clamped and reported
    as applied. Silent conversion is how a safety switch flips without anyone
    meaning it to.
    """
    kind = spec["type"]
    if kind == "enum":
        if value not in spec["values"]:
            raise ValueError("must be one of " + ", ".join(spec["values"]))
        return value
    if kind == "secret":
        return "" if value is None else str(value).strip()
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in TRUEISH | FALSEISH:
            return value.strip().lower() in TRUEISH
        raise ValueError("must be true or false")
    if kind in ("int", "float"):
        if isinstance(value, bool) or value is None:
            raise ValueError("must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None
        if not math.isfinite(number):
            raise ValueError("must be a finite number")
        out = int(round(number)) if kind == "int" else number
        low, high = spec.get("min"), spec.get("max")
        if low is not None and out < low:
            raise ValueError(f"must be at least {low}")
        if high is not None and out > high:
            raise ValueError(f"must be at most {high}")
        return out
    return "" if value is None else str(value)


def settings_snapshot() -> dict:
    """Current settings. Secrets are reported as set/unset, never as values."""
    out = {}
    for spec in SETTINGS:
        value = globals().get(spec["name"], spec["default"])
        if spec["type"] == "secret":
            out[spec["name"]] = SECRET_SET if value else SECRET_UNSET
        else:
            out[spec["name"]] = value
    return out


def persist_secrets() -> bool:
    """Write the token to its own 0600 file so config.json stays shareable."""
    try:
        if HA_TOKEN:
            SECRETS_PATH.write_text(json.dumps({"HA_TOKEN": HA_TOKEN}) + "\n")
            try:
                os.chmod(SECRETS_PATH, 0o600)
            except OSError:                     # Windows / mounted fs
                pass
        elif SECRETS_PATH.exists():
            SECRETS_PATH.unlink()
        return True
    except OSError as exc:
        log.warning("could not write %s: %s", SECRETS_PATH, exc)
        return False


def load_secrets() -> None:
    global HA_TOKEN
    if not SECRETS_PATH.exists():
        return
    try:
        data = json.loads(SECRETS_PATH.read_text())
    except (OSError, ValueError) as exc:
        log.error("ignoring %s: %s", SECRETS_PATH, exc)
        return
    token = str(data.get("HA_TOKEN") or "").strip()
    if token:
        HA_TOKEN = token
        log.info("loaded the Home Assistant token from %s", SECRETS_PATH)


def persist_config(path: Path | None = None) -> bool:
    path = path or CONFIG_PATH
    snapshot = settings_snapshot()
    PERSISTED.clear()
    PERSISTED.update(snapshot)
    data = {name: value for name, value in snapshot.items()
            if SETTINGS_BY_NAME.get(name, {}).get("type") != "secret"}
    try:
        path.write_text(json.dumps(data, indent=2) + "\n")
        return True
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
        return False


def apply_settings(payload: dict, persist: bool = True) -> tuple[dict, list[str]]:
    """Validate, apply at runtime and (optionally) persist. Never partial-applies."""
    global TARGETS, GEOMETRY_REV
    if not isinstance(payload, dict):
        return {}, ["payload must be an object"]
    new: dict = {}
    errors: list[str] = []
    for name, raw in payload.items():
        spec = SETTINGS_BY_NAME.get(name)
        if spec is None:
            errors.append(f"{name}: not a setting")
            continue
        try:
            new[name] = _coerce(spec, raw)
        except (TypeError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
    if {"ZONE_ROWS", "ZONE_COLS"} & set(new):
        rows = int(new.get("ZONE_ROWS", ZONE_ROWS))
        cols = int(new.get("ZONE_COLS", ZONE_COLS))
        limit = rows * cols
        overflow = sorted({z for t in TARGETS for z in t.zones if z > limit})
        if overflow:
            errors.append(f"ZONE_ROWS/ZONE_COLS: {limit} zones, but the light mapping "
                          f"uses zone {max(overflow)} - change the mapping first")
            new.pop("ZONE_ROWS", None)
            new.pop("ZONE_COLS", None)
    if "TRANSITION" in new:
        raw = str(new["TRANSITION"]).strip()
        if raw.lower() == "auto":
            new["TRANSITION"] = "auto"
        else:
            try:
                seconds = float(raw)
            except (TypeError, ValueError):
                errors.append('TRANSITION: must be "auto" or a number of seconds')
                new.pop("TRANSITION")
            else:
                if not 0.0 <= seconds <= 5.0:
                    errors.append("TRANSITION: must be between 0 and 5 seconds")
                    new.pop("TRANSITION")
                else:
                    new["TRANSITION"] = raw
    if "BIND_HOST" in new:
        host = str(new["BIND_HOST"]).strip()
        if not valid_bind_host(host):
            errors.append("BIND_HOST: an address such as 127.0.0.1, 0.0.0.0 or a hostname")
            new.pop("BIND_HOST")
        else:
            new["BIND_HOST"] = host
    if "LIGHT_MAP" in new:
        try:
            parse_light_map(new["LIGHT_MAP"])
        except ValueError as exc:
            errors.append(f"LIGHT_MAP: {exc}")
            new.pop("LIGHT_MAP")
    if errors:
        return {}, errors

    token_changed = False
    if "HA_TOKEN" in new:
        supplied = new["HA_TOKEN"]
        if supplied in ("", SECRET_SET, SECRET_UNSET):
            new.pop("HA_TOKEN")                 # blank = leave the saved token alone
        elif supplied == "--clear--":
            new["HA_TOKEN"] = ""
            token_changed = True
        else:
            token_changed = True
    if "HA_URL" in new:
        new["HA_URL"] = str(new["HA_URL"]).rstrip("/")

    for name, value in new.items():
        globals()[name] = value
    if "START_WITH_WINDOWS" in new:
        ok, message = set_autostart(bool(new["START_WITH_WINDOWS"]))
        AUTOSTART_RESULT.clear()
        AUTOSTART_RESULT.update({"ok": ok, "detail": message})
        log.info("autostart: %s", message)
    if {"ZONE_ROWS", "ZONE_COLS"} & set(new):
        rebuild_zone_index()
        if isinstance(LIGHT_MAP, str) and LIGHT_MAP:
            TARGETS = parse_light_map(LIGHT_MAP)
        GEOMETRY_REV += 1
    if token_changed:
        persist_secrets()
    if {"HA_URL", "HA_TOKEN"} & set(new):
        # the picker must not keep serving the previous instance's lights
        LIGHTS_CACHE.update({"at": 0.0, "data": None})
    if "LIGHT_MAP" in new:
        TARGETS = parse_light_map(new["LIGHT_MAP"])
        GEOMETRY_REV += 1
    if persist:
        persist_config()
    return new, []


CONFIG_LOADED = False


def load_config_file(path: Path | None = None) -> None:
    global CONFIG_LOADED
    path = path or CONFIG_PATH
    CONFIG_LOADED = True                  # one attempt, one log line
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        log.error("ignoring %s: %s", path, exc)
        return
    if not isinstance(data, dict):
        log.error("ignoring %s: the top level must be an object", path)
        CONFIG_LOADED = True
        return
    unknown = sorted(set(data) - set(SETTINGS_BY_NAME))
    if unknown:
        # one typo used to discard every other setting in the file
        log.warning("ignoring unknown key(s) in %s: %s", path, ", ".join(unknown))
    known = {k: v for k, v in data.items() if k in SETTINGS_BY_NAME}
    applied, errors = apply_settings(known, persist=False)
    if errors:
        log.error("config problems in %s: %s", path, "; ".join(errors))
    CONFIG_LOADED = True
    PERSISTED.clear()
    PERSISTED.update(settings_snapshot())
    if applied:
        log.info("loaded %d setting(s) from %s", len(applied), path)
    else:
        log.warning("nothing usable in %s - running on defaults", path)



# ------------------------------------------------------------- shared state -
latest_data = {"raw_grid": [], "regions": {}, "lights": {}}
stop_event = threading.Event()
state_lock = threading.Lock()

TARGETS: list[Target] = []
stats = {"posts": 0, "frames": 0, "dropped": 0, "rtt_ms": None, "interval_ms": None,
         "transition": None, "errors": 0, "busy": 0, "pace_ms": None,
         "age_ms": None, "max_age_ms": None, "saturation_k": None,
         "prep_ms": None, "idle": False, "cycle_ms": None, "crop": None,
         "timeouts": 0, "radio_stalls": 0, "frame_errors": 0,
         "last_frame_error": None,
         "calibrating": False, "calibration": None,
         "autotuning": False, "autotune": None}
_last_error_log = 0.0

session = requests.Session()
session.headers.update({"Content-Type": "application/json"})


def auth_headers() -> dict:
    """Built per request: the token can change at runtime from the UI."""
    return {"Authorization": f"Bearer {HA_TOKEN}"} if HA_TOKEN else {}


# ------------------------------------------------------------- colour math --
def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = np.asarray(c, dtype=np.float32)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(np.asarray(c, dtype=np.float32), 0.0, 1.0)
    out = np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)
    return np.clip(out, 0.0, 1.0)


def _linear_to_xyz(lin) -> tuple[float, float, float]:
    r, g, b = (float(v) for v in lin)
    x = 0.4124 * r + 0.3576 * g + 0.1805 * b
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = 0.0193 * r + 0.1192 * g + 0.9505 * b
    return x, y, z


def xy_from_linear(lin) -> tuple[float, float]:
    """Linear sRGB -> CIE 1931 xy (sRGB D65 matrix, not HA's wide-gamut one)."""
    x, y, z = _linear_to_xyz(lin)
    total = x + y + z
    if total <= 1e-9:
        return D65_XY
    return x / total, y / total


def xy_from_rgb(rgb8) -> tuple[float, float]:
    return xy_from_linear(_srgb_to_linear(np.asarray(rgb8, dtype=np.float32) / 255.0))


def srgb_from_linear(lin) -> list[int]:
    return [int(round(v * 255)) for v in _linear_to_srgb(np.asarray(lin, dtype=np.float32))]


def _linear_rgb_from_xy(xy, level: float = 1.0) -> tuple[float, float, float]:
    x, y = xy
    if y <= 1e-6:
        return 0.0, 0.0, 0.0
    big_x = x * level / y
    big_y = level
    big_z = (1 - x - y) * level / y
    return (3.2406 * big_x - 1.5372 * big_y - 0.4986 * big_z,
            -0.9689 * big_x + 1.8758 * big_y + 0.0415 * big_z,
            0.0557 * big_x - 0.2040 * big_y + 1.0570 * big_z)


def srgb_from_xy(xy, level: float = 1.0) -> list[int]:
    """CIE xy -> sRGB 8-bit via the exact inverse of the sRGB matrix above."""
    return srgb_from_linear(_linear_rgb_from_xy(xy, level))


def _in_srgb_gamut(xy) -> bool:
    """True when xy can be shown by an sRGB display without a negative channel."""
    return min(_linear_rgb_from_xy(xy)) >= -1e-4


def _xy_to_uv(xy) -> tuple[float, float]:
    x, y = xy
    d = -2 * x + 12 * y + 3
    return 4 * x / d, 9 * y / d


def _uv_to_xy(uv) -> tuple[float, float]:
    u, v = uv
    d = 6 * u - 16 * v + 12
    if abs(d) < 1e-9:
        return D65_XY
    return 9 * u / d, 4 * v / d


def _neo(xy_a, xy_b) -> float:
    """CIE 1976 u'v' distance - the standard measure of chromaticity error."""
    u1, v1 = _xy_to_uv(xy_a)
    u2, v2 = _xy_to_uv(xy_b)
    return float(np.hypot(u1 - u2, v1 - v2))


def _cells_uv(cells_lin: np.ndarray):
    """Linear RGB cells -> (u', v', chroma distance from white) in uniform space."""
    r, g, b = cells_lin[:, 0], cells_lin[:, 1], cells_lin[:, 2]
    big_x = 0.4124 * r + 0.3576 * g + 0.1805 * b
    big_y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    big_z = 0.0193 * r + 0.1192 * g + 0.9505 * b
    total = big_x + big_y + big_z
    ok = total > 1e-6
    if not ok.any():
        return None
    x = big_x[ok] / total[ok]
    y = big_y[ok] / total[ok]
    d = -2 * x + 12 * y + 3
    u = 4 * x / d
    v = 9 * y / d
    wu, wv = _xy_to_uv(D65_XY)
    # `kept` is essential: the masks below are built on this filtered array, so
    # anything indexing the original cells must go through these indices. Getting
    # this wrong raised "boolean index did not match indexed array" the moment a
    # region contained fully black cells (letterboxing, a black window, dark video).
    return u, v, np.hypot(u - wu, v - wv), np.flatnonzero(ok)


def dominant_colour(cells_lin: np.ndarray, fallback) -> tuple[tuple[float, float], np.ndarray | None]:
    """Chroma-weighted peak-hue cluster: the screen's actual colour, not its wash.

    Returns the chromaticity and the cluster's own mean linear RGB, so brightness
    can follow the object rather than the whole region.
    """
    data = _cells_uv(cells_lin)
    if data is None:
        return fallback, None
    u, v, chroma, kept = data
    wu, wv = _xy_to_uv(D65_XY)
    coloured = chroma >= CHROMA_FLOOR
    if not coloured.any():
        return fallback, None
    u, v, chroma = u[coloured], v[coloured], chroma[coloured]

    hue = np.arctan2(v - wv, u - wu)
    bins = np.floor((hue + np.pi) / (2 * np.pi) * HUE_BINS).astype(int) % HUE_BINS
    scores = np.bincount(bins, weights=chroma, minlength=HUE_BINS)
    peak = int(np.argmax(scores))
    # include the neighbours so a hue straddling a bin edge is not split
    neighbours = [(peak - 1) % HUE_BINS, peak, (peak + 1) % HUE_BINS]
    sel = np.isin(bins, neighbours)
    weights = chroma[sel]
    if not sel.any() or weights.sum() <= 0:
        return fallback, None
    pu = float((u[sel] * weights).sum() / weights.sum())
    pv = float((v[sel] * weights).sum() / weights.sum())
    cluster = cells_lin[kept][coloured][sel].mean(axis=0)   # back through the indices
    return _uv_to_xy((pu, pv)), cluster


def saturation_factor(luminance: float) -> float:
    if not SATURATION_BOOST or SATURATION_GAIN <= 0:
        return 1.0
    lum = max(0.0, min(1.0, luminance))
    return min(SATURATION_MAX, 1.0 + SATURATION_GAIN * (lum ** SATURATION_CURVE))


def valid_xy(xy) -> bool:
    """Is this a chromaticity Home Assistant will accept?

    Boosting a saturated colour can step outside the CIE xy domain (y <= 0),
    which real HA rejects with 400 on every frame, and which converts to
    rgb 0,0,0 in rgb mode - a black light instead of a blue one.
    """
    try:
        x, y = float(xy[0]), float(xy[1])
    except (TypeError, ValueError, IndexError):
        return False
    return 0.0 <= x <= 1.0 and 0.0 < y <= 1.0 and (x + y) <= 1.0


def boost_saturation(xy, luminance: float) -> tuple[tuple[float, float], float]:
    """Push chromaticity away from D65 along its own hue, clipped to sRGB."""
    k = saturation_factor(luminance)
    if k <= 1.0:
        return xy, 1.0
    u, v = _xy_to_uv(xy)
    wu, wv = _xy_to_uv(D65_XY)
    du, dv = u - wu, v - wv
    # Below roughly one just-noticeable step of chroma the offset is numerical
    # noise (and amplifying it would tint a white screen at random).
    if _neo(xy, D65_XY) < 0.002:
        return xy, 1.0

    target = _uv_to_xy((wu + k * du, wv + k * dv))
    if _in_srgb_gamut(target) and valid_xy(target):
        return target, k
    lo, hi = 1.0, k
    best = xy
    for _ in range(14):
        mid = (lo + hi) / 2.0
        cand = _uv_to_xy((wu + mid * du, wv + mid * dv))
        if _in_srgb_gamut(cand) and valid_xy(cand):
            best, lo = cand, mid
        else:
            hi = mid
    return best, max(1.0, lo)


def lab_from_srgb(rgb8) -> tuple[float, float, float]:
    x, y, z = _linear_to_xyz(_srgb_to_linear(np.asarray(rgb8, dtype=np.float32) / 255.0))

    def f(t: float) -> float:
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = f(x / WN[0]), f(y / WN[1]), f(z / WN[2])
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(rgb_a, rgb_b) -> float:
    la, aa, ba = lab_from_srgb(rgb_a)
    lb, ab, bb = lab_from_srgb(rgb_b)
    return float(np.sqrt((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2))


def aggregate(cells_lin: np.ndarray) -> dict:
    """One region's (or one light's zones') colour, averaged in linear light."""
    mean_lin = cells_lin.reshape(-1, 3).mean(axis=0)
    x, y = xy_from_linear(mean_lin)
    out = {
        "rgb": srgb_from_linear(mean_lin),
        "xy": [round(x, 4), round(y, 4)],
        "luminance": round(float(_linear_to_xyz(mean_lin)[1]), 4),
    }
    if COLOR_ALGO == "dominant":
        (dx, dy), cluster_lin = dominant_colour(cells_lin.reshape(-1, 3), (x, y))
        if DOMINANT_MIX < 1.0:
            mu, mv = _xy_to_uv((x, y))
            du, dv = _xy_to_uv((dx, dy))
            blend = min(1.0, max(0.0, DOMINANT_MIX))
            dx, dy = _uv_to_xy((mu + blend * (du - mu), mv + blend * (dv - mv)))
        out["dominant"] = [round(dx, 4), round(dy, 4)]
        # The brightness/dedupe decision has to look at the colour we actually
        # send, not the region mean, or the two disagree (a vivid block on a dark
        # page kept the lamp dim, and hue changes could pass the dE test).
        level = out["luminance"]
        if DOMINANT_LUMINANCE == "cluster" and cluster_lin is not None:
            cluster_y = float(_linear_to_xyz(cluster_lin)[1])
            if DOMINANT_MIX < 1.0:          # blend the brightness with the mix too
                blend = min(1.0, max(0.0, DOMINANT_MIX))
                cluster_y = level + blend * (cluster_y - level)
            out["dominant_luminance"] = round(cluster_y, 4)
            level = cluster_y
        out["rgb_dominant"] = srgb_from_xy((dx, dy), max(level, 1e-4))
    return out


def zones_cells(lin: np.ndarray) -> list[np.ndarray]:
    """Flat (N, 3) cells per zone.

    The 3x3 split is ragged - 64 columns become 22/21/21 and 36 rows become
    12/12/12 - so a zone spanning two column groups has a different width from
    its neighbour. Stitching those blocks together only works once each block is
    flattened to a list of cells.
    """
    return [lin[np.ix_(r, c)].reshape(-1, 3) for r, c in ZONE_INDEX]


def cells_for(target: Target, lin: np.ndarray, zone_cells: list[np.ndarray]) -> np.ndarray:
    if len(target.zones) == ZONE_COUNT:
        return lin.reshape(-1, 3)
    return np.concatenate([zone_cells[z - 1] for z in sorted(target.zones)], axis=0)


def brightness_for_luminance(lum: float) -> int:
    """Relative luminance 0..1 -> 1-255 brightness (0 = off in HA, so floor at 1)."""
    return int(round(max(1.0, min(255.0, 255.0 * max(0.0, lum) ** BRIGHTNESS_GAMMA))))


def lstar_from_luminance(y: float) -> float:
    """Relative luminance -> CIELAB L* (0-100): perceptual, not photometric."""
    t = max(0.0, min(1.0, y))
    f = t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29
    return 116.0 * f - 16.0


def brightness_channel(colour: dict) -> float:
    """The 0..1 quantity that BRIGHTNESS_GAMMA is applied to."""
    if BRIGHTNESS_FROM == "value":
        return max(colour["rgb"]) / 255.0
    if BRIGHTNESS_FROM == "lstar":
        return lstar_from_luminance(colour["luminance"]) / 100.0
    return colour["luminance"]


def brightness_for_colour(colour: dict) -> int:
    return brightness_for_luminance(brightness_channel(colour))


def should_send(target: Target, colour: dict, brightness: int) -> bool:
    """Decide from the screen, never from a retry queue."""
    last = target.last_attempted
    if last is None:
        return True
    d_e = delta_e(colour["rgb"], last[0])
    if d_e >= MIN_DELTA_E:
        return True
    # With SEND_BRIGHTNESS off the payload carries no brightness, so the last
    # attempt recorded None: comparing against it raised TypeError on every
    # frame. Nothing to compare means there is nothing brightness could change.
    if last[1] is not None and abs(brightness - last[1]) >= MIN_BRIGHTNESS_DELTA:
        return True
    # Same colour as the last attempt. Re-send only when that attempt is still
    # unconfirmed, and never sooner than RESEND_AFTER after the attempt itself:
    # timing from last_success_mono re-queued every frame while the very first
    # POST was still in flight (a static screen then posted twice).
    if target.last_sent == last:
        return False
    return time.perf_counter() - target.last_attempt_mono >= RESEND_AFTER


def current_transition() -> float:
    if TRANSITION != "auto":
        try:
            return max(0.0, float(TRANSITION))
        except (TypeError, ValueError):
            # a bad value used to raise here, inside the sender thread, which
            # then died for good and stopped driving the lights
            log.error("TRANSITION %r is not a number - using auto", TRANSITION)
    interval = stats["interval_ms"]
    if interval is None:
        return MIN_TRANSITION
    return max(MIN_TRANSITION, min(MAX_TRANSITION, interval / 1000.0 * 1.15))


def preview_colour(colour: dict) -> dict:
    """What the bulb will actually show: the same transform the payload uses.

    The UI draws the 3x3 grid and the per-light chips from this, so what you see
    is the post-boost, post-clamp colour at the brightness that gets sent - not
    the raw average of the screen.
    """
    base = tuple(colour["xy"])
    xy, k = boost_saturation(base, colour["luminance"])
    if not valid_xy(xy):
        xy, k = (base if valid_xy(base) else D65_XY), 1.0
    brightness = brightness_for_colour(colour) if SEND_BRIGHTNESS else 255
    rgb = srgb_from_xy(xy, max(brightness / 255.0, 1e-4))
    return {
        "xy": [round(xy[0], 4), round(xy[1], 4)],
        "brightness": brightness,
        "rgb": list(rgb),
        "hex": "#%02x%02x%02x" % tuple(rgb),
        "saturation_k": round(k, 2),
    }


def build_payload(colour: dict) -> dict:
    """Service data for one light (no entity_id, so identical ones can merge)."""
    payload: dict = {}
    preview = preview_colour(colour)
    xy = tuple(preview["xy"])
    stats["saturation_k"] = preview["saturation_k"]
    if SEND_MODE == "rgb":
        rgb = srgb_from_xy(xy, max(colour["luminance"], 1e-4))
        peak = max(max(rgb), 1)          # HA reads chromaticity from rgb_color
        scaled = [int(round(c * 255.0 / peak)) for c in rgb]
        payload["rgb_color"] = scaled if max(scaled) > 0 else [255, 255, 255]
    else:
        payload["xy_color"] = [round(xy[0], 4), round(xy[1], 4)]
    if SEND_BRIGHTNESS:
        payload["brightness"] = preview["brightness"]
    transition = current_transition()
    if transition > 0:
        payload["transition"] = round(transition, 2)
    return payload


# ------------------------------------------------------------- send thread --
class Mailbox:
    """Single slot, newest wins: the sender never processes a stale frame."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._item = None
        self._closed = False

    def put(self, item) -> bool:
        with self._cv:
            if self._closed:
                return False
            dropped = self._item is not None
            self._item = item
            self._cv.notify()
            return dropped

    def get(self, timeout: float = 0.5):
        with self._cv:
            if self._item is None:
                self._cv.wait(timeout)
            item, self._item = self._item, None
            return item

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()


mailbox = Mailbox()


_local = threading.local()


def _session_local() -> requests.Session:
    """One keep-alive session per thread: requests.Session is not thread-safe."""
    sess = getattr(_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({"Content-Type": "application/json"})
        _local.session = sess
    return sess


def _post(body: dict, timeout: float | None = None) -> requests.Response:
    return _session_local().post(f"{HA_URL}/api/services/light/turn_on", json=body,
                                 headers=auth_headers(),
                                 timeout=REQUEST_TIMEOUT if timeout is None else timeout)


def _safe_post(body: dict) -> tuple[str, object]:
    try:
        return "ok", _post(body)
    except requests.RequestException as exc:
        return "exc", exc


def post_concurrently(bodies: list[dict]) -> list[tuple[str, object]]:
    """Issue every light's command at once instead of waiting for each device ack.

    ZHA answers the HTTP call only after the Zigbee device replies (~114 ms for a
    single spotlight vs ~30 ms for the group), so a serial loop made each cycle
    the SUM of the lights' acks. On a ZHA mesh with three lights, 351 ms -> 214 ms
    per cycle.
    """
    results: list = [None] * len(bodies)

    def run(index: int, body: dict) -> None:
        results[index] = _safe_post(body)

    threads = [threading.Thread(target=run, args=(i, b), name=f"post{i}", daemon=True)
               for i, b in enumerate(bodies)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    return [r if r is not None else ("exc", requests.RequestException("send timeout")) for r in results]




def group_payloads(payloads: dict[str, dict]) -> list[tuple[dict, list[str]]]:
    """Lights with identical service data share one API call."""
    groups: dict[tuple, list[str]] = defaultdict(list)
    for entity, payload in payloads.items():
        key = (tuple(payload.get("xy_color") or ()), tuple(payload.get("rgb_color") or ()),
               payload.get("brightness"), payload.get("transition"))
        groups[key].append(entity)
    out = []
    for _key, entities in groups.items():
        body = dict(payloads[entities[0]])          # identical by construction
        body["entity_id"] = entities[0] if len(entities) == 1 else list(entities)
        out.append((body, entities))
    return out


def sender_loop() -> None:
    """Post the newest colours at the rate the Zigbee radio can actually take."""
    try:
        _sender_loop()
    except Exception:                     # never lose the sender for good
        log.exception("sender loop stopped unexpectedly")
        stats["send_errors"] = stats.get("send_errors", 0) + 1


def _sender_loop() -> None:
    """Where the work happens; wrapped above so the thread cannot die."""
    global _last_error_log

    pace = FIXED_POST_INTERVAL if FIXED_POST_INTERVAL > 0 else max(
        POST_INTERVAL_FLOOR, 1.0 / CAPTURE_FPS)
    next_slot = 0.0
    hold_until = 0.0
    streak = 0
    fail_streak = 0
    blocked_until = 0.0
    last_probe = 0.0

    def note_failure() -> None:
        """Host-level breaker: HA refused the connection or is truly down."""
        nonlocal fail_streak, blocked_until
        fail_streak += 1
        if fail_streak >= FAIL_THRESHOLD and blocked_until <= time.perf_counter():
            blocked_until = time.perf_counter() + FAIL_COOLDOWN
            log.warning("Home Assistant unreachable (%d failures), pausing sends for %.0f s",
                        fail_streak, FAIL_COOLDOWN)

    def note_radio_stall(reason: str) -> None:
        """Zigbee-side stall: the host is fine, the coordinator is not.

        A read timeout on a light command means ZHA never finished the Zigbee
        exchange (bellows NCP reset, "ApplicationController is not running",
        "device did not respond"). HA itself is healthy - the GET probe right
        afterwards succeeds - so this must NOT trip the host breaker; back off
        and let the controller come back instead of hammering it.
        """
        global _last_error_log
        nonlocal pace, hold_until
        stats["radio_stalls"] += 1
        pace = min(POST_INTERVAL_CEILING, max(pace * 1.3, POST_INTERVAL_FLOOR))
        hold_until = time.perf_counter() + RADIO_PAUSE
        now = time.time()
        if now - _last_error_log > 30:
            _last_error_log = now
            log.warning("Zigbee controller stall (%s) - holding off %.1fs",
                        reason or "no detail", RADIO_PAUSE)

    while not stop_event.is_set():
        # --- host breaker: hold off, probe cheaply, resume when it answers ---
        if blocked_until > time.perf_counter():
            stop_event.wait(min(0.5, blocked_until - time.perf_counter()))
            if time.perf_counter() - last_probe > 2.0:
                last_probe = time.perf_counter()
                try:
                    recovered = session.get(f"{HA_URL}/api/", headers=auth_headers(), timeout=2).ok
                except requests.RequestException:
                    recovered = False
                if recovered:
                    blocked_until = 0.0
                    fail_streak = 0
                    next_slot = 0.0
                    log.info("Home Assistant reachable again, resuming")
            continue

        delay = next_slot - time.perf_counter()
        if delay > 0:
            stop_event.wait(delay)
        item = mailbox.get(0.2)
        if item is None:
            continue

        payloads: dict[str, dict] = {}
        ages = []
        for entity, colour in item.items():
            ages.append((time.perf_counter() - colour.get("measured_at", time.perf_counter())) * 1000.0)
            try:
                payloads[entity] = build_payload(colour)
            except Exception as exc:          # one bad colour must not kill the thread
                stats["send_errors"] = stats.get("send_errors", 0) + 1
                log.error("Could not build a payload for %s: %s", entity, exc)
        age_ms = min(ages) if ages else 0.0
        stats["age_ms"] = round(age_ms, 1) if stats["age_ms"] is None else round(
            0.8 * stats["age_ms"] + 0.2 * age_ms, 1)
        stats["max_age_ms"] = round(max(stats["max_age_ms"] or 0.0, age_ms), 1)

        by_entity = {t.entity_id: t for t in TARGETS}
        groups = group_payloads(payloads)
        for body, entities in groups:                 # stamp attempts before sending
            for entity in entities:
                target = by_entity.get(entity)
                if target is not None:
                    target.last_attempted = (tuple(item[entity]["rgb"]), body.get("brightness"))

        if DRY_RUN:
            for body, entities in groups:
                log.info("DRY_RUN %s -> %s (age %.0f ms)", entities, body, age_ms)
                for entity in entities:
                    target = by_entity.get(entity)
                    if target is not None:
                        target.last_sent = target.last_attempted
                        target.last_success_mono = time.perf_counter()
            continue

        started = time.perf_counter()
        if SEND_CONCURRENCY and len(groups) > 1:
            results = post_concurrently([body for body, _ in groups])
        else:
            results = [_safe_post(body) for body, _ in groups]
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        stats["cycle_ms"] = round(elapsed_ms, 1)
        stats["rtt_ms"] = round(elapsed_ms / max(1, len(groups)), 1)

        for (body, entities), (kind, outcome) in zip(groups, results):
            if kind == "exc":
                if isinstance(outcome, (requests.ConnectTimeout, requests.ConnectionError)):
                    stats["errors"] += 1
                    streak = 0
                    for entity in entities:
                        target = by_entity.get(entity)
                        if target is not None:
                            target.errors += 1
                    note_failure()
                    now = time.time()
                    if now - _last_error_log > 30:
                        _last_error_log = now
                        log.error("Home Assistant not reachable at %s: %s", HA_URL, outcome)
                else:
                    # ReadTimeout & friends: HA answered other requests, so this
                    # is the radio, not the host.
                    stats["timeouts"] += 1
                    streak = 0
                    for entity in entities:
                        target = by_entity.get(entity)
                        if target is not None:
                            target.errors += 1
                    note_radio_stall("read timeout after %.0f ms" % (REQUEST_TIMEOUT * 1000))
                continue

            response = outcome
            if response.ok:
                stats["posts"] += 1
                streak += 1
                fail_streak = 0
                stats["transition"] = body.get("transition")
                now_mono = time.perf_counter()
                for entity in entities:
                    target = by_entity.get(entity)
                    if target is None:
                        continue
                    # Track the refresh interval PER LIGHT: with several lights
                    # in one cycle each one updates every (cycle) seconds, so a
                    # post-to-post average would make the ramps too short.
                    if target.last_success_mono > 0:
                        per_light_ms = (now_mono - target.last_success_mono) * 1000.0
                        if per_light_ms < 10000:
                            prev = stats["interval_ms"]
                            stats["interval_ms"] = round(
                                per_light_ms if prev is None else 0.7 * prev + 0.3 * per_light_ms, 1)
                    target.posts += 1
                    target.last_sent = target.last_attempted
                    target.last_success_mono = now_mono
                if streak >= 3 and FIXED_POST_INTERVAL <= 0:
                    pace = max(max(POST_INTERVAL_FLOOR, 1.0 / CAPTURE_FPS), pace * 0.95)
            elif is_queue_busy(response.text):
                # Queue full: the frame was dropped, never re-queued. Pace back
                # off, but no hold-off - the radio is working, just saturated.
                stats["busy"] += 1
                streak = 0
                for entity in entities:
                    target = by_entity.get(entity)
                    if target is not None:
                        target.busy += 1
                        # definitely not applied: let the next frame retry at once
                        target.last_attempted = target.last_sent
                pace = min(POST_INTERVAL_CEILING, pace * 1.3)
                now = time.time()
                if now - _last_error_log > 30:
                    _last_error_log = now
                    log.warning("Zigbee queue busy (%.0f ms pace), backing off", pace * 1000)
            elif is_radio_stall(response.text):
                # Coordinator restarting/unreachable: hold off rather than push.
                streak = 0
                for entity in entities:
                    target = by_entity.get(entity)
                    if target is not None:
                        target.busy += 1
                note_radio_stall(response.text.strip()[:120])
            else:
                stats["errors"] += 1
                streak = 0
                for entity in entities:
                    target = by_entity.get(entity)
                    if target is not None:
                        target.errors += 1
                        target.last_attempted = target.last_sent   # refused: retry freely
                note_failure()
                now = time.time()
                if now - _last_error_log > 30:
                    _last_error_log = now
                    log.error("HA API %s: %s", response.status_code, response.text[:300])

        stats["pace_ms"] = round(pace * 1000.0, 1)
        # Pace start-to-start (a slow round-trip eats the slot) and honour any
        # hold-off a radio stall asked for.
        next_slot = max(started + pace, hold_until)


def send_to_ha(batch: dict[str, dict]) -> None:
    """Hand a batch of {entity: colour} to the sender."""
    if mailbox.put(batch):
        stats["dropped"] += 1


def restore_light(restore: dict, attempts: int = 3) -> bool:
    """Put a light back the way we found it, retrying a dropped Zigbee command."""
    if len(restore) <= 1:
        return True
    restore = {**restore, "transition": 0}   # a ramp makes the group drop mode changes
    for _ in range(attempts):
        _post(restore)
        time.sleep(0.6)
        try:
            attrs = session.get(f"{HA_URL}/api/states/{restore['entity_id']}", headers=auth_headers(), timeout=5).json().get("attributes", {})
        except requests.RequestException:
            continue
        # accept the mode that matches what we asked for: an rgb restore reports
        # "rgb", not "xy", and used to look like a failure and be retried 3x
        if "xy_color" in restore:
            expected = {"xy"}
        elif "rgb_color" in restore:
            expected = {"rgb", "rgbw", "rgbww", "rgbx", "hs", "xy"}
        elif "color_temp_kelvin" in restore or "kelvin" in restore:
            expected = {"color_temp"}
        else:
            expected = None                      # brightness-only: nothing to compare
        mode = attrs.get("color_mode")
        if expected is None or mode in expected:
            return True
    log.warning("Light may not have been fully restored to %s", restore)
    return False


def check_ha() -> bool:
    if not HA_TOKEN:
        log.error("HA_TOKEN is empty - create one in HA: profile -> Security -> Long-lived access tokens")
        return False
    try:
        response = session.get(f"{HA_URL}/api/", headers=auth_headers(), timeout=5)
    except requests.RequestException as exc:
        log.error("Could not reach Home Assistant at %s: %s", HA_URL, exc)
        return False
    if response.status_code != 200:
        log.error("HA auth failed (%s) - token invalid or expired", response.status_code)
        return False
    log.info("Connected to Home Assistant at %s", HA_URL)
    return True


# --------------------------------------------------------------- calibrate --
CALIBRATION_SET = [
    [255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 0], [0, 255, 255], [255, 0, 255],
    [255, 128, 0], [200, 60, 40], [120, 140, 200], [64, 96, 64], [255, 255, 255], [90, 90, 110],
]


CALIBRATION_LOCK = threading.Lock()


AUTOTUNE_LOCK = threading.Lock()
POST_INTERVAL_FLOOR_MIN = 0.10      # fastest the tuner will allow
POST_INTERVAL_CEILING_MAX = 2.00
AUTOTUNE_STEPS = (0.45, 0.35, 0.26, 0.20, 0.15, 0.12, 0.09, 0.07)


AUTOTUNE_COLOURS = ([255, 190, 110], [120, 180, 255], [255, 120, 150],
                     [150, 255, 150], [230, 230, 230])


def _autotune_attrs(targets) -> list[dict]:
    """What each light is showing now, so it can be put back."""
    restores = []
    for target in targets:
        try:
            attrs = session.get(f"{HA_URL}/api/states/{target.entity_id}",
                                headers=auth_headers(), timeout=5).json().get("attributes", {})
        except (requests.RequestException, ValueError, AttributeError):
            attrs = {}
        restores.append({
            "entity_id": target.entity_id,
            "rgb_color": attrs.get("rgb_color") or [255, 200, 120],
            "brightness": attrs.get("brightness") or 140,
            "transition": 0,
        })
    return restores


def autotune(verbose: bool = True, per_step: int = 4) -> dict:
    """Stress **every** configured light and pick a pacing the radio accepts.

    The bottleneck is the radio, and it has to carry one command per light per
    frame - so a single-light probe flatters the result. Each round sends a
    command to every target (concurrently, like the sender does), and a round
    only counts as clean if all of them came back. Every light is restored
    afterwards.
    """
    targets = list(TARGETS) or [Target(LIGHT_ENTITY_ID, frozenset(range(1, ZONE_COUNT + 1)))]
    report = {
        "entity": targets[0].entity_id,
        "lights": [t.entity_id for t in targets],
        "light_count": len(targets),
        "dry_run": DRY_RUN,
        "steps": [],
        "floor": None,
        "ceiling": None,
        "best_ms": None,
        "busy_at_ms": None,
        "worst_rtt_ms": None,
        "applied": {},
        "restored": 0,
        "ok": False,
        "error": None,
    }
    say = print if verbose else (lambda *a, **k: None)

    if DRY_RUN:
        report["error"] = "dry run is on: nothing is sent, so there is nothing to measure"
        return report
    if not check_ha():
        report["error"] = "Home Assistant is unreachable or the token was rejected"
        return report

    restores = _autotune_attrs(targets)

    def round_bodies(round_no: int) -> list[dict]:
        return [{"entity_id": t.entity_id,
                 "rgb_color": list(AUTOTUNE_COLOURS[i % len(AUTOTUNE_COLOURS)]),
                 "brightness": 120 if round_no % 2 else 200,
                 "transition": 0}
                for i, t in enumerate(targets)]

    def fire(round_no: int) -> tuple[int, int, float, str | None]:
        """One command to every light. Returns (ok, busy, worst_rtt_ms, error)."""
        bodies = round_bodies(round_no)
        started = time.perf_counter()

        def post_one(body):
            began = time.perf_counter()
            try:
                response = _post(body, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                return False, (time.perf_counter() - began) * 1000.0, str(exc)[:80]
            return response.ok, (time.perf_counter() - began) * 1000.0, response.text

        if SEND_CONCURRENCY and len(bodies) > 1:
            with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
                results = list(pool.map(post_one, bodies))
        else:
            results = [post_one(b) for b in bodies]
        ok = sum(1 for good, _ms, _txt in results if good)
        busy = len(results) - ok
        worst = max(ms for _good, ms, _txt in results) if results else 0.0
        problems = [txt for good, _ms, txt in results if not good and txt]
        wall = (time.perf_counter() - started) * 1000.0
        return ok, busy, max(worst, wall), (problems[0][:80] if problems else None)

    accepted = []
    try:
        for interval in AUTOTUNE_STEPS:
            clean = 0
            rejected = 0
            rtts = []
            error = None
            for n in range(per_step):
                ok, busy, rtt, problem = fire(n)
                rtts.append(rtt)
                if busy == 0 and ok == len(targets):
                    clean += 1
                else:
                    rejected += 1
                    error = error or problem
                time.sleep(interval)
            step = {"interval_ms": round(interval * 1000), "ok": clean,
                    "busy": rejected,
                    "rtt_ms": round(sorted(rtts)[len(rtts) // 2], 1) if rtts else None}
            if error:
                step["error"] = error
            report["steps"].append(step)
            say(f"  {step['interval_ms']:>4} ms for {len(targets)} light(s): "
                f"{clean} clean, {rejected} rejected, rtt {step['rtt_ms']} ms")
            if rejected:
                report["busy_at_ms"] = step["interval_ms"]
                break
            accepted.append(interval)
            report["worst_rtt_ms"] = step["rtt_ms"]
    finally:
        # the burst just filled the radio's queue, so wait at least as long as the
        # fastest interval we were willing to accept before putting the lights back
        gap = max(0.25, (accepted[-1] * 1.2) if accepted else 0.5)
        for _attempt in range(3):
            time.sleep(gap)
            restored = sum(1 for restore in restores if restore_light(restore, attempts=2))
            if restored == len(restores):
                break
        report["restored"] = restored

    if not accepted:
        report["error"] = ("even the slowest probe was rejected - the radio is busy or "
                          "the coordinator is down")
        return report

    floor = accepted[-1] * 1.15                       # a margin, not the cliff edge
    ceiling = min(max(floor * 4.0, 0.30), 2.0)
    floor = round(max(POST_INTERVAL_FLOOR_MIN, min(floor, POST_INTERVAL_CEILING_MAX)), 3)
    ceiling = round(max(ceiling, floor), 3)
    report.update({"floor": floor, "ceiling": ceiling,
                   "best_ms": round(floor * 1000)})

    applied, errors = apply_settings({"POST_INTERVAL_FLOOR": floor,
                                      "POST_INTERVAL_CEILING": ceiling}, persist=False)
    report["applied"] = applied
    if errors:
        report["error"] = "; ".join(errors)
        return report
    report["ok"] = True
    say(f"  -> {floor:.2f}-{ceiling:.2f} s between commands for {len(targets)} light(s) "
        + (f"(rejected at {report['busy_at_ms']} ms)" if report["busy_at_ms"]
           else "(no rejection seen)"))
    return report


def calibrate(verbose: bool = True) -> dict:
    """Push test colours to one light and measure the chromaticity it reports back.

    Returns a report dict so the CLI and the web UI can show the same numbers.
    """
    entity = TARGETS[0].entity_id if TARGETS else LIGHT_ENTITY_ID
    report = {
        "entity": entity,
        "zones": TARGETS[0].zone_label if TARGETS else "1-9",
        "dry_run": DRY_RUN,
        "samples": [],
        "worst_duv": None,
        "worst_de": None,
        "restored": False,
        "ok": False,
        "error": None,
    }
    say = print if verbose else (lambda *a, **k: None)

    if not DRY_RUN and not check_ha():
        report["error"] = "Home Assistant is unreachable or the token was rejected"
        return report
    if DRY_RUN:
        attrs = {}                                  # no HA needed to dry-run
    else:
        try:
            attrs = session.get(f"{HA_URL}/api/states/{entity}", headers=auth_headers(), timeout=5).json().get("attributes", {})
        except requests.RequestException as exc:
            report["error"] = f"could not read the light's current state: {exc}"
            return report

    restore = {"entity_id": entity}
    if attrs.get("color_mode") == "xy" and attrs.get("xy_color"):
        restore["xy_color"] = attrs["xy_color"]
    elif attrs.get("color_temp_kelvin"):
        restore["color_temp_kelvin"] = attrs["color_temp_kelvin"]
    if attrs.get("brightness"):
        restore["brightness"] = attrs["brightness"]

    worst_uv = worst_e = 0.0
    say(f"calibrating {entity} (zones {report['zones']})")
    try:
        for target_rgb in CALIBRATION_SET:
            xy = xy_from_rgb(target_rgb)
            body = {"entity_id": entity, "xy_color": [round(xy[0], 4), round(xy[1], 4)],
                    "brightness": 128, "transition": 0}
            if DRY_RUN:
                say(f"DRY_RUN would send {target_rgb} -> xy {body['xy_color']}")
                continue
            _post(body)
            time.sleep(0.5)
            got = session.get(f"{HA_URL}/api/states/{entity}", headers=auth_headers(), timeout=5).json().get("attributes", {})
            got_xy = got.get("xy_color") or []
            if len(got_xy) != 2:
                say(f"{target_rgb}: no xy reported (mode={got.get('color_mode')})")
                continue
            d_uv = _neo(xy, got_xy)
            # Equal-lightness comparison: HA's rgb_color attribute is its own
            # wide-gamut, clipping reconstruction, not what the lamp emits.
            y_target = float(_linear_to_xyz(_srgb_to_linear(
                np.asarray(target_rgb, dtype=np.float32) / 255.0))[1])
            comparable = srgb_from_xy(got_xy, max(y_target, 1e-4))
            d_e = delta_e(target_rgb, comparable)
            worst_uv, worst_e = max(worst_uv, d_uv), max(worst_e, d_e)
            report["samples"].append({"target": target_rgb,
                                      "xy": [round(v, 4) for v in got_xy],
                                      "duv": round(d_uv, 4), "de": round(d_e, 1)})
            say(f"target {str(target_rgb):>18} -> xy {[round(v, 3) for v in got_xy]}  "
                f"d_uv'={d_uv:.4f}  dE76={d_e:5.1f}")
    finally:
        if not DRY_RUN:
            report["restored"] = restore_light(restore)
            if report["restored"]:
                log.info("Light restored to %s", {k: v for k, v in restore.items() if k != "entity_id"})

    if report["samples"]:
        report["worst_duv"] = round(worst_uv, 4)
        report["worst_de"] = round(worst_e, 1)
    report["ok"] = bool(report["samples"]) or DRY_RUN
    if report["samples"]:
        say(f"\nworst chromaticity error: du'v'={worst_uv:.4f} "
            f"({'visible' if worst_uv > 0.003 else 'imperceptible'}, 0.003 ~= 1 step)")
        say(f"worst colour error at equal lightness: dE76={worst_e:.1f} "
            f"({'visible' if worst_e > 2 else 'imperceptible'})")
    return report



# ---------------------------------------------------------------- capture ---
def pick_monitor(monitors: list, wanted: int) -> tuple[int, str | None]:
    """Choose a monitor that exists, and say so when the configured one doesn't.

    monitors[0] is mss's synthetic "all screens" canvas, so a machine with two
    screens reports three entries and only 1..2 are real.
    """
    screens = max(0, len(monitors) - 1)
    if 0 <= wanted < len(monitors):
        return wanted, None
    used = 1 if screens >= 1 else 0
    return used, (f"MONITOR_INDEX {wanted} does not exist: this machine reports "
                  f"{screens} monitor(s) (0 = all combined), so monitor {used} "
                  f"is being captured instead")


def capture_geometry(monitor: dict, targets: list["Target"]) -> tuple[dict, tuple[int, int, int, int], tuple[int, int]]:
    """Grab only the rectangle the lights actually read.

    Frame cost scales with captured pixels (measured 5.5 ms for 4K, 1.9 ms for a
    third of it), so if every light sits on part of the screen we stop grabbing
    the rest. Returns (grab rect, (row0,row1,col0,col1) grid window, cell size).
    """
    used = sorted({z for t in targets for z in t.zones})
    if len(used) == 9:
        rect = {"left": monitor["left"], "top": monitor["top"],
                "width": monitor["width"], "height": monitor["height"]}
        return rect, (0, GRID_SIZE[1] - 1, 0, GRID_SIZE[0] - 1), GRID_SIZE

    rows = np.unique(np.concatenate([ZONE_INDEX[z - 1][0] for z in used]))
    cols = np.unique(np.concatenate([ZONE_INDEX[z - 1][1] for z in used]))
    r0, r1 = int(rows[0]), int(rows[-1])
    c0, c1 = int(cols[0]), int(cols[-1])

    cell_w = monitor["width"] / GRID_SIZE[0]
    cell_h = monitor["height"] / GRID_SIZE[1]
    x0, x1 = int(c0 * cell_w), int(round((c1 + 1) * cell_w))
    y0, y1 = int(r0 * cell_h), int(round((r1 + 1) * cell_h))
    rect = {"left": monitor["left"] + x0, "top": monitor["top"] + y0,
            "width": max(1, x1 - x0), "height": max(1, y1 - y0)}
    return rect, (r0, r1, c0, c1), (c1 - c0 + 1, r1 - r0 + 1)


def effective_stride(width: int, height: int) -> int:
    """Cap the stride so every cell still gets several samples per axis.

    Without this, a small screen (or a window-sized capture) would end up with
    one point sample per cell, which is not a mean at all.
    """
    cell_x = width // GRID_SIZE[0]
    cell_y = height // GRID_SIZE[1]
    samples_per_axis = max(1, min(cell_x, cell_y) // 2)
    return max(1, min(CAPTURE_STRIDE, cell_x, cell_y, samples_per_axis))


def grid_from_shot(shot, size: tuple[int, int] | None = None) -> np.ndarray:
    """Screen capture -> sample grid (default 64x36), as cheaply as possible."""
    width, height = shot.size
    size = size or GRID_SIZE
    step = effective_stride(width, height)
    if step > 1:
        try:
            frame = np.asarray(shot)                      # zero-copy BGRA view
            sub = np.ascontiguousarray(frame[::step, ::step])
            small = Image.frombuffer("RGB", (sub.shape[1], sub.shape[0]), sub, "raw", "BGRX", 0, 1)
            return np.asarray(small.resize(size, Image.Resampling.BOX), dtype=np.uint8)
        except Exception:                                 # fall back to the safe path
            pass
    raw = getattr(shot, "raw", None)
    if raw is not None:
        img = Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
    else:
        img = Image.frombytes("RGB", (width, height), shot.rgb)   # non-mss fakes
    # BOX = exact area mean per cell (LANCZOS samples a kernel and rings).
    return np.asarray(img.resize(size, Image.Resampling.BOX), dtype=np.uint8)


def disable_captureblt(gdi_module=None) -> bool:
    """Drop CAPTUREBLT from mss's BitBlt on Windows.

    mss always asks for layered windows (`SRCCOPY | CAPTUREBLT`). At capture
    rates that flag makes the hardware cursor flicker and jump on many Windows
    setups - the pointer blinks to the top-left corner in step with the capture
    loop (python-mss issue #179). The flag is a module-level constant that
    grab() reads on every call, so zeroing it is enough and needs no fork.

    Returns True when the constant was changed.
    """
    if gdi_module is None:
        if os.name != "nt":
            return False
        try:
            from mss.windows import gdi as gdi_module
        except Exception:                     # older mss: one windows module
            try:
                import mss.windows as gdi_module
            except Exception as exc:
                log.debug("could not reach the Windows capture backend: %s", exc)
                return False
    if not getattr(gdi_module, "CAPTUREBLT", 0):
        return False
    gdi_module.CAPTUREBLT = 0
    log.info("capturing without CAPTUREBLT (stops the Windows cursor flicker)")
    return True


def capture_loop() -> None:
    interval = 1.0 / CAPTURE_FPS if CAPTURE_FPS > 0 else 0.5
    disable_captureblt()
    try:
        grabber = mss()
    except Exception as exc:                     # no display / permissions
        log.error("Screen capture unavailable (%s). Run this on the machine with the monitor.", exc)
        return

    monitors = grabber.monitors
    index, warning = pick_monitor(monitors, MONITOR_INDEX)
    if warning:
        log.error(warning)
    monitor = monitors[index]
    seen_rev = -1                                 # geometry is built on the first pass
    grab_rect = grid_zeros = cell_size = None
    log.info("Capturing monitor %s (%sx%s) as %s grid; %d light(s): %s",
             index, monitor["width"], monitor["height"], GRID_SIZE, len(TARGETS),
             ", ".join(f"{t.entity_id}[{t.zone_label}]" for t in TARGETS))
    # (both are re-read each iteration below so the UI can retune the rate live)
    prev_grid = None
    identical = 0
    last_eval = 0.0

    while not stop_event.is_set():
        started = time.perf_counter()
        interval = 1.0 / CAPTURE_FPS if CAPTURE_FPS > 0 else 0.5
        idle_interval = 1.0 / IDLE_FPS if IDLE_FPS > 0 else interval
        try:
            if seen_rev != GEOMETRY_REV:          # first pass, or the zone map changed
                grab_rect, (row0, row1, col0, col1), cell_size = capture_geometry(monitor, TARGETS)
                grid_zeros = None
                if (row0, row1, col0, col1) != (0, GRID_SIZE[1] - 1, 0, GRID_SIZE[0] - 1):
                    grid_zeros = np.zeros((GRID_SIZE[1], GRID_SIZE[0], 3), dtype=np.uint8)
                seen_rev = GEOMETRY_REV
                stats["crop"] = [grab_rect["width"], grab_rect["height"]]
                if grid_zeros is None:
                    log.info("capture window: full monitor, %d light(s)", len(TARGETS))
                else:
                    log.info("capture window: %sx%s at (%s,%s) for %d light(s)",
                             grab_rect["width"], grab_rect["height"], grab_rect["left"],
                             grab_rect["top"], len(TARGETS))
            shot = grabber.grab(grab_rect)
            if grid_zeros is None:
                grid = grid_from_shot(shot)
            else:                       # paste the used window into a full grid
                grid = grid_zeros.copy()
                grid[row0:row1 + 1, col0:col1 + 1] = grid_from_shot(shot, size=cell_size)
            prep_ms = (time.perf_counter() - started) * 1000.0
            stats["prep_ms"] = round(prep_ms, 2) if stats["prep_ms"] is None else round(
                0.8 * stats["prep_ms"] + 0.2 * prep_ms, 2)

            # A frozen screen cannot change any light colour, so stop paying for
            # the colour work; sample slower, but keep evaluating at 2 Hz so the
            # RESEND_AFTER rescue still fires when a command was lost.
            unchanged = prev_grid is not None and np.array_equal(grid, prev_grid)
            prev_grid = grid
            identical = identical + 1 if unchanged else 0
            stats["idle"] = identical >= IDLE_AFTER

            if stats["idle"] and time.perf_counter() - last_eval < 0.5:
                stop_event.wait(max(0.0, idle_interval - (time.perf_counter() - started)))
                continue

            lin = _srgb_to_linear(grid.astype(np.float32) / 255.0)
            measured_at = time.perf_counter()
            zone_cells = zones_cells(lin)

            zone_colours = [aggregate(c) for c in zone_cells]
            regions = {
                "zones": [dict(c, preview=preview_colour(c)) for c in zone_colours],
                "full": dict(aggregate(lin), preview=preview_colour(aggregate(lin))),
            }
            batch: dict[str, dict] = {}
            light_report: dict[str, dict] = {}
            for target in TARGETS:
                colour = aggregate(cells_for(target, lin, zone_cells))
                if COLOR_ALGO == "dominant" and "dominant" in colour:
                    colour["xy"] = colour["dominant"]
                    colour["rgb"] = colour.pop("rgb_dominant", colour["rgb"])
                    colour["luminance"] = colour.pop("dominant_luminance", colour["luminance"])
                colour["measured_at"] = measured_at
                colour["preview"] = preview_colour(colour)
                light_report[target.entity_id] = colour
                brightness = brightness_for_colour(colour)
                if should_send(target, colour, brightness):
                    batch[target.entity_id] = colour
                    # record it now: the sender may not post for a few ms, and
                    # dedupe has to hold for that window too
                    target.last_attempted = (tuple(colour["rgb"]), brightness)
                    target.last_attempt_mono = time.perf_counter()

            with state_lock:
                latest_data["raw_grid"] = grid        # serialised on request only
                latest_data["regions"] = regions
                latest_data["lights"] = light_report

            last_eval = time.perf_counter()
            stats["frames"] += 1
            if batch:
                send_to_ha(batch)
        except Exception as exc:                 # never let one bad frame kill the thread
            stats["frame_errors"] += 1
            stats["last_frame_error"] = f"{type(exc).__name__}: {exc}"
            log.error("Frame error: %s", exc)

        stop_event.wait(max(0.0, (idle_interval if stats["idle"] else interval)
                            - (time.perf_counter() - started)))

    grabber.close()
    log.info("Capture loop stopped")


# --------------------------------------------------------------- windows ----
AUTOSTART_RESULT: dict = {}
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "HassAmbilights"


def autostart_command() -> str:
    """The command Windows should run at login (frozen exe, else pythonw)."""
    if FROZEN:
        return f'"{Path(sys.executable).resolve()}" --tray'
    script = Path(__file__).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{runner}" "{script}" --tray'


def autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
        return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """Register or remove the login entry. Returns (ok, message)."""
    if os.name != "nt":
        # harmless elsewhere: the setting is simply inert
        return False, "start with Windows is only available on Windows"
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True, "autostart " + ("enabled" if enabled else "disabled")
    except OSError as exc:
        return False, f"could not change autostart: {exc}"


def make_dpi_aware() -> None:
    """Per-monitor DPI awareness: without it mss reports scaled coordinates.

    On a mixed-DPI multi-monitor desk the captured rectangle lands on the wrong
    pixels (or the wrong monitor) unless the process declares awareness before
    any window or capture exists.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor v2
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception as exc:                                    # never fatal
        log.debug("could not set DPI awareness: %s", exc)


def port_is_ours(host: str, port: int, timeout: float = 1.0) -> bool:
    """Is something already serving this app on that port?"""
    try:
        response = requests.get(f"http://{host}:{port}/health", timeout=timeout)
        body = response.json()
    except (requests.RequestException, ValueError):
        return False
    return isinstance(body, dict) and "version" in body and "lights" in body


def find_instance(host: str, first: int, limit: int = 12) -> int | None:
    """Port of an instance that is already running, if any."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    for port in range(first, first + limit):
        if port_is_ours(probe_host, port):
            return port
    return None


def first_free_port(host: str, first: int, limit: int = 12) -> int:
    """The preferred port, or the next free one so a stale run cannot block us."""
    import socket as _socket

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    for port in range(first, first + limit):
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
            sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            try:
                sock.bind((probe_host, port))
            except OSError:
                continue
            return port
    return first


# ------------------------------------------------------------------- web ----
@asynccontextmanager
async def lifespan(_app: FastAPI):
    global TARGETS
    if not CONFIG_LOADED:
        load_secrets()
        load_config_file()
    if not PERSISTED:                          # no config.json yet: revert to boot values
        PERSISTED.update(settings_snapshot())
    TARGETS = parse_light_map(LIGHT_MAP if isinstance(LIGHT_MAP, str) else "")
    check_ha()
    stop_event.clear()
    worker = threading.Thread(target=capture_loop, name="capture", daemon=True)
    poster = threading.Thread(target=sender_loop, name="sender", daemon=True)
    worker.start()
    poster.start()
    try:
        yield
    finally:
        stop_event.set()
        mailbox.close()
        worker.join(timeout=3)
        poster.join(timeout=3)


app = FastAPI(title="HASS Ambilights", lifespan=lifespan)


@app.get("/raw_grid")
def get_raw_grid():
    with state_lock:
        grid = latest_data["raw_grid"]
    cells = grid.tolist() if hasattr(grid, "tolist") else grid
    return {"grid": cells, "size": list(GRID_SIZE)}


@app.get("/regions")
def get_regions():
    with state_lock:
        return {"zones": latest_data["regions"].get("zones", []),
                "full": latest_data["regions"].get("full")}


@app.get("/health")
def health():
    return {
        "version": __version__,
        "zone_grid": [ZONE_ROWS, ZONE_COLS],
        "zone_count": ZONE_COUNT,
        "ha_url": HA_URL,
        "fps": CAPTURE_FPS,
        "capture_stride": CAPTURE_STRIDE,
        "capture_rect": stats["crop"],
        "send_concurrency": SEND_CONCURRENCY,
        "dominant_luminance": DOMINANT_LUMINANCE,
        "idle_fps": IDLE_FPS,
        "color_algo": COLOR_ALGO,
        "brightness_from": BRIGHTNESS_FROM,
        "dominant_mix": DOMINANT_MIX,
        "saturation_boost": SATURATION_BOOST,
        "send_mode": SEND_MODE,
        "dry_run": DRY_RUN,
        # raw_grid is a numpy array (serialised lazily); bool(array) raises
        "has_frame": bool(len(latest_data["raw_grid"])) if latest_data["raw_grid"] is not None else False,
        "lights": [t.as_dict(latest_data["lights"].get(t.entity_id)) for t in TARGETS],
        "stats": dict(stats),
    }


@app.get("/")
def index_page():
    # no-store: a UI edit must not need a hard refresh to appear
    return HTMLResponse(UI_HTML, headers={"Cache-Control": "no-store"})


# what is on disk, so the UI can tell "changed" from "saved"
PERSISTED = {}
LIGHTS_CACHE = {"at": 0.0, "data": None}
LIGHTS_CACHE_TTL = 30.0


@app.get("/api/monitors")
def get_monitors():
    """Monitors this machine can capture, for the picker (0 = all combined)."""
    try:
        with mss() as sct:
            monitors = list(sct.monitors)
    except Exception as exc:                       # headless, no permission
        return {"ok": False, "monitors": [], "detail": str(exc)}
    out = []
    for index, mon in enumerate(monitors):
        if index == 0:
            out.append({"index": 0, "label": "All monitors combined (%dx%d)"
                        % (mon.get("width", 0), mon.get("height", 0))})
            continue
        out.append({"index": index, "label": "Monitor %d (%dx%d at %d,%d)%s" % (
            index, mon.get("width", 0), mon.get("height", 0),
            mon.get("left", 0), mon.get("top", 0),
            " - primary" if index == 1 else "")})
    # count is monitors, not choices: index 0 is a synthetic combined canvas
    return {"ok": True, "count": len(out) - 1, "choices": len(out), "monitors": out}


@app.get("/api/lights")
def get_lights(refresh: bool = False):
    """Light entities from Home Assistant, for the picker."""
    now = time.time()
    cached = LIGHTS_CACHE["data"]
    if not refresh and cached is not None and now - LIGHTS_CACHE["at"] < LIGHTS_CACHE_TTL:
        return cached
    if not HA_TOKEN:
        return {"ok": False, "lights": [],
                "detail": "no token yet - fill in the Home Assistant card above and save"}
    try:
        response = session.get(f"{HA_URL}/api/states", headers=auth_headers(),
                               timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        return {"ok": False, "lights": [], "detail": str(exc)}
    if response.status_code != 200:
        detail = ("token rejected (401)" if response.status_code == 401
                  else f"HTTP {response.status_code}")
        return {"ok": False, "lights": [], "detail": detail}
    try:
        states = response.json()
    except ValueError:
        return {"ok": False, "lights": [], "detail": "Home Assistant returned non-JSON"}

    found = []
    for state in states if isinstance(states, list) else []:
        entity = str(state.get("entity_id") or "")
        if not entity.startswith("light."):
            continue
        attrs = state.get("attributes") or {}
        modes = [str(m) for m in (attrs.get("supported_color_modes") or [])]
        found.append({
            "entity_id": entity,
            "name": str(attrs.get("friendly_name") or entity.split(".", 1)[-1].replace("_", " ")),
            "state": state.get("state"),
            "color_modes": modes,
            # only these modes can express an arbitrary hue
            "color_capable": any(m in ("xy", "rgb", "rgbw", "rgbww", "hs", "rgbxy") for m in modes),
        })
    found.sort(key=lambda l: (not l["color_capable"], l["name"].lower()))
    payload = {"ok": True, "count": len(found), "lights": found}
    LIGHTS_CACHE.update({"at": now, "data": payload})
    return payload


@app.get("/favicon.ico")
def favicon():
    """Browsers ask for this regardless; the page carries the icon inline."""
    return Response(status_code=204)


@app.get("/api/config")
def get_config():
    return {
        "version": __version__,
        "settings": settings_snapshot(),
        "meta": SETTINGS,
        "config_path": str(CONFIG_PATH),
        "ha_url": HA_URL,
        "dry_run": DRY_RUN,
        "lights": [t.as_dict(latest_data["lights"].get(t.entity_id)) for t in TARGETS],
        "restart_required": [s["name"] for s in SETTINGS if s.get("restart")],
        "config_exists": CONFIG_PATH.exists(),
        "ha_url": HA_URL,
        "ha_token_set": bool(HA_TOKEN),
        # what a Save would write: lets Revert restore the file, and lets a
        # page load say "these values are live but unsaved"
        "zone_grid": [ZONE_ROWS, ZONE_COLS],
        "zone_count": ZONE_COUNT,
        "saved_settings": dict(PERSISTED) if PERSISTED else settings_snapshot(),
        "unsaved": bool(PERSISTED) and PERSISTED != settings_snapshot(),
        "secrets_path": str(SECRETS_PATH),
        "secrets_exist": SECRETS_PATH.exists(),
    }


@app.post("/api/test-connection")
def post_test_connection(payload: dict = Body(default={})):
    """Probe Home Assistant with the saved (or just-supplied) URL and token."""
    global HA_URL, HA_TOKEN
    url = str(payload.get("HA_URL") or HA_URL).rstrip("/")
    token = str(payload.get("HA_TOKEN") or "").strip() or HA_TOKEN
    if not token:
        return {"ok": False, "detail": "no token set"}
    try:
        response = requests.get(f"{url}/api/", headers={"Authorization": f"Bearer {token}"},
                                timeout=5)
    except requests.RequestException as exc:
        return {"ok": False, "detail": str(exc)}
    if response.status_code == 200:
        return {"ok": True, "detail": "connected", "ha_url": url}
    if response.status_code == 401:
        return {"ok": False, "detail": "token rejected (401)"}
    return {"ok": False, "detail": f"HTTP {response.status_code}"}


def _control_flag(payload: dict, key: str) -> tuple[bool, str | None]:
    """Only literal booleans (or "true"/"false" strings) count as true.

    `bool("false")` is True, which used to mean a client sending the string
    "false" reset every setting or wrote config.json when it asked not to.
    """
    if key not in payload or payload[key] is None:
        return False, None
    value = payload[key]
    if isinstance(value, bool):
        return value, None
    if isinstance(value, str) and value.strip().lower() in TRUEISH | FALSEISH:
        return value.strip().lower() in TRUEISH, None
    return False, f"{key}: must be true or false"


@app.post("/api/config")
def post_config(payload: Any = Body(...)):
    """Save settings. Validated as a whole: nothing is applied on any error."""
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "errors": ["payload must be an object"]},
                            status_code=400)
    before = settings_snapshot()
    before_token = HA_TOKEN
    persist = True
    for flag in ("reset", "persist"):
        if flag in payload:
            value, problem = _control_flag(payload, flag)
            if problem:
                return JSONResponse({"ok": False, "errors": [problem]}, status_code=400)
            if flag == "persist":
                persist = value
            reset = value
    reset = locals().get("reset", False)
    # a flat body is fine, but its control keys are ours, not settings
    settings = payload.get("settings", {k: v for k, v in payload.items()
                                        if k not in ("reset", "persist", "settings")})
    if not isinstance(settings, dict):
        return JSONResponse({"ok": False, "errors": ["settings must be an object"]},
                            status_code=400)
    if reset:
        applied, errors = apply_settings({s["name"]: s["default"] for s in SETTINGS},
                                         persist=persist)
    else:
        applied, errors = apply_settings(settings, persist=persist)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=400)
    # the UI posts every setting, so report what actually changed rather than
    # telling the user to restart for knobs they never touched
    after = settings_snapshot()
    changed_names = [name for name in applied if str(before.get(name)) != str(after.get(name))]
    if HA_TOKEN != before_token:
        changed_names.append("HA_TOKEN")     # masked below, but it is a change
        applied["HA_TOKEN"] = HA_TOKEN
    applied = {name: applied[name] for name in changed_names}
    # `applied` is already reduced to real changes (a rotated token included,
    # which the masked snapshot cannot show), so the probe keys off it directly
    changed = set(applied)
    probe = {"ok": True, "detail": "unchanged"}
    if {"HA_URL", "HA_TOKEN"} & changed:
        probe = post_test_connection({})
    # never hand a secret back, even the one that was just supplied
    safe_applied = {
        name: (SECRET_SET if value else SECRET_UNSET)
        if SETTINGS_BY_NAME.get(name, {}).get("type") == "secret" else value
        for name, value in applied.items()
    }
    return {"ok": True, "applied": safe_applied, "settings": settings_snapshot(),
            "persisted": persist, "connection": probe, "autostart": dict(AUTOSTART_RESULT),
            "restart_required": sorted(n for n in changed
                                        if SETTINGS_BY_NAME.get(n, {}).get("restart"))}


@app.post("/api/autotune")
def post_autotune():
    """Measure the fastest pacing the radio accepts (flashes the first light)."""
    if DRY_RUN:
        return JSONResponse({"ok": False, "error": "dry run is on - nothing to measure"},
                            status_code=400)
    if not HA_TOKEN:
        return JSONResponse({"ok": False, "error": "HA_TOKEN is not set"}, status_code=400)
    if not AUTOTUNE_LOCK.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "an auto tune is already running"},
                            status_code=409)
    stats["autotuning"] = True
    stats["autotune"] = None

    def run() -> None:
        try:
            stats["autotune"] = autotune(verbose=False)
        except Exception as exc:                      # keep the flag usable
            stats["autotune"] = {"ok": False, "error": str(exc)}
        finally:
            stats["autotuning"] = False
            AUTOTUNE_LOCK.release()

    threading.Thread(target=run, name="autotune", daemon=True).start()
    return {"ok": True, "started": True}


@app.post("/api/calibrate")
def post_calibrate():
    """Measure the first light's real colour accuracy (flashes it for ~7 s)."""
    if not HA_TOKEN:
        return JSONResponse({"ok": False, "error": "HA_TOKEN is not set"}, status_code=400)
    if not CALIBRATION_LOCK.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "a calibration is already running"},
                            status_code=409)
    entity = TARGETS[0].entity_id if TARGETS else LIGHT_ENTITY_ID
    stats["calibrating"] = True
    stats["calibration"] = None

    def run() -> None:
        try:
            stats["calibration"] = calibrate(verbose=False)
        except Exception as exc:                      # keep the flag usable
            stats["calibration"] = {"ok": False, "error": str(exc)}
        finally:
            stats["calibrating"] = False
            CALIBRATION_LOCK.release()

    threading.Thread(target=run, name="calibrate", daemon=True).start()
    return {"ok": True, "started": True, "entity": entity}


def gui_available() -> bool:
    """Is there a native window to open (pywebview and a backend)?"""
    import importlib.util

    return importlib.util.find_spec("webview") is not None


def tray_icon_image():
    """Four dots on dark, matching the page icon. Pure PIL, so it is testable."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (13, 17, 23, 255))
    draw = ImageDraw.Draw(img)
    colours = [(255, 92, 92), (68, 209, 122), (68, 147, 248), (255, 210, 38)]
    for i, colour in enumerate(colours):
        x = 10 + (i % 2) * 30
        y = 10 + (i // 2) * 30
        draw.ellipse((x, y, x + 18, y + 18), fill=colour + (255,))
    return img


def start_server(host: str, port: int):
    """Run uvicorn in a thread so a window can own the main loop."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", log_config=None)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None      # not the main thread
    ACTIVE_SERVER["server"] = server
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    return thread, server


def wait_for_server(url: str, timeout: float = 15.0) -> bool:
    """Poll until the port answers - it is closed for the first few hundred ms."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            if requests.get(url + "health", timeout=1.0).ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.15)
    return False


TRAY_ICON: dict = {}
ACTIVE_SERVER: dict = {}
QUITTING = threading.Event()


def request_shutdown() -> None:
    """Ask the embedded server and the capture/send threads to stop."""
    QUITTING.set()
    stop_event.set()
    server = ACTIVE_SERVER.get("server")
    if server is not None:
        try:
            server.should_exit = True
        except Exception:                     # a half-built server is not fatal
            log.debug("could not stop the server", exc_info=True)


def force_exit_soon(delay: float = 2.0) -> None:
    """Leave the process even if a GUI backend refuses to unwind its loop.

    WebView2 and the pystray message loop can keep non-daemon threads alive after
    the window is gone, which leaves HassAmbilights.exe in Task Manager with no
    window and no tray icon. The graceful stop runs first; this is the backstop.
    """

    def _die() -> None:
        os._exit(0)

    timer = threading.Timer(delay, _die)
    timer.daemon = True
    timer.start()


def _hide_instead_of_close(window) -> bool:
    """Cancel the close and hide. Returning False is how pywebview vetoes it.

    While quitting the veto must not apply: pywebview's destroy() closes the
    window through the same FormClosing event, so vetoing it left the process
    (and its hidden window) running after tray -> Quit.
    """
    if QUITTING.is_set():
        return True                           # do not cancel: this close is the quit
    try:
        window.hide()
    except Exception as exc:                  # no window backend: let it close
        log.debug("could not hide the window: %s", exc)
        return True
    log.info("window hidden - still running in the tray")
    return False


def build_tray(on_show, on_quit, label: str = "HASS Ambilights"):
    """Create the tray icon without starting it, or None if pystray is missing."""
    try:
        import pystray
    except ImportError:
        log.warning("pystray is not installed - no tray icon")
        return None
    icon = pystray.Icon(
        "hass_ambilights", tray_icon_image(), label,
        menu=pystray.Menu(pystray.MenuItem("Open settings", on_show, default=True),
                          pystray.MenuItem("Quit", on_quit)))
    TRAY_ICON["icon"] = icon
    return icon


def run_tray_detached(icon) -> None:
    """Run a tray icon without owning the main thread (Windows message loop)."""
    if hasattr(icon, "run_detached"):
        try:
            icon.run_detached()
            return
        except Exception as exc:
            log.debug("run_detached failed (%s), using a thread", exc)
    threading.Thread(target=icon.run, name="tray", daemon=True).start()


def start_tray_for_window(window, url: str) -> None:
    """A tray icon that can bring the hidden window back, or quit for real."""
    import webbrowser

    def show_settings(_icon=None, _item=None):
        try:
            window.show()
        except Exception:
            webbrowser.open(url)

    def quit_app(_icon=None, _item=None):
        request_shutdown()
        icon = TRAY_ICON.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                log.debug("could not stop the tray icon", exc_info=True)
        try:
            window.destroy()
        except Exception:
            log.debug("could not destroy the window", exc_info=True)
        force_exit_soon()

    icon = build_tray(show_settings, quit_app)
    if icon is not None:
        run_tray_detached(icon)


def run_window(url: str, close_to_tray: bool | None = None) -> int:
    """The UI in a native window; with CLOSE_TO_TRAY, closing hides it."""
    if close_to_tray is None:
        close_to_tray = CLOSE_TO_TRAY
    try:
        import webview
    except ImportError:
        log.warning("pywebview is not installed - using your browser instead")
        import webbrowser

        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0
    window = webview.create_window("HASS Ambilights", url, width=1180, height=840,
                                   min_size=(900, 600))
    if close_to_tray:
        start_tray_for_window(window, url)
        window.events.closing += lambda: _hide_instead_of_close(window)
    webview.start()
    return 0


def run_tray(url: str) -> int:
    """A tray icon only: open the settings, or quit. Used for autostart."""
    try:
        import pystray
    except ImportError:
        log.warning("pystray is not installed - serving without a tray icon")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    import webbrowser

    def open_settings(_icon=None, _item=None):
        webbrowser.open(url)

    def quit_app(_icon=None, _item=None):
        request_shutdown()
        icon = TRAY_ICON.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                log.debug("could not stop the tray icon", exc_info=True)
        force_exit_soon()

    icon = build_tray(open_settings, quit_app)
    if icon is None:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0
    icon.run()
    return 0


def splash_text(message: str) -> None:
    """Update the boot splash. Silent when this build has no splash."""
    try:
        import pyi_splash
    except ImportError:
        return
    try:
        pyi_splash.update_text(message)
    except Exception:                        # a splash without a text area
        pass


def close_splash() -> None:
    """Dismiss the boot splash. Safe to call anywhere, any number of times."""
    try:
        import pyi_splash
    except ImportError:                      # not a frozen --splash build
        return
    try:
        pyi_splash.close()
    except Exception:
        pass


def main() -> None:
    try:
        _run()
    finally:
        close_splash()               # never outlive a failed start


def _run() -> None:
    parser = argparse.ArgumentParser(description="HASS Ambilights - screen colour -> Home Assistant lights")
    parser.add_argument("--version", action="version",
                        version=f"hass_ambilights {__version__}")
    parser.add_argument("--autotune", action="store_true",
                        help="stress the lights and pick the fastest pacing they accept")
    parser.add_argument("--calibrate", action="store_true",
                        help="measure this light's colour accuracy, then restore its state")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the settings page on a run with no token")
    parser.add_argument("--window", action="store_true",
                        help="open the settings in a native window (pywebview) and quit "
                             "when it closes")
    parser.add_argument("--tray", action="store_true",
                        help="run in the system tray with Open settings / Quit")
    parser.add_argument("--server", action="store_true",
                        help="serve only, no window or tray (services, CI)")
    args = parser.parse_args()
    if args.autotune:
        load_config_file()
        TARGETS.extend(parse_light_map(LIGHT_MAP if isinstance(LIGHT_MAP, str) else ""))
        report = autotune()
        if report["error"]:
            print("auto tune failed: " + report["error"], file=sys.stderr)
        raise SystemExit(0 if report["ok"] else 1)
    if args.calibrate:
        load_config_file()
        TARGETS.extend(parse_light_map(LIGHT_MAP if isinstance(LIGHT_MAP, str) else ""))
        report = calibrate()
        if report["error"]:
            print("calibration failed: " + report["error"], file=sys.stderr)
        raise SystemExit(0 if report["ok"] else 1)
    if args.server:
        mode = "server"
    elif args.tray:
        mode = "tray"
    elif args.window or FROZEN:          # a double-clicked exe opens its window
        mode = "window"
    else:
        mode = "server"

    redirect_std_streams()
    setup_file_logging()
    make_dpi_aware()                     # before any window or capture exists
    log.info("HASS Ambilights %s starting (mode=%s, frozen=%s)", __version__, mode, FROZEN)
    splash_text("loading settings")

    load_secrets()
    load_config_file()                   # BIND_* may come from config.json
    splash_text("starting the settings server")

    host = "127.0.0.1" if BIND_HOST in ("0.0.0.0", "::") else BIND_HOST

    existing = find_instance(host, BIND_PORT)
    if existing is not None:
        url = f"http://{host}:{existing}/"
        log.info("HASS Ambilights is already running at %s", url)
        close_splash()
        if mode == "window" and gui_available():
            run_window(url)
        else:
            import webbrowser

            webbrowser.open(url)
        return

    if mode == "server":
        port = BIND_PORT
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((BIND_HOST if BIND_HOST != "::" else "0.0.0.0", port))
        except OSError as exc:
            raise SystemExit(
                f"cannot listen on {BIND_HOST}:{port} ({exc.strerror or exc}).\n"
                f"Something else is using that port - stop it, or set BIND_PORT "
                f"(Server card, or BIND_PORT=8001 in the environment).")
        finally:
            probe.close()
    else:
        port = first_free_port(BIND_HOST, BIND_PORT)
        if port != BIND_PORT:
            log.warning("port %s is taken; using %s", BIND_PORT, port)

    if BIND_HOST not in ("127.0.0.1", "localhost", "::1"):
        log.warning("settings UI on http://%s:%s — it has no authentication, so anyone "
                    "who can reach that port can change settings", BIND_HOST, port)

    url = f"http://{host}:{port}/"
    if mode == "server":
        if not HA_TOKEN and not args.no_browser:
            # nothing to mirror yet: hand the user the settings page
            import webbrowser

            threading.Timer(1.0, webbrowser.open, (url,)).start()
        log.info("serving on http://%s:%s", BIND_HOST, port)
        close_splash()
        uvicorn.run(app, host=BIND_HOST, port=port, log_config=None)
        return

    log.info("starting the embedded server on %s", url)
    thread, server = start_server(BIND_HOST, port)
    if not wait_for_server(url):
        server.should_exit = True
        thread.join(timeout=5)
        raise SystemExit(f"the settings server did not start on {url} - check the log")
    log.info("serving %s in %s mode", url, mode)
    close_splash()                       # the window is about to appear
    try:
        code = run_window(url) if mode == "window" else run_tray(url)
    finally:
        request_shutdown()
        thread.join(timeout=5)
    # A GUI close can leave WebView2 / pystray threads holding the interpreter
    # open after every window is gone; leave the process deterministically.
    for _handler in logging.getLogger().handlers:
        try:
            _handler.flush()
        except Exception:
            pass
    os._exit(code)


if __name__ == "__main__":
    main()
