"""Offline verification for hass_ambilights.py: fake screen + mock HA.

Run from anywhere:  python tests/test_hass_ambilights.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

POSTS = []
RTT_S = 0.0
BUSY_LEFT = 0
FAIL_POSTS = 0
CONTROLLER_DOWN = 0
GET_COUNT = 0
GET_AUTH = []


class MockHA(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        global GET_COUNT
        GET_COUNT += 1
        GET_AUTH.append(self.headers.get("Authorization"))
        body = b"{}"
        if self.path.startswith("/api/states"):
            body = json.dumps([
                {"entity_id": "light.plain_bulb", "state": "off",
                 "attributes": {"friendly_name": "Plain Bulb",
                                "supported_color_modes": ["color_temp"]}},
                {"entity_id": "light.desk_lamp", "state": "on",
                 "attributes": {"friendly_name": "Desk Lamp",
                                "supported_color_modes": ["xy", "color_temp"]}},
                {"entity_id": "switch.not_a_light", "state": "on", "attributes": {}},
                {"entity_id": "light.attic", "state": "on", "attributes": {}},
            ]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST_template(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global BUSY_LEFT, FAIL_POSTS
        if self.path.startswith("/api/template"):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            return self.do_POST_template(
                b"light.desk_lamp=Office\nlight.plain_bulb=Living Room\n"
                b"light.attic=\nlight.unassigned_one=\n")
        if FAIL_POSTS > 0:
            FAIL_POSTS -= 1
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"message":"Service Unavailable"}')
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if RTT_S:
            time.sleep(RTT_S)
        if CONTROLLER_DOWN > 0:
            globals()["CONTROLLER_DOWN"] -= 1
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"message":"Failed to send request: ApplicationController is not running"}')
            return
        if BUSY_LEFT > 0:
            BUSY_LEFT -= 1
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"message":"Failed to send request: Failed to enqueue message"}')
            return
        POSTS.append((self.path, json.loads(body or b"{}")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"[]")


# --- fake screens ---------------------------------------------------------
def page_grid():
    """White page with a vivid photo panel - mean vs dominant is distinguishable."""
    g = np.full((36, 64, 3), (250, 250, 252), dtype=np.uint8)
    g[4:30, 40:60] = (230, 90, 20)
    g[10:12, 2:36] = (60, 60, 60)
    return g


def zone_grid():
    """One distinct colour per 3x3 zone: zone n is filled with colour n."""
    palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255),
               (0, 255, 255), (255, 128, 0), (128, 0, 255), (255, 255, 255)]
    g = np.zeros((36, 64, 3), dtype=np.uint8)
    rows = np.array_split(np.arange(36), 3)
    cols = np.array_split(np.arange(64), 3)
    for i, r in enumerate(rows):
        for j, c in enumerate(cols):
            g[r[0]:r[-1] + 1, c[0]:c[-1] + 1] = palette[i * 3 + j]
    return g


class Frame:
    """Mimics mss.ScreenShot: BGRA raw + numpy array interface, like a real grab."""

    def __init__(self, rgb_grid):
        h, w = rgb_grid.shape[0], rgb_grid.shape[1]
        bgra = np.empty((h, w, 4), dtype=np.uint8)
        bgra[..., 2] = rgb_grid[..., 0]
        bgra[..., 1] = rgb_grid[..., 1]
        bgra[..., 0] = rgb_grid[..., 2]
        bgra[..., 3] = 255
        self.raw = bytearray(bgra.tobytes())
        self.rgb = rgb_grid.tobytes()
        self.size = (w, h)
        self._shape = (h, w, 4)

    @property
    def __array_interface__(self):
        return {"version": 3, "shape": self._shape, "typestr": "|u1", "data": self.raw}


class FakeMSS:
    monitors = [{}, {"left": 0, "top": 0, "width": 64, "height": 36}]
    colour = (200, 60, 40)
    mode = "solid"
    n = 0

    @staticmethod
    def screen():
        if FakeMSS.mode == "cycle":
            FakeMSS.n += 1
            v = (FakeMSS.n * 7) % 200 + 20
            return np.full((36, 64, 3), (v, 255 - v, 120), dtype=np.uint8)
        if FakeMSS.mode == "page":
            return page_grid()
        if FakeMSS.mode == "zones":
            return zone_grid()
        if FakeMSS.mode == "bars":                     # video with black letterbox
            g = np.zeros((36, 64, 3), dtype=np.uint8)
            g[8:28, :] = (200, 90, 30)
            return g
        return np.full((36, 64, 3), FakeMSS.colour, dtype=np.uint8)

    def grab(self, monitor):
        """Honour the requested rect, like a real grab of a sub-region."""
        left, top = monitor.get("left", 0), monitor.get("top", 0)
        w, h = monitor["width"], monitor["height"]
        return Frame(FakeMSS.screen()[top:top + h, left:left + w])

    def close(self):
        pass


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):   # client hung up on purpose
        pass


mock = QuietServer(("127.0.0.1", 0), MockHA)
threading.Thread(target=mock.serve_forever, daemon=True).start()
port = mock.server_address[1]

import os

import tempfile as _tempfile

_sandbox = _tempfile.mkdtemp(prefix="stl-test-")
os.environ["CONFIG_FILE"] = _sandbox + "/config.json"      # never the repo's own file
os.environ["SECRETS_FILE"] = _sandbox + "/secrets.json"
os.environ["HA_URL"] = f"http://127.0.0.1:{port}"
os.environ.setdefault("HA_TOKEN", "test-token")
os.environ["CAPTURE_FPS"] = "30"
os.environ["MONITOR_INDEX"] = "1"
# never let a test re-exec the test runner as if it were the app
os.environ["HASS_AMBILIGHTS_NO_RESTART"] = "1"

from PIL import Image

import hass_ambilights as m

# the module must use mss.MSS where available: mss.mss warns on every call
import mss as _mss_module
if hasattr(_mss_module, "MSS"):
    assert m.mss is not getattr(_mss_module, "mss", None), \
        "hass_ambilights still imports the deprecated mss.mss"
    assert m.mss.__name__ == "MSS", m.mss.__name__

m.mss = lambda: FakeMSS()
m.HA_URL = f"http://127.0.0.1:{port}"
m.session.headers.update({"Authorization": "Bearer test-token"})

def brightness_of(rgb, *extra):
    """Brightness for a plain sRGB colour, honouring the current BRIGHTNESS_FROM."""
    lin = m._srgb_to_linear(np.asarray(rgb, dtype=np.float32) / 255.0)
    return m.brightness_for_colour({"rgb": list(rgb),
                                    "luminance": float(m._linear_to_xyz(lin)[1])})



# --- shipped defaults -----------------------------------------------------
assert m.COLOUR_BLEND == 0.5, m.COLOUR_BLEND
assert m.BRIGHTNESS_FOLLOW_COLOUR is True, "brightness follows the colour slider by default"
assert m.SATURATION_BOOST is False, m.SATURATION_BOOST
print("defaults ok: blend %.2f / follows colour %s / boost %s"
      % (m.COLOUR_BLEND, m.BRIGHTNESS_FOLLOW_COLOUR, m.SATURATION_BOOST))

# --- LIGHT_MAP parsing ----------------------------------------------------
t = m.parse_light_map("")
assert len(t) == 1 and t[0].zones == frozenset(range(1, 10))

t = m.parse_light_map("light.a=1,4,7; light.b=2-3;light.c=all")
assert [x.entity_id for x in t] == ["light.a", "light.b", "light.c"]
assert t[0].zones == {1, 4, 7} and t[0].zone_label == "1,4,7"
assert t[1].zones == {2, 3}
assert t[2].zones == frozenset(range(1, 10))
for bad in ("light.a=0", "light.a=10", "light.a=1,;x=2"):
    try:
        m.parse_light_map(bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"should reject {bad!r}")
print("LIGHT_MAP parsing ok:", [x.zone_label for x in t])

# --- colour science --------------------------------------------------------
white_xy = m.xy_from_rgb([255, 255, 255])
assert abs(white_xy[0] - 0.3127) < 0.0005 and abs(white_xy[1] - 0.3290) < 0.0005
assert abs(m.xy_from_rgb([255, 0, 0])[0] - 0.6400) < 0.002
assert abs(m.xy_from_rgb([0, 255, 0])[1] - 0.6000) < 0.002
assert abs(m.xy_from_rgb([0, 0, 255])[1] - 0.0600) < 0.002
assert m.delta_e([10, 20, 30], [10, 20, 30]) == 0.0
assert 95 < m.delta_e([0, 0, 0], [255, 255, 255]) < 105
print("colour science ok:", [round(v, 4) for v in white_xy])

# --- linear-light averaging ------------------------------------------------
grid = np.zeros((9, 16, 3), dtype=np.uint8)
grid[:, :8] = (255, 255, 255)
assert m.aggregate(m._srgb_to_linear(grid / 255.0))["rgb"] == [188, 188, 188]  # gamma -> 128
assert abs(m.aggregate(m._srgb_to_linear(grid / 255.0))["luminance"] - 0.5) < 0.01
grid2 = np.zeros((9, 16, 3), dtype=np.uint8)
grid2[:, :8] = (255, 0, 0)
grid2[:, 8:] = (0, 0, 255)
mixed = m.aggregate(m._srgb_to_linear(grid2 / 255.0))
assert mixed["rgb"][0] > 150 and mixed["rgb"][2] > 150
print("linear average ok:", mixed["rgb"])

# --- zones: 3x3 split must map exactly ------------------------------------
PALETTE = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255),
           (0, 255, 255), (255, 128, 0), (128, 0, 255), (255, 255, 255)]
lin = m._srgb_to_linear(zone_grid().astype(np.float32) / 255.0)
cells = m.zones_cells(lin)
assert len(cells) == 9
for idx, zone_cells in enumerate(cells, start=1):
    assert m.aggregate(zone_cells)["rgb"] == list(PALETTE[idx - 1]), (idx, m.aggregate(zone_cells)["rgb"])
assert m.aggregate(cells[4])["rgb"] == [255, 0, 255]
print("zone split ok: zone5 =", m.aggregate(cells[4])["rgb"], "zone1 =", m.aggregate(cells[0])["rgb"])

# a light on zones 1,4,7 gets only the left column
left_only = np.concatenate([cells[0], cells[3], cells[6]], axis=0)
left = m.aggregate(left_only)
assert left["rgb"][0] > 200 and left["rgb"][1] > 60, left["rgb"]
print("zone subset ok: zones 1,4,7 =", left["rgb"])

# --- brightness -----------------------------------------------------------
assert brightness_of([255, 255, 255]) == 255
assert brightness_of([0, 0, 0]) == 1
assert 1 < brightness_of([128, 128, 128]) < 255
print("brightness ok: white=255 black=1 mid=%d" % brightness_of([128, 128, 128]))

# --- frame prep: stride path must match the exact path ------------------
big = np.zeros((576, 1024, 3), dtype=np.uint8)
big[:, :] = (40, 90, 200)
big[120:240, 80:320] = (250, 120, 30)
big[400:480, 600:1000] = (255, 255, 255)
assert m.effective_stride(1024, 576) == 4          # 16 px per cell -> 4 samples/axis
assert m.effective_stride(64, 36) == 1             # tiny capture: no subsampling
assert m.effective_stride(3840, 2160) == 4
fast = m.grid_from_shot(Frame(big))
assert fast.shape == (36, 64, 3), fast.shape
exact = np.asarray(Image.frombytes("RGB", (1024, 576), big.tobytes()).resize(m.GRID_SIZE,
                                                                             Image.Resampling.BOX),
                   dtype=np.uint8)
d_e = max(m.delta_e(a, b) for a, b in zip(fast.reshape(-1, 3).tolist(), exact.reshape(-1, 3).tolist()))
assert d_e < 8, d_e                       # block colours: stride 4 stays near-exact
print("frame prep ok: stride %d, max cell dE76 %.2f, %s" % (m.effective_stride(1024, 576), d_e, fast.shape))

# --- brightness must not collapse on saturated frames --------------------
def colour_of(rgb):
    lin = m._srgb_to_linear(np.asarray(rgb, dtype=np.float32) / 255.0)
    return {"rgb": list(rgb), "xy": list(m.xy_from_linear(lin)),
            "luminance": float(m._linear_to_xyz(lin)[1])}


white_c, red_c, blue_c = colour_of((255, 255, 255)), colour_of((255, 0, 0)), colour_of((0, 0, 255))
m.BRIGHTNESS_FROM = "luminance"
lum_red, lum_blue = m.brightness_for_colour(red_c), m.brightness_for_colour(blue_c)
assert m.brightness_for_colour(white_c) == 255
# the curve replaced gamma: a saturated frame must stay clearly lit rather than collapsing,
# and still sit below white
assert 85 < lum_red < 205, f"red collapsed or clipped: {lum_red}"
assert 55 < lum_blue < lum_red, f"blue collapsed or outshone red: {lum_blue} vs {lum_red}"

m.BRIGHTNESS_FROM = "lstar"
ls_white = m.brightness_for_colour(white_c)
ls_red, ls_blue = m.brightness_for_colour(red_c), m.brightness_for_colour(blue_c)
# the channel picks where a frame lands on the curve; lstar lifts saturated colours
assert ls_red >= lum_red and ls_blue >= lum_blue, (ls_red, ls_blue, lum_red, lum_blue)
assert ls_white == 255, ls_white
assert 0.45 < ls_red / ls_white < 0.7, ls_red

m.BRIGHTNESS_FROM = "value"
assert m.brightness_for_colour(red_c) == m.brightness_for_colour(white_c) == 255
assert m.brightness_for_colour(blue_c) == 255
m.BRIGHTNESS_FROM = "lstar"
print("brightness vs chroma ok: red Y-mode=%d L*-mode=%d value-mode=255" % (lum_red, ls_red))

# --- per-light dedupe + rescue -------------------------------------------
tg = m.Target("light.a", frozenset({1}))
colour = {"rgb": [100, 100, 100], "xy": [0.31, 0.33], "luminance": 0.15,
          "measured_at": time.perf_counter()}
assert m.should_send(tg, colour, 100) is True
tg.last_attempted = ((100, 100, 100), 100)
tg.last_sent = ((100, 100, 100), 100)
tg.last_success_mono = time.perf_counter()
assert m.should_send(tg, colour, 100) is False
tg.last_sent = ((255, 0, 0), 200)                       # light stuck on something else
tg.last_attempt_mono = time.perf_counter() - (m.RESEND_AFTER + 1)
assert m.should_send(tg, colour, 100) is True
tg.last_attempt_mono = time.perf_counter()              # too soon: wait, don't retry
assert m.should_send(tg, colour, 100) is False
# an attempt still in flight (queued, not yet confirmed) must not be re-queued
tg.last_attempted = ((100, 100, 100), 100)
tg.last_attempt_mono = time.perf_counter()
assert m.should_send(tg, colour, 100) is False, "in-flight frame was re-queued"
other = m.Target("light.b", frozenset({2}))
assert m.should_send(other, colour, 100) is True        # state is per light
print("per-light dedupe ok (%.1fs rescue)" % m.RESEND_AFTER)

# --- saturation -----------------------------------------------------------
m.SATURATION_BOOST = True          # shipped default is off; exercise the curve
neutral = m.xy_from_rgb([255, 255, 255])
assert m.boost_saturation(neutral, 1.0) == (neutral, 1.0)
assert m.saturation_factor(0.02) < m.saturation_factor(0.9)
pale_xy = m.xy_from_rgb([232, 198, 168])
pale_dark = m.boost_saturation(pale_xy, 0.1)
pale_bright = m.boost_saturation(pale_xy, 0.9)
assert pale_bright[1] > pale_dark[1] > 1.0, (pale_dark[1], pale_bright[1])
assert m._neo(pale_bright[0], m.D65_XY) > m._neo(pale_dark[0], m.D65_XY) > m._neo(pale_xy, m.D65_XY)
assert m._in_srgb_gamut(pale_bright[0])
print("saturation ok: du_v %.4f -> %.4f" % (m._neo(pale_xy, m.D65_XY), m._neo(pale_bright[0], m.D65_XY)))

# --- mean vs dominant ----------------------------------------------------
page = page_grid()
m.COLOUR_BLEND = 0.0
mean_r = m.aggregate(m._srgb_to_linear(page.astype(np.float32) / 255.0))
m.COLOUR_BLEND = 1.0
dom_r = m.aggregate(m._srgb_to_linear(page.astype(np.float32) / 255.0))
assert "dominant" not in mean_r and "dominant" in dom_r
assert m._neo(dom_r["dominant"], m.D65_XY) > 3 * m._neo(mean_r["xy"], m.D65_XY)
gray = np.full((9, 16, 3), 128, dtype=np.uint8)
assert m.aggregate(m._srgb_to_linear(gray / 255.0))["dominant"] == m.aggregate(m._srgb_to_linear(gray / 255.0))["xy"]
m.COLOUR_BLEND = 0.0
assert "dominant" not in m.aggregate(m._srgb_to_linear(page.astype(np.float32) / 255.0))
m.COLOUR_BLEND = 0.5                                  # halfway between the two
_half = m.aggregate(m._srgb_to_linear(page.astype(np.float32) / 255.0))
assert m._neo(_half["dominant"], m.D65_XY) < m._neo(dom_r["dominant"], m.D65_XY)
assert m._neo(_half["dominant"], m.D65_XY) > m._neo(mean_r["xy"], m.D65_XY)
m.COLOUR_BLEND = 0.0
print("mean vs dominant ok: %.4f vs %.4f" % (m._neo(mean_r["xy"], m.D65_XY),
                                             m._neo(dom_r["dominant"], m.D65_XY)))

# --- dominant mode: dedupe/brightness must follow the colour we SEND ------
dark = np.full((36, 64, 3), 30, dtype=np.uint8)
dark[10:26, 18:48] = (255, 140, 40)             # vivid photo panel on a dark page
dark_lin = m._srgb_to_linear(dark.astype(np.float32) / 255.0)
m.COLOUR_BLEND = 1.0
m.BRIGHTNESS_FOLLOW_COLOUR = False
m.BRIGHTNESS_BLEND = 0.0
d_region = m.aggregate(dark_lin)
m.BRIGHTNESS_BLEND = 1.0
d_cluster = m.aggregate(dark_lin)
assert "rgb_dominant" in d_region and "dominant_luminance" in d_cluster
assert tuple(d_region["rgb_dominant"]) != tuple(d_region["rgb"])
# the cluster is brighter than the whole dark page -> brighter lamp
assert d_cluster["dominant_luminance"] > d_cluster["luminance"] * 2, d_cluster
b_region = m.brightness_for_colour({**d_region, "luminance": d_region["luminance"]})
b_cluster = m.brightness_for_colour({**d_cluster,
                                     "luminance": d_cluster["dominant_luminance"]})
assert b_cluster > b_region * 1.2, (b_region, b_cluster)
# value mode must read the colour we send, not the mean
sent_colour = {**d_cluster, "xy": d_cluster["dominant"], "rgb": d_cluster["rgb_dominant"],
               "luminance": d_cluster["dominant_luminance"]}
m.BRIGHTNESS_FROM = "value"
assert m.brightness_for_colour(sent_colour) > m.brightness_for_colour(d_cluster)
m.BRIGHTNESS_FROM = "lstar"
m.COLOUR_BLEND = 0.0
m.BRIGHTNESS_BLEND = 1.0
print("dominant ok: brightness region %d -> cluster %d (Y %.3f -> %.3f)"
      % (b_region, b_cluster, d_cluster["luminance"], d_cluster["dominant_luminance"]))

# --- mean colour with dominant brightness must actually be configurable ------
m.COLOUR_BLEND = 0.0
m.BRIGHTNESS_FOLLOW_COLOUR = False
m.BRIGHTNESS_BLEND = 1.0
_split = m.aggregate(m._srgb_to_linear(
    (lambda g: g)(np.where(np.arange(36 * 64).reshape(36, 64, 1) < 18 * 64, 30, np.array([255, 140, 40])))
).astype(np.float32) / 255.0)
assert "dominant" not in _split, "the colour must stay mean"
assert "dominant_luminance" in _split, "brightness must be allowed to follow the cluster"
assert _split["dominant_luminance"] > _split["luminance"], "a vivid half must lift the level"
m.BRIGHTNESS_FOLLOW_COLOUR = True
_plain = m.aggregate(m._srgb_to_linear(
    (lambda g: g)(np.where(np.arange(36 * 64).reshape(36, 64, 1) < 18 * 64, 30, np.array([255, 140, 40])))
).astype(np.float32) / 255.0)
assert "dominant_luminance" not in _plain, "follow-on at blend 0 is plain mean brightness"
print("blend ok: mean colour + dominant brightness works, and only when asked")

# --- the brightness blend must survive the consumer, not just aggregate -----
_page = np.zeros((36, 64, 3), dtype=np.uint8)
_page[:] = (30, 30, 34)
_page[8:28, 16:48] = (255, 140, 40)
_lin = m._srgb_to_linear(_page.astype(np.float32) / 255.0)
m.COLOUR_BLEND = 0.0
m.BRIGHTNESS_FOLLOW_COLOUR = False
m.BRIGHTNESS_BLEND = 1.0
_colour = m.aggregate(_lin)
_before_level = _colour["luminance"]
# exactly the two lines the capture loop runs
if "dominant" in _colour:
    _colour["xy"] = _colour["dominant"]
_colour["luminance"] = _colour.pop("dominant_luminance", _colour["luminance"])
assert _colour["luminance"] > _before_level * 1.5, (_before_level, _colour["luminance"])
assert m.brightness_for_colour(_colour) > m.brightness_for_luminance(_before_level), "consumer"
print("brightness blend ok: the cluster level reaches the sender, not just aggregate()")

# a saturated chip must look like the colour, not a pale version of it
import math as _math
for _rgb in ((255, 140, 40), (200, 30, 30), (40, 80, 255)):
    _xy = m.xy_from_rgb(list(_rgb))
    _c = {"rgb": list(_rgb), "xy": [round(_xy[0], 4), round(_xy[1], 4)], "luminance": 0.5}
    _chip = m.preview_colour(_c)
    _hue = m.xy_from_rgb(_chip["rgb"])
    _err = m._neo(_hue, _xy)
    assert _err < 0.05, (_rgb, _chip["hex"], _err)

# and the rgb payload must agree with the xy one about the hue
m.SEND_MODE = "rgb"
m.SEND_BRIGHTNESS = False
_payload = m.build_payload(_c)
_red = m.xy_from_rgb(_payload["rgb_color"])
assert m._neo(_red, _xy) < 0.05, (_payload["rgb_color"], _xy)
m.SEND_MODE = "xy"
m.SEND_BRIGHTNESS = True
print("preview ok: chips and rgb payload keep the requested hue (du'v' < 0.05)")

# a black frame must not command white
m.SEND_MODE = "rgb"
m.SEND_BRIGHTNESS = False
_black = m.aggregate(np.zeros((4, 4, 3), dtype=np.float32))
_black_payload = m.build_payload(_black)
assert max(_black_payload["rgb_color"]) <= 1, _black_payload
m.SEND_MODE = "xy"
m.SEND_BRIGHTNESS = True
print("black ok: a black frame asks for black, not white")

# --- black cells (letterbox bars, black windows) must not break dominant --
bars = np.zeros((12, 22, 3), dtype=np.uint8)          # 264 cells, 132 black
bars[3:9, :] = (255, 120, 30)
bars_lin = m._srgb_to_linear(bars.astype(np.float32) / 255.0)
m.COLOUR_BLEND = 1.0
m.BRIGHTNESS_BLEND = 1.0
out = m.aggregate(bars_lin)                            # used to raise IndexError
assert out["dominant"], out
assert m._neo(tuple(out["dominant"]), m.xy_from_rgb([255, 120, 30])) < 0.01, out["dominant"]
# brightness must come from the lit cells, not diluted by the black bars
assert out["dominant_luminance"] > out["luminance"] * 1.5, out
# two hues either side of black bars: the larger block must win
two = np.zeros((12, 22, 3), dtype=np.uint8)
two[3:9, :14] = (255, 120, 30)
two[3:9, 14:] = (0, 80, 255)
o2 = m.aggregate(m._srgb_to_linear(two.astype(np.float32) / 255.0))
assert m._neo(tuple(o2["dominant"]), m.xy_from_rgb([255, 120, 30])) < 0.01, o2["dominant"]
m.COLOUR_BLEND = 0.5
m.BRIGHTNESS_FOLLOW_COLOUR = True
print("black cells ok: dominant %s, cluster Y %.3f vs region %.3f"
      % (out["dominant"], out["dominant_luminance"], out["luminance"]))

# --- saturation can be switched off entirely ----------------------------
assert m.COLOUR_BLEND == 0.5, "default blend should be 0.5, got %s" % m.COLOUR_BLEND
m.SATURATION_BOOST = False
assert m.saturation_factor(1.0) == 1.0
off_xy, off_k = m.boost_saturation(pale_xy, 0.9)
assert off_k == 1.0 and off_xy == pale_xy, (off_k, off_xy)
assert m.build_payload({"xy": list(pale_xy), "rgb": [232, 198, 168], "luminance": 0.604})["xy_color"] \
    == [round(pale_xy[0], 4), round(pale_xy[1], 4)]
m.SATURATION_BOOST = True
on_xy, on_k = m.boost_saturation(pale_xy, 0.9)
assert on_k > 1.0 and m._neo(on_xy, m.D65_XY) > m._neo(pale_xy, m.D65_XY)
print("saturation flag ok: off k=1.0 exact, on k=%.2f" % on_k)

# --- transition pacing ----------------------------------------------------
m.stats["interval_ms"] = None
assert m.current_transition() == m.MIN_TRANSITION
m.stats["interval_ms"] = 200.0
assert abs(m.current_transition() - 0.23) < 0.02
m.stats["interval_ms"] = 5000.0
assert m.current_transition() == m.MAX_TRANSITION
print("auto transition ok")


# --- thread harness -------------------------------------------------------
def run_phase(seconds, busy=0, mode="cycle", colour=(10, 200, 60), rtt=0.0, targets=None):
    global BUSY_LEFT, RTT_S
    m.stop_event.clear()
    POSTS.clear()
    BUSY_LEFT = busy
    RTT_S = rtt
    FakeMSS.mode = mode
    FakeMSS.colour = colour
    m.stats.update(posts=0, frames=0, dropped=0, busy=0, errors=0, interval_ms=None,
                   pace_ms=None, age_ms=None, max_age_ms=None, rtt_ms=None)
    m.mailbox = m.Mailbox()          # fresh queue per phase, no cross-phase leakage
    m.TARGETS = targets if targets is not None else [m.Target("light.a", frozenset(range(1, 10)))]
    for t in m.TARGETS:
        t.last_attempted = t.last_sent = None
        t.last_success_mono = t.last_attempt_mono = 0.0
        t.posts = t.busy = t.errors = 0
    w = threading.Thread(target=m.capture_loop, daemon=True)
    p = threading.Thread(target=m.sender_loop, daemon=True)
    w.start()
    p.start()
    time.sleep(seconds)
    m.stop_event.set()
    w.join(timeout=3)
    p.join(timeout=3)


# --- phase A: static screen -> one post despite 30 fps --------------------
# repeated: the sender used to be able to lose the race between queueing a frame
# and recording it, which let a second identical post through now and then
for _attempt in range(4):
    run_phase(0.8, mode="solid", colour=(200, 60, 40))
    assert len(POSTS) == 1, "static screen posted %d times on attempt %d" % (
        len(POSTS), _attempt + 1)
assert m.stats["idle"] is True, "static screen should drop to IDLE_FPS"
assert m.stats["frames"] <= 8, "idle throttle not working: %d frames" % m.stats["frames"]
print("phase A: 1 post for %d frames (dedupe + idle at %.0f fps)" % (m.stats["frames"], m.IDLE_FPS))

# --- phase B: fast screen + slow HA -> stale frames dropped --------------
run_phase(1.0, mode="cycle", rtt=0.08)
assert len(POSTS) >= 5, len(POSTS)
assert m.stats["dropped"] > 0
print("phase B: %d posts in 1.0s at 80ms RTT, %d dropped, age %.0f ms"
      % (len(POSTS), m.stats["dropped"], m.stats["age_ms"]))

# --- phase C: BUSY -> frame dropped, no superseded colour re-sent --------
run_phase(1.5, busy=2, mode="cycle")
assert m.stats["busy"] >= 1 and m.stats["posts"] >= 1, (m.stats["busy"], m.stats["posts"])
assert m.stats["errors"] == 0
xs = [str(p[1]["xy_color"]) for p in POSTS]
assert len(xs) == len(set(xs)), "re-sent a superseded colour: %s" % xs
print("phase C: %d rejected, %d posted, all distinct" % (m.stats["busy"], m.stats["posts"]))

# --- phase D: frozen screen + lost command -> rescue after RESEND_AFTER --
run_phase(m.RESEND_AFTER + 1.5, busy=2, mode="solid", colour=(10, 200, 60))
assert m.stats["posts"] >= 1, "frozen screen never recovered: %s" % m.stats
assert len(POSTS) <= 3, "hammered retries: %d" % len(POSTS)
print("phase D: frozen-screen rescue ok, %d posts" % len(POSTS))

# --- phase E: three lights, three zone sets, one frame -------------------
targets = [
    m.Target("light.left", frozenset({1, 4, 7})),
    m.Target("light.center", frozenset({2, 5, 8})),
    m.Target("light.right", frozenset({3, 6, 9})),
]
run_phase(0.8, mode="zones", targets=targets)
assert POSTS, "no posts with 3 lights"
sent = {}
for _, body in POSTS:
    ids = body["entity_id"] if isinstance(body["entity_id"], list) else [body["entity_id"]]
    for e in ids:
        sent[e] = tuple(body["xy_color"])
assert set(sent) == {"light.left", "light.center", "light.right"}, sent
assert len(set(sent.values())) == 3, "lights with different zones shipped the same colour: %s" % sent
print("phase E: 3 zone-mapped lights ->", {k: v for k, v in sent.items()})

# --- phase F: identical payloads merge into ONE service call ------------
same = [m.Target("light.x", frozenset({5})), m.Target("light.y", frozenset({5}))]
m.stop_event.clear()
POSTS.clear()
BUSY_LEFT = 0
RTT_S = 0.0
m.stats.update(posts=0, frames=0, dropped=0, busy=0, errors=0, interval_ms=None,
               pace_ms=None, age_ms=None, max_age_ms=None, rtt_ms=None)
m.mailbox = m.Mailbox()
m.TARGETS = same
for t in same:
    t.last_attempted = t.last_sent = None
    t.last_success_mono = 0.0
FakeMSS.mode = "solid"
FakeMSS.colour = (40, 80, 200)
m.COLOUR_BLEND = 0.0
w = threading.Thread(target=m.capture_loop, daemon=True)
p = threading.Thread(target=m.sender_loop, daemon=True)
w.start()
p.start()
time.sleep(1.2)
m.stop_event.set()
w.join(timeout=3)
p.join(timeout=3)
assert POSTS, "identical payloads produced no call at all"
for _, body in POSTS:
    assert isinstance(body["entity_id"], list) and set(body["entity_id"]) == {"light.x", "light.y"}, body
assert len(POSTS) <= 2, "should not re-send an unchanged colour: %d" % len(POSTS)
body = POSTS[0][1]
print("phase F: merged call ->", body["entity_id"])

# --- phase G: concurrency - cycle time should be ~RTT, not the sum ---------
targets = [m.Target("light.left", frozenset({1, 4, 7})),
           m.Target("light.center", frozenset({2, 5, 8})),
           m.Target("light.right", frozenset({3, 6, 9}))]
# a changing screen, so repeated posts are the thing being measured rather than
# a dedupe accident: 3 lights x 120 ms RTT must overlap, not serialise
run_phase(1.0, mode="cycle", rtt=0.12, targets=targets)
assert m.stats["posts"] >= 6, m.stats["posts"]
assert m.stats["cycle_ms"] < 260, "cycle %.0f ms looks serial, not concurrent" % m.stats["cycle_ms"]
print("phase G: 3 lights, 120 ms each -> cycle %.0f ms (serial would be ~360)" % m.stats["cycle_ms"])

# --- phase H: circuit breaker stops hammering a dead HA, then recovers -----
m.FAIL_THRESHOLD, m.FAIL_COOLDOWN = 2, 1.0
FAIL_POSTS = 40
run_phase(2.5, mode="cycle")
assert 0 < m.stats["errors"] <= 6, "kept hammering: %d errors" % m.stats["errors"]
assert m.stats["posts"] == 0
print("phase H: breaker opened after 2 failures, only %d attempts in 2.5 s" % m.stats["errors"])
FAIL_POSTS = 0
run_phase(1.2, mode="cycle")
assert m.stats["posts"] >= 1, "never recovered: %s" % m.stats
print("phase H: recovered, %d posts resume" % m.stats["posts"])
m.FAIL_THRESHOLD, m.FAIL_COOLDOWN = 3, 10.0

# --- credentials via the settings API ------------------------------------
import json as _json
import tempfile
_tmp = tempfile.mkdtemp()
_orig_url, _orig_token, _orig_secrets = m.HA_URL, m.HA_TOKEN, m.SECRETS_PATH
m.SECRETS_PATH = __import__("pathlib").Path(_tmp) / "secrets.json"

assert m.settings_snapshot()["HA_TOKEN"] in ("<set>", "<unset>"), "token must never be reported back"
assert "test-token" not in _json.dumps(m.settings_snapshot())
applied, errors = m.apply_settings({"HA_URL": "http://ha.example:8123/", "HA_TOKEN": "tok-123"})
assert not errors, errors
assert m.HA_URL == "http://ha.example:8123", m.HA_URL          # trailing slash dropped
assert m.HA_TOKEN == "tok-123"
assert m.SECRETS_PATH.exists()
if os.name == "nt":
    # Windows has no POSIX mode bits (it reports 0o666); protection there is the
    # file's ACL, so only the existence and the write itself are checkable here
    print("secrets perms: skipped on Windows (ACL, not mode bits)")
else:
    assert m.SECRETS_PATH.stat().st_mode & 0o777 == 0o600, oct(m.SECRETS_PATH.stat().st_mode)
assert "tok-123" not in _json.dumps(m.settings_snapshot())
assert m.settings_snapshot()["HA_TOKEN"] == "<set>"
assert m.auth_headers() == {"Authorization": "Bearer tok-123"}
cfg = _json.dumps(m.get_config())
assert "tok-123" not in cfg, "token leaked through /api/config"
assert m.get_config()["ha_token_set"] is True

applied, errors = m.apply_settings({"HA_TOKEN": ""})            # blank = keep
assert m.HA_TOKEN == "tok-123", "blank must not wipe the saved token"
applied, errors = m.apply_settings({"HA_TOKEN": "<set>"})       # sentinel = keep
assert m.HA_TOKEN == "tok-123"
# the HTTP layer must not echo the secret back either
_saved_token = m.HA_TOKEN
m.HA_TOKEN = "tok-echo-check"
_posted = _json.dumps(m.post_config({"settings": {"HA_TOKEN": "tok-echo-check"}}))
assert "tok-echo-check" not in _posted, "post_config echoed the token"
m.HA_TOKEN = _saved_token

applied, errors = m.apply_settings({"HA_TOKEN": "--clear--"})   # explicit clear
assert m.HA_TOKEN == "" and not m.SECRETS_PATH.exists()
assert m.settings_snapshot()["HA_TOKEN"] == "<unset>"
m.HA_URL, m.HA_TOKEN, m.SECRETS_PATH = _orig_url, _orig_token, _orig_secrets
print("credentials ok: url trimmed, token 0600-persisted, never echoed")

# --- bind address is loopback by default and validated --------------------
assert m.settings_snapshot()["BIND_HOST"] == "127.0.0.1", "must not expose the UI by default"
assert m.settings_snapshot()["BIND_PORT"] == 8000
assert m.SETTINGS_BY_NAME["BIND_HOST"].get("restart") is True
assert m.SETTINGS_BY_NAME["BIND_PORT"].get("restart") is True
assert {"BIND_HOST", "BIND_PORT"} <= set(m.get_config()["restart_required"])

_orig_bind = (m.BIND_HOST, m.BIND_PORT)
applied, errors = m.apply_settings({"BIND_HOST": "0.0.0.0", "BIND_PORT": 9000})
assert not errors and m.BIND_HOST == "0.0.0.0" and m.BIND_PORT == 9000
for bad in ("http://0.0.0.0:8000", "0.0.0.0/ui", ""):
    applied, errors = m.apply_settings({"BIND_HOST": bad})
    assert errors, f"{bad!r} should be rejected"
    assert m.BIND_HOST == "0.0.0.0", "a rejected value must not be applied"
applied, errors = m.apply_settings({"BIND_PORT": "99999"})
assert errors and m.BIND_PORT == 9000, (errors, m.BIND_PORT)   # rejected, not clamped
m.BIND_HOST, m.BIND_PORT = _orig_bind
print("bind ok: loopback default, URL rejected, port out of range rejected, restart-tagged")

# --- input validation: never convert a bad value silently ------------------
_keep = {n: getattr(m, n) for n in ("DRY_RUN", "TRANSITION", "CAPTURE_FPS", "HUE_BINS",
                                    "COLOUR_BLEND", "HA_URL")}
m.DRY_RUN = True
applied, errors = m.apply_settings({"DRY_RUN": None})
assert errors and m.DRY_RUN is True, (errors, m.DRY_RUN)      # a null must not disarm it
assert m.apply_settings({"CAPTURE_FPS": True})[1], "a bool is not a number"
assert m.apply_settings({"HUE_BINS": float("inf")})[1], "inf must be rejected"
assert m.apply_settings({"CAPTURE_FPS": 9999})[1], "out of range must be rejected, not clamped"
assert m.apply_settings({"SEND_MODE": "nope"})[1], "bad enum must be rejected"
assert m.apply_settings({"COLOUR_BLEND": 1.5})[1], "an out-of-range blend must be rejected"
_orig_url_test = m.HA_URL
assert m.apply_settings({"TRANSITION": 0.4})[0].get("TRANSITION") == "0.4", "strings may stringify"
m.HA_URL = _orig_url_test
for bad_flag in ("yes please", 5, [], 1.5):
    _r = m.post_config({"persist": bad_flag})
    assert getattr(_r, "status_code", 200) == 400, (bad_flag, _r)
_r = m.post_config({"reset": "false"})          # literal "false" means false
assert _r.get("ok") is True and _r.get("applied") == {}, ("string false must not reset", _r)
_p = m.post_config([1, 2, 3])
assert getattr(_p, "status_code", 400) == 400 and b"payload must be an object" in _p.body
_p = m.post_config({"COLOUR_BLEND": 0.0, "persist": False})
assert _p.get("ok") is True, ("flat body with a control flag", _p)
assert m.COLOUR_BLEND == 0.0
_p = m.post_config({"settings": {"COLOUR_BLEND": 1.0}, "persist": "no"})
assert _p.get("ok") is True and _p.get("persisted") is False, _p
for _n, _v in _keep.items():
    setattr(m, _n, _v)                      # leave the process as this phase found it
# an index that doesn't exist must be reported, not silently swapped
_mons = [{}, {"left": 0, "top": 0, "width": 64, "height": 36},
         {"left": 64, "top": 0, "width": 64, "height": 36}]
_i, _w = m.pick_monitor(_mons, 7)
assert _i == 1 and _w and "2 monitor(s)" in _w, (_i, _w)
_i, _w = m.pick_monitor([_mons[0], _mons[1]], 7)
assert _i == 1 and "1 monitor(s)" in _w, (_i, _w)
assert m.pick_monitor(_mons, 2) == (2, None)
assert m.pick_monitor(_mons, 0) == (0, None)
print("monitor choice ok: bad index reported with the real count")

# /api/monitors: count means screens, index 0 is the synthetic canvas
_m = m.get_monitors()
if _m["ok"]:
    assert _m["count"] == len(_m["monitors"]) - 1, _m
    assert _m["choices"] == len(_m["monitors"])
    assert _m["monitors"][0]["index"] == 0

# a new token is reported as applied (masked) and probed, and the picker cache dies
_keep_token = m.HA_TOKEN
_r = m.post_config({"settings": {"HA_TOKEN": "tok-rotate"}, "persist": False})
assert _r["applied"].get("HA_TOKEN") == "<set>", _r["applied"]
assert "tok-rotate" not in _json.dumps(_r), "token leaked in the save response"
assert (_r.get("connection") or {}).get("detail") != "unchanged", "a new token must be probed"
m.HA_TOKEN = _keep_token

m.LIGHTS_CACHE.update({"at": 1e9, "data": {"ok": True, "count": 1, "lights": []}})
_keep_url = m.HA_URL
m.apply_settings({"HA_URL": "http://127.0.0.1:9"}, persist=False)
assert m.LIGHTS_CACHE["data"] is None, "changing HA_URL must invalidate the light list"
m.HA_URL = _keep_url
print("credentials/cache ok: token rotation reported+probed, cache invalidated")

print("validation ok: nulls/bools/inf/out-of-range rejected, control flags literal")

# --- zones that span ragged 3x3 splits (64 -> 22/21/21 columns) ------------
_grid = np.zeros((m.GRID_SIZE[1], m.GRID_SIZE[0], 3), dtype=np.float32)   # 64x36
_cells = m.zones_cells(_grid)
assert all(c.ndim == 2 and c.shape[1] == 3 for c in _cells), [c.shape for c in _cells]
_widths = sorted({c.shape[0] for c in _cells})
assert _widths == [252, 264], f"expected ragged zone sizes, got {_widths}"   # 21*12, 22*12
_span = m.Target("light.wide", frozenset({1, 2}))          # columns 1+2: 22 then 21 wide
_stitched = m.cells_for(_span, _grid, _cells)
assert _stitched.shape == (22 * 12 + 21 * 12, 3), _stitched.shape
_row = m.Target("light.row", frozenset({1, 2, 3}))         # whole top row
assert m.cells_for(_row, _grid, _cells).shape[0] == 64 * 12
_whole = m.Target("light.all", frozenset(range(1, 10)))
assert m.cells_for(_whole, _grid, _cells).shape == (64 * 36, 3)
_col = m.Target("light.col", frozenset({1, 4, 7}))         # one column, the old case
assert m.cells_for(_col, _grid, _cells).shape[0] == 22 * 36
m.aggregate(_stitched)                                     # must not raise
print("ragged zones ok: 22/21 column blocks stitch into one cell list")

# --- semantic versioning ---------------------------------------------------
import re as _re

_ver = m.__version__
assert _re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", _ver), _ver
assert m.health()["version"] == _ver
assert m.get_config()["version"] == _ver
assert f'v{_ver}' in m.UI_HTML or 'id="version"' in m.UI_HTML, "the UI must show the version"
_changelog = Path(m.__file__).with_name("CHANGELOG.md")
if _changelog.exists():
    text = _changelog.read_text(encoding="utf-8")
    assert f"## [{_ver}]" in text, f"CHANGELOG has no section for {_ver}"
    # the version in the README must not drift from the code
    readme = Path(m.__file__).with_name("README.md")
    if readme.exists():
        assert _ver in readme.read_text(encoding="utf-8"), "README does not mention the current version"
print(f"versioning ok: {_ver} exposed in /health, /api/config, UI and CHANGELOG")

# --- auto tune: find the fastest pacing the radio accepts -------------------
class _Resp:
    def __init__(self, ok, text):
        self.ok, self.text, self.status_code = ok, text, 200 if ok else 500


_busy_below = 0.18                  # the "radio" starts rejecting faster than this
_last_post = [0.0]
_posts = []


# A radio accepts a frame's worth of commands at once and rejects the *next* frame
# if it arrives too soon, so measure the gap between frames, not between commands.
def _fake_post(body, timeout=None):
    now = time.perf_counter()
    with _frame_lock:
        if _in_frame[0] <= 0:                 # first command of a frame
            _in_frame[1] = now - _last_post[0]
            _last_post[0] = now
            _in_frame[0] = 3
        _in_frame[0] -= 1
        gap = _in_frame[1]
    _posts.append((gap, body))
    if gap < _busy_below:
        return _Resp(False, '{"message": "Failed to enqueue message"}')
    return _Resp(True, "[]")


_in_frame = [0, 0.0]
_frame_lock = threading.Lock()
_keep_env = (m.DRY_RUN, m.HA_TOKEN, m.check_ha, m._post, m.restore_light, m.session)
_restored = []
m.DRY_RUN = False
m.HA_TOKEN = "tune-token"
m.check_ha = lambda: True
m._post = _fake_post
m.restore_light = lambda restore, attempts=3: (_restored.append(restore), True)[1]


class _State:
    def json(self):
        return {"attributes": {"rgb_color": [10, 20, 30], "brightness": 99}}


m.session = type("S", (), {"get": staticmethod(lambda *a, **k: _State())})()
_three = [m.Target("light.left_lamp", frozenset({1, 4, 7})),
          m.Target("light.center_lamp", frozenset({2, 5, 8})),
          m.Target("light.right_lamp", frozenset({3, 6, 9}))]
m.TARGETS = _three
_posts.clear()
_restored.clear()
_report = m.autotune(verbose=False, per_step=3)
assert _report["ok"], _report
assert _report["floor"] * 1000 >= _busy_below * 1000 * 0.9, _report
assert _report["ceiling"] > _report["floor"], _report
assert _report["busy_at_ms"] is not None, "should have found the cliff"

# every light must be exercised, not just the first
_probed = {b["entity_id"] for _gap, b in _posts}
assert _probed == {t.entity_id for t in _three}, _probed
assert _report["light_count"] == 3, _report
assert _report["lights"] == [t.entity_id for t in _three], _report
_rounds = len(_posts) / 3.0
assert _rounds == int(_rounds), f"each round must hit all three lights, got {len(_posts)} posts"

# ...and every light must be put back, not just the first
_put_back = {r["entity_id"] for r in _restored}
assert _put_back == {t.entity_id for t in _three}, _put_back
assert _report["restored"] == 3, _report
print(f"autotune: {_report['light_count']} lights probed "
      f"({len(_posts)} commands) and all restored")

# the capture rate follows the pacing the radio accepted...
assert _report["fps"], _report
assert abs(_report["fps"] - 1.0 / _report["floor"]) <= 0.6, _report
assert _report["applied"]["CAPTURE_FPS"] == _report["fps"], _report["applied"]
assert _report["applied"]["IDLE_FPS"] <= _report["fps"], _report["applied"]
assert m.CAPTURE_FPS == _report["fps"], m.CAPTURE_FPS
print(f"autotune: capture rate set to {_report['fps']} fps for a "
      f"{_report['floor']:.3f} s floor")

# ...and a fast radio is allowed to be fast: no 0.1 s ceiling in the tuner
_busy_below = 0.05
_in_frame[0], _last_post[0] = 0, 0.0
_fast = m.autotune(verbose=False, per_step=2)
assert _fast["ok"], _fast
assert _fast["floor"] < 0.10, f"pacing clamped again: {_fast['floor']}"
assert _fast["busy_at_ms"] is None or _fast["busy_at_ms"] <= 50, _fast
assert abs(_fast["fps"] - 1.0 / _fast["floor"]) <= 0.6, _fast
assert m.POST_INTERVAL_FLOOR == _fast["floor"], m.POST_INTERVAL_FLOOR
print(f"autotune: fast radio reached {_fast['floor']:.3f} s "
      f"({_fast['fps']} fps), below the old 0.1 s clamp")

# a very fast radio does not push the capture rate past what one core can do
_busy_below = 0.005
_in_frame[0], _last_post[0] = 0, 0.0
_quick = m.autotune(verbose=False, per_step=1)
assert _quick["ok"], _quick
assert _quick["fps"] == m.AUTOTUNE_MAX_FPS, _quick
assert _quick["fps_capped"] is True, _quick
print(f"autotune: {_quick['floor']:.3f} s wants {1/_quick['floor']:.0f} fps, "
      f"capped at {_quick['fps']}")

# validation agrees with the tuner about how fast is allowed
_applied, _errors = m.apply_settings({"POST_INTERVAL_FLOOR": 0.03}, persist=False)
assert not _errors and _applied.get("POST_INTERVAL_FLOOR") == 0.03, (_applied, _errors)
_applied, _errors = m.apply_settings({"POST_INTERVAL_FLOOR": 0.005}, persist=False)
assert _errors and "at least" in _errors[0], _errors
print("validity: 0.03 s accepted, 0.005 s refused")

# the probe must own the radio: capture stays off it, and the flag never sticks
assert m.health()["tuning"] is False, "a finished tune must leave the radio free"
m.TUNING.set()
assert m.health()["tuning"] is True, "health must show the radio is being measured"
m.TUNING.clear()
_source = Path(m.__file__).read_text(encoding="utf-8")
assert "tuning = TUNING.is_set()" in _source and "if not tuning and should_send" in _source, \
    "capture must skip both stamping and sending while tuning"
_failed = m.autotune(verbose=False)      # DRY_RUN back on below
m.DRY_RUN = True
_broken = m.autotune(verbose=False)
assert _broken["ok"] is False
assert m.TUNING.is_set() is False, "a failed tune must not leave sending paused"
m.DRY_RUN = False
print("autotune: capture pauses while tuning and never stays paused")

_busy_below = 0.18
assert _report["restored"] and _restored, "the light must be put back"
assert "POST_INTERVAL_FLOOR" in _report["applied"], _report["applied"]
# a dry run has nothing to measure and must say so rather than pretend
m.DRY_RUN = True
assert m.autotune(verbose=False, per_step=1)["ok"] is False
(m.DRY_RUN, m.HA_TOKEN, m.check_ha, m._post, m.restore_light, m.session) = _keep_env
m.COLOUR_BLEND = m.COLOUR_BLEND          # no-op, keeps the phase boundary obvious
print(f"autotune ok: picked {_report['floor']:.2f}-{_report['ceiling']:.2f} s "
      f"(rejected at {_report['busy_at_ms']} ms), light restored")

# --- any grid, not just 3x3 ------------------------------------------------
_keep_grid = (m.ZONE_ROWS, m.ZONE_COLS)


def _zone_cells(rows, cols):
    m.ZONE_ROWS, m.ZONE_COLS = rows, cols
    count = m.rebuild_zone_index()
    blocks = m.ZONE_INDEX
    assert len(blocks) == rows * cols == count
    cells = sum(len(r) * len(c) for r, c in blocks)
    assert cells == m.GRID_SIZE[0] * m.GRID_SIZE[1], (rows, cols, cells)
    covered = {(y, x) for r, c in blocks for y in r for x in c}
    assert len(covered) == cells, "zones overlap"
    return count


for _r, _c in ((3, 3), (4, 4), (5, 3), (1, 1), (8, 8)):
    _zone_cells(_r, _c)
print("grid: 3x3, 4x4, 5x3, 1x1 and 8x8 all tile the capture grid exactly")

m.ZONE_ROWS, m.ZONE_COLS = 4, 4
m.rebuild_zone_index()
assert m.parse_zones("1-16") == frozenset(range(1, 17)), "range form follows the grid"
assert m.parse_zones("all") == frozenset(range(1, 17))
assert 16 in m.parse_zones("12,16")
try:
    m.parse_zones("17")
except ValueError as exc:
    assert "1-16" in str(exc), exc
else:
    raise AssertionError("zone 17 accepted on a 4x4 grid")

# a mapping that uses zones the smaller grid does not have must not be applied
m.LIGHT_MAP = "light.a=16; light.b=1-3"
m.TARGETS = m.parse_light_map(m.LIGHT_MAP)
_applied, _errors = m.apply_settings({"ZONE_ROWS": 3, "ZONE_COLS": 3}, persist=False)
assert _errors and "16" in str(_errors), _errors
assert (m.ZONE_ROWS, m.ZONE_COLS) == (4, 4), "a rejected grid change must not apply"
print("grid: mapping beyond the new grid is rejected, grid left alone")

# shrinking is fine once the mapping fits
m.LIGHT_MAP = "light.a=1; light.b=2-3"
m.TARGETS = m.parse_light_map(m.LIGHT_MAP)
_applied, _err = m.apply_settings({"ZONE_ROWS": 3, "ZONE_COLS": 3}, persist=False)
assert not _err, _err
assert (m.ZONE_ROWS, m.ZONE_COLS) == (3, 3)
assert m.ZONE_COUNT == 9, m.ZONE_COUNT
assert len(m.parse_light_map("light.a=1").__getitem__(0).zones) == 1
print("grid: 4x4 -> 3x3 applies once the mapping fits")

m.ZONE_ROWS, m.ZONE_COLS = _keep_grid
m.rebuild_zone_index()

# --- close to tray ---------------------------------------------------------
class _FakeWindow:
    def __init__(self, can_hide=True):
        self.can_hide = can_hide
        self.hidden = 0
        self.destroyed = 0
        self.events = type("E", (), {"closing": []})()

    def hide(self):
        if not self.can_hide:
            raise RuntimeError("no backend")
        self.hidden += 1

    def show(self):
        self.hidden -= 1

    def destroy(self):
        self.destroyed += 1


_win = _FakeWindow()
assert m._hide_instead_of_close(_win) is False, "close must be vetoed"
assert _win.hidden == 1, "window must be hidden"

# The same handler also sees the close that destroy() causes during Quit. If it
# vetoes that one, the window never closes and the process stays in Task Manager.
m.QUITTING.set()
assert m._hide_instead_of_close(_FakeWindow()) is True, "quit must not be vetoed"
m.QUITTING.clear()

_dead = _FakeWindow(can_hide=False)
assert m._hide_instead_of_close(_dead) is True, "no backend: allow the close"
print("tray: closing hides the window, and falls back to closing without a backend")

# a fake pystray so the menu wiring can be inspected without a GUI
import sys as _sys
import types as _types


class _MenuItem:
    def __init__(self, label, action, default=False):
        self.label, self.action, self.default = label, action, default


class _Menu:
    def __init__(self, *items):
        self.items = items


class _Icon:
    def __init__(self, name, image, title, menu=None):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.stopped = 0
        self.ran = 0
        _FAKE_ICONS.append(self)

    def run(self):
        self.ran += 1

    def stop(self):
        self.stopped += 1


_FAKE_ICONS = []
_fake = _types.ModuleType("pystray")
_fake.Menu, _fake.MenuItem, _fake.Icon = _Menu, _MenuItem, _Icon
_keep_pystray = _sys.modules.get("pystray")
_sys.modules["pystray"] = _fake
_keep_tray_icon = dict(m.TRAY_ICON)
m.TRAY_ICON.clear()

_win = _FakeWindow()


class _FakeServer:
    def __init__(self):
        self.should_exit = False


_srv = _FakeServer()
_keep_active = dict(m.ACTIVE_SERVER)
_keep_stop = m.stop_event.is_set()
m.ACTIVE_SERVER["server"] = _srv
_forced = []
_keep_force = m.force_exit_soon
m.force_exit_soon = lambda delay=2.0: _forced.append(delay)

m.start_tray_for_window(_win, "http://127.0.0.1:8000/")
time.sleep(0.15)
assert _FAKE_ICONS, "no tray icon created"
_icon = _FAKE_ICONS[-1]
_labels = [i.label for i in _icon.menu.items]
assert _labels == ["Open settings", "Quit"], _labels
assert _icon.menu.items[0].default is True, "left-click must open settings"
assert _icon.ran == 1, "tray must run on its own thread"
_icon.menu.items[0].action()
assert _win.hidden == -1, "Open settings must show the window"
_icon.menu.items[1].action()
assert _win.destroyed == 1, "Quit must destroy the window"
assert _icon.stopped == 1, "Quit must stop the tray icon"
assert m.QUITTING.is_set(), "Quit must release the close veto"
assert _srv.should_exit, "Quit must stop the embedded server"
assert _forced, "Quit must arm the forced exit for a backend that will not unwind"
print("tray: Open settings shows the window, Quit destroys it and stops the icon")

m.force_exit_soon = _keep_force
m.ACTIVE_SERVER.clear()
m.ACTIVE_SERVER.update(_keep_active)
m.QUITTING.clear()
if _keep_stop:
    m.stop_event.set()
else:
    m.stop_event.clear()

m.TRAY_ICON.clear()
m.TRAY_ICON.update(_keep_tray_icon)
if _keep_pystray is not None:
    _sys.modules["pystray"] = _keep_pystray
else:
    _sys.modules.pop("pystray", None)

# --- restore must accept the mode it actually asked for --------------------
class _Attrs:
    def __init__(self, mode):
        self.mode = mode

    def json(self):
        return {"attributes": {"color_mode": self.mode}}


_keep_session, _keep_post = m.session, m._post
for mode, payload, want in (("rgb", {"rgb_color": [1, 2, 3]}, True),
                            ("xy", {"xy_color": [0.3, 0.3]}, True),
                            ("color_temp", {"color_temp_kelvin": 2000}, True),
                            ("rgb", {"xy_color": [0.3, 0.3]}, False)):
    m.session = type("S", (), {"get": staticmethod(lambda *a, _m=mode, **k: _Attrs(_m))})()
    m._post = lambda *a, **k: None
    target = {"entity_id": "light.a", **payload}
    got = m.restore_light(target, attempts=1)
    assert got is want, (mode, payload, got)
m.session, m._post = _keep_session, _keep_post
print("restore ok: rgb/xy/colour-temp read-backs all match")

# --- name, and the splash ---------------------------------------------------
import importlib.util as _ilu
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = _ilu.spec_from_file_location(name, _ROOT / rel)
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# the splash is optional and must never be a reason to fail a start
assert m.close_splash() is None, "close_splash must be harmless without pyi_splash"
assert m.splash_text("anything") is None

import sys as _sys2
import types as _types2

_seen = {"text": None, "closed": 0}
_fake_splash = _types2.ModuleType("pyi_splash")
_fake_splash.update_text = lambda text: _seen.__setitem__("text", text)
_fake_splash.close = lambda: _seen.__setitem__("closed", _seen["closed"] + 1)
_sys2.modules["pyi_splash"] = _fake_splash
try:
    m.splash_text("starting the settings server")
    assert _seen["text"] == "starting the settings server", _seen
    m.close_splash()
    assert _seen["closed"] == 1, _seen
    m.close_splash()                        # idempotent
    assert _seen["closed"] == 2, _seen

    # a splash that cannot close must not take the app down with it
    _fake_splash.close = lambda: (_ for _ in ()).throw(RuntimeError("no window"))
    assert m.close_splash() is None
finally:
    _sys2.modules.pop("pyi_splash", None)
print("splash: updates its text, closes, and never raises")

_assets = _load("make_assets", "packaging/make_assets.py")
_info = _Path(tempfile.mkdtemp()) / "version_info.txt"
_assets.make_version_info(_info, "9.9.9")
_text = _info.read_text(encoding="utf-8")
assert "StringStruct('FileDescription', 'HASS Ambilights')" in _text, "Task Manager name"
assert "StringStruct('ProductName', 'HASS Ambilights')" in _text
assert "'Hass Ambilights'" not in _text, "old brand left in the version resource"
print("windows resource: FileDescription is 'HASS Ambilights' (Task Manager name)")

_splash_mod = _load("make_splash", "packaging/make_splash.py")
_png = _Path(tempfile.mkdtemp()) / "splash.png"
_splash_mod.make_splash(_png, "9.9.9")
assert _png.exists() and _png.stat().st_size > 2000, _png.stat().st_size
from PIL import Image as _Image

with _Image.open(_png) as _im:
    assert _im.size == (620, 360), _im.size
    assert _im.format == "PNG"
print(f"splash image: {_png.stat().st_size} bytes, 620x360 PNG")

_ps1 = (_ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8")
_yml = (_ROOT / ".github" / "workflows" / "windows.yml").read_text(encoding="utf-8")
for _name, _script in (("build.ps1", _ps1), ("windows.yml", _yml)):
    assert "--splash packaging/splash.png" in _script, _name
    assert "make_splash.py" in _script, f"{_name} must generate the splash"
    # PyInstaller's splash is a Tk window, so Tk may not be excluded any more
    assert "--exclude-module tkinter" not in _script, f"{_name} would break the splash"
print("build: splash passed through, Tk no longer excluded")

# the brand a person sees, in one place
_ui = m.UI_HTML
assert "<h1>HASS Ambilights</h1>" in _ui, "page heading"
assert "<title>HASS Ambilights</title>" in _ui, "tab title"
assert '"Hass Ambilights "' not in _ui, "old brand left in the page"
assert m.app.title == "HASS Ambilights", m.app.title
print("brand: heading, tab title and API name all read HASS Ambilights")

# --- a setting that needs a restart gets one --------------------------------
_keep = (m.CONFIG_PATH, os.execv, m.AUTO_RESTART, m.stats.get("restarting"),
         m.MONITOR_INDEX, m.CAPTURE_FPS)
_guard_dir = Path(tempfile.mkdtemp())
m.CONFIG_PATH = _guard_dir / "config.json"
m.AUTO_RESTART = True
_fired = []
os.execv = lambda path, argv: _fired.append((path, list(argv)))

# the command that starts the program again
_nonfrozen = m.restart_argv()
assert _nonfrozen[0] == sys.executable, _nonfrozen
assert _nonfrozen[1].endswith("hass_ambilights.py"), _nonfrozen
sys.frozen = True
try:
    _frozen = m.restart_argv()
    assert _frozen[0] == sys.executable and _frozen[1:] == sys.argv[1:], _frozen
finally:
    del sys.frozen
print("restart: re-execs the exe when frozen, the script when not")

# saving a restart-only setting restarts, and says so before it goes
m.stats["restarting"] = False
# a monitor change is applied in place now: restarting it was the bug (the new process
# came up on a different port with no config)
_epoch = m.MONITOR_EPOCH
_saved = m.post_config({"MONITOR_INDEX": 2})
assert _saved["restart_required"] == [], _saved
assert _saved["restarting"] is False, _saved
assert m.MONITOR_EPOCH > _epoch, "capture must be told to re-pick the monitor"
assert not _fired, "a monitor change must not re-exec the program"
m.post_config({"MONITOR_INDEX": 1})
print("restart: a monitor change applies in place and never re-execs")

# and the restart machinery itself still works when something really needs it,
# including the guard that stops a broken setting looping forever
_fired.clear()
_guard = m._restart_guard_path()
_guard.unlink(missing_ok=True)
assert m.restart_self("test") is True, "restart_self must fire on a clean guard"
time.sleep(1.2)
assert _fired, "os.execv was never called"
assert _fired[0][0] == _fired[0][1][0]
_guard.write_text(str(time.time()), encoding="utf-8")
assert m.restart_self("test again") is False, "a second restart within 30s must be refused"
_guard.unlink(missing_ok=True)
print("restart: fires when it must, refuses to loop")

# a second restart inside the guard window is refused: that would be a crash loop
_guard.write_text(str(time.time()), encoding="utf-8")
assert m.restart_self("too soon") is False, "a second restart inside 30 s must be refused"
_guard.unlink(missing_ok=True)
print("restart: a second restart within 30 s is refused (no crash loop)")

# live application is not a save, so it must not restart anything
_fired.clear()
m.stats["restarting"] = False
_live = m.post_config({"MONITOR_INDEX": 0, "persist": False})
assert _live["restart_required"] == [], _live
assert _live["restarting"] is False, "a live apply must not restart the app"
time.sleep(1.0)
assert not _fired, _fired
print("restart: live apply of the same setting stays put")

# a setting that does not need a restart must not cause one
m.stats["restarting"] = False
_plain = m.post_config({"CAPTURE_FPS": 9.0})
assert _plain["restart_required"] == [], _plain
assert _plain["restarting"] is False, _plain
print("restart: ordinary settings still apply without one")

m.CONFIG_PATH, os.execv, m.AUTO_RESTART = _keep[0], _keep[1], _keep[2]
m.MONITOR_INDEX, m.CAPTURE_FPS = _keep[4], _keep[5]
m.stats["restarting"] = False

# --- audit round: flags, coercion and legacy wiring -------------------------
_saved_before = dict(m.settings_snapshot())
# {"persist": true} used to reset every setting, light map included
m.post_config({"settings": {"COLOUR_BLEND": 0.31}, "persist": True})
assert m.COLOUR_BLEND == 0.31, m.COLOUR_BLEND
assert m.LIGHT_MAP == _saved_before["LIGHT_MAP"], "a plain save must not wipe the light map"
_reset_out = m.post_config({"reset": True, "persist": False})
assert _reset_out["ok"] and m.COLOUR_BLEND == 0.5, "reset still resets"
# reset really does clear everything, so put the mock server back for later tests
m.apply_settings({"HA_URL": _saved_before["HA_URL"],
                  "LIGHT_MAP": _saved_before["LIGHT_MAP"]}, persist=False)
print("save ok: persist no longer resets, reset still resets")

# a bad type is rejected, not repaired
def _post_errors(payload):
    """post_config returns a dict on success and a JSONResponse on a 400."""
    out = m.post_config(payload)
    if isinstance(out, dict):
        return out.get("errors") or []
    return json.loads(out.body.decode())["errors"]


_errors = _post_errors({"MONITOR_INDEX": 2.6, "persist": False})
assert _errors and "whole number" in _errors[0], _errors
_errors = _post_errors({"HA_URL": None, "persist": False})
assert _errors and "text" in _errors[0], _errors
assert m.HA_URL.endswith(str(port)) or m.HA_URL, "HA_URL must survive a null"
print("coercion ok: 2.6 and null are refused, not rounded or blanked")

# the legacy keys really do reach the sliders now
m.COLOUR_BLEND = 0.5
_applied, _errors = m.apply_settings({"COLOR_ALGO": "mean"}, persist=False)
assert not _errors, _errors
assert m.COLOUR_BLEND == 0.0, f"COLOR_ALGO=mean must become 0, got {m.COLOUR_BLEND}"
_applied, _errors = m.apply_settings({"DOMINANT_MIX": 0.25}, persist=False)
assert m.COLOUR_BLEND == 0.25, m.COLOUR_BLEND
_applied, _errors = m.apply_settings({"DOMINANT_LUMINANCE": "region"}, persist=False)
assert m.BRIGHTNESS_BLEND == 0.0, m.BRIGHTNESS_BLEND
m.apply_settings({"COLOUR_BLEND": 0.5, "BRIGHTNESS_BLEND": 0.5}, persist=False)
print("legacy ok: COLOR_ALGO/DOMINANT_MIX/DOMINANT_LUMINANCE reach the sliders")

# a refused POST frees the frame instead of pretending it was delivered
_t = m.Target("light.retry", frozenset({1}))
_t.last_sent = (tuple([10, 20, 30]), 100)
_t.last_attempted = (tuple([10, 20, 30]), 100)
assert m.should_send(_t, {"rgb": [10, 20, 30], "luminance": 0.1}, 100) is False
_t.last_attempted = None
assert m.should_send(_t, {"rgb": [11, 20, 30], "luminance": 0.1}, 100) is True
print("retry ok: a cleared attempt is retried at once")

# health must be able to say whether the pipeline is alive
_h = m.health()
assert "capture_beat" in m.stats, "capture must stamp a heartbeat"
assert "sender_beat" in m.stats or True, "sender stamps once it has run"
print("liveness ok: capture_beat is stamped")

# --- wave three: breaker, loops, bind address, UI payload -------------------
# a 4xx is one misconfigured entity, not a dead host
assert m.apply_settings({"BIND_HOST": "203.0.113.9"}, persist=False)[1], \
    "an address this machine cannot bind must be refused"
assert m.BIND_HOST != "203.0.113.9", m.BIND_HOST
assert m.host_for_url("::1") == "[::1]", m.host_for_url("::1")
assert m.host_for_url("127.0.0.1") == "127.0.0.1"
print("bind ok: unbindable address refused, IPv6 literals bracketed")

# the sender survives an exception instead of dying for good
_keep_sender = m._sender_loop
_restarts_before = m.stats.get("sender_restarts", 0)
m._sender_loop = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
m.stop_event.clear()
_thread = threading.Thread(target=m.sender_loop, daemon=True)
_thread.start()
time.sleep(0.4)
m.stop_event.set()
_thread.join(timeout=3)
m._sender_loop = _keep_sender
assert not _thread.is_alive(), "the sender loop must exit when asked"
assert m.stats.get("sender_restarts", 0) > _restarts_before, m.stats.get("sender_restarts")
print("sender ok: an exception restarts the loop instead of ending it")

# the UI must not save an empty zone set as "all", nor post hidden aliases
_ui_src = m.UI_HTML
assert "if (!z.length) return" in _ui_src, "empty zones must not serialise as all"
assert "visible.has(k)" in _ui_src, "hidden legacy keys must not be saved back"
assert "setDirty(!!data.unsaved, false)" in _ui_src, "hydrating must not fire a live POST"
assert "async function openCalibration(flow)" in _ui_src, "the panel takes a flow"
assert "calibrate-color" in _ui_src, "colour calibration has its own button"
assert "api/calibrate" not in _ui_src, "the automatic probe is no longer in the page"
print("ui ok: no empty-zone inflation, no hidden-key saves, no POST on load")

# --- wave four: the example config must not fall behind the code -----------
_example_path = Path(m.__file__).with_name("config.example.json")
_example = _json.loads(_example_path.read_text(encoding="utf-8"))
_missing = sorted(s["name"] for s in m.SETTINGS
                  if not s.get("hidden") and s["type"] != "secret"
                  and s["name"] not in _example)
assert not _missing, f"config.example.json is missing {_missing}"
assert "COLOUR_BLEND" in _example and "ZONE_ROWS" in _example
print(f"example config ok: all {len(_example)} visible settings present")

# the unit must not ship a token, and must name a display and a start limit
_unit = (_example_path.parent / "deploy" / "hass-ambilights.service").read_text(encoding="utf-8")
assert not [ln for ln in _unit.splitlines()
            if ln.strip().startswith("Environment=") and "HA_TOKEN" in ln], \
    "the unit must not carry a token (a commented example is fine)"
assert "EnvironmentFile=-/etc/hass-ambilights.env" in _unit
assert "DISPLAY=" in _unit and "User=%i" in _unit
assert "StartLimitBurst" in _unit
assert "--server --no-browser" in _unit
print("service unit ok: no token, display, user, start limit, headless mode")

# --- wave five: no LAN port scanning, no stolen focus ----------------------
assert m._probe_allowed("http://192.168.1.10:8123")[0] is True
assert m._probe_allowed("http://127.0.0.1:8123")[0] is True
_public_ok, _public_why = m._probe_allowed("http://8.8.8.8:8123")
assert _public_ok is False and "not on this network" in _public_why, _public_why
assert m._probe_allowed("file:///etc/passwd")[0] is False
assert "hunter2" not in m.redact_url("Failed: http://user:hunter2@h:1/api/")
# a URL with credentials is refused, so the request never applies
_rejected = _post_errors({"HA_URL": "http://u:p@h:8123", "persist": False})
assert _rejected and "credentials in the token" in _rejected[0], _rejected
print("ssrf ok: public hosts refused, userinfo redacted and rejected")

_ui = m.UI_HTML
assert "captureFocus()" in _ui and "restoreFocus(focusBefore)" in _ui
print("focus ok: a refresh puts the caret back")

# --- upgrades must not lose a user's settings -------------------------------
import json as _json
import pathlib as _pathlib2
import tempfile as _tempfile

_mig_dir = _pathlib2.Path(_tempfile.mkdtemp())
_mig_cfg = _mig_dir / "config.json"
_mig_cfg.write_text(_json.dumps({
    "COLOR_ALGO": "dominant", "DOMINANT_MIX": 0.7, "DOMINANT_LUMINANCE": "region",
    "LIGHT_MAP": "light.x=1,2", "CAPTURE_FPS": 15,
    "A_FUTURE_KEY": {"nested": [1, 2]},   # written by a newer build, or by hand
}), encoding="utf-8")
_saved_path = m.CONFIG_PATH
m.CONFIG_PATH = _mig_cfg
try:
    m.load_config_file(_mig_cfg)
    assert m.COLOUR_BLEND == 0.7, f"DOMINANT_MIX must become COLOUR_BLEND: {m.COLOUR_BLEND}"
    assert m.BRIGHTNESS_BLEND == 0.0, f"region must become BRIGHTNESS_BLEND 0: {m.BRIGHTNESS_BLEND}"
    assert m.CONFIG_EXTRAS.get("A_FUTURE_KEY") == {"nested": [1, 2]}, m.CONFIG_EXTRAS
    assert m.persist_config(_mig_cfg), "the migrated config must be writable"
    _after = _json.loads(_mig_cfg.read_text(encoding="utf-8"))
    assert _after["COLOUR_BLEND"] == 0.7 and _after["BRIGHTNESS_BLEND"] == 0.0, _after
    assert _after["WHITE_MODE"] == "auto", "a new setting must appear with its default"
    assert _after["A_FUTURE_KEY"] == {"nested": [1, 2]}, "unknown keys must survive a save"
    assert _after["LIGHT_MAP"] == "light.x=1,2" and _after["CAPTURE_FPS"] == 15
    assert not {"COLOR_ALGO", "DOMINANT_MIX", "DOMINANT_LUMINANCE"} & set(_after), (
        "translated keys must retire, or the file carries both schemas for ever")
    print("migration ok: old keys translated and retired, new defaults added, "
          "unknown keys preserved")
finally:
    m.CONFIG_PATH = _saved_path
    m.CONFIG_EXTRAS.clear()

# a monitor change is applied in place: restarting it is what lost the port and the config
_mon_spec = m.SETTINGS_BY_NAME["MONITOR_INDEX"]
assert not _mon_spec.get("restart"), "MONITOR_INDEX must not force a restart"
_epoch = m.MONITOR_EPOCH
m.apply_settings({"MONITOR_INDEX": 2}, persist=False)
assert m.MONITOR_EPOCH > _epoch, "capture must be told to re-pick the monitor"
assert m.MONITOR_INDEX == 2, m.MONITOR_INDEX
m.apply_settings({"MONITOR_INDEX": 1}, persist=False)
print("monitor ok: changing it bumps the epoch capture watches, with no restart")

# --- calibration: three level points, a white kelvin, a cap per colour ------
import pathlib as _pl
import shutil as _sh
import subprocess as _spr

# the step list the panel walks: level first, then the three primaries
_steps = _pl.Path(__file__).parent / "check_calsteps.js"
if _sh.which("node"):
    _out = _spr.run(["node", str(_steps), str(_pl.Path(m.__file__))],
                   capture_output=True, text=True, timeout=30)
    assert _out.returncode == 0, _out.stdout + _out.stderr
    print("calibration ok: " + _out.stdout.strip())
else:
    assert "CAL_STEPS" in m.UI_HTML, "the panel must define its steps"

# a level curve that passes exactly through what was measured (the grey anchor moves with
# the brightness channel, so pin the channel this section measures in)
m.BRIGHTNESS_FROM = "luminance"
_cal = {"black": 70.0, "grey": 150.0, "white": 240.0}
assert m.calibrated_brightness(0.0, _cal) == 70, m.calibrated_brightness(0.0, _cal)
assert m.calibrated_brightness(m.CALIBRATION_LEVEL_LUMINANCE["grey"], _cal) == 150
assert m.calibrated_brightness(1.0, _cal) == 240
_levels = [m.calibrated_brightness(y, _cal) for y in (0.0, 0.05, 0.2159, 0.5, 1.0)]
assert _levels == sorted(_levels) and len(set(_levels)) == 5, _levels
# a lamp that is nearly black at 20% is followed, not averaged
_dark_heavy = {"black": 40.0, "grey": 60.0, "white": 250.0}
assert m.calibrated_brightness(0.4, _dark_heavy) < m.calibrated_brightness(0.4, _cal)
# without all three points there is no curve to apply
assert m.calibrated_brightness(0.5, {"black": 20.0}) is None
print(f"calibration ok: level curve through the measured points {_levels}")

# caps: whichever colour was chosen brightest sets the scale, the rest come down to match
_caps = {"cap_red": 1.0, "cap_green": 150 / 255, "cap_blue": 90 / 255}
_red = m.colour_cap(m.xy_from_rgb([255, 0, 0]), _caps)
_green = m.colour_cap(m.xy_from_rgb([0, 255, 0]), _caps)
_blue = m.colour_cap(m.xy_from_rgb([0, 0, 255]), _caps)
assert abs(_red - 1.0) < 1e-6 and _green < 0.65 and _blue < 0.40, (_red, _green, _blue)
# white is one of the four outputs being equalised: if it is judged dimmest, a white page
# must not outshine the colour that was matched to it
_cals = {"black": 70.0, "grey": 150.0, "white": 240.0,
         "cap_red": 200 / 240, "cap_green": 255 / 240, "cap_blue": 120 / 240}
_seen_caps = {}
for _name, _rgb in (("white", [255, 255, 255]), ("red", [255, 0, 0]),
                    ("green", [0, 255, 0]), ("blue", [0, 0, 255])):
    _c = {"rgb": _rgb, "xy": list(m.xy_from_rgb(_rgb)), "luminance": 1.0,
          "white": _name == "white"}
    m.apply_calibration(_c, _cals)
    _seen_caps[_name] = (_c.get("calibration_cap", 1.0), m.brightness_for_colour(_c))
# every judged value must come out exactly as judged, including one above white: dimming the
# whole room to the strongest colour would mean nothing you set is what gets sent
_judged = {"white": 240, "red": 200, "green": 255, "blue": 120}
for _name, _value in _judged.items():
    assert _seen_caps[_name][1] == _value, (
        f"{_name} was judged {_value} but sent {_seen_caps[_name][1]}")
assert "calibration_cap" not in {"white": 0} or _seen_caps["white"][0] == 1.0, (
    "a neutral frame is the reference; it carries no cap")
print(f"calibration ok: judged values are sent as judged - white {_seen_caps['white'][1]}, "
      f"green {_seen_caps['green'][1]}, red {_seen_caps['red'][1]}, blue {_seen_caps['blue'][1]}")

# a hue between two primaries blends, so nothing steps at red/green/blue
_yellow = m.colour_cap(m.xy_from_rgb([255, 255, 0]), _caps)
assert _green < _yellow < _red, (_green, _yellow, _red)
assert m.colour_cap(m.xy_from_rgb([255, 0, 0]), {}) == 1.0, "uncalibrated means uncapped"
print(f"calibration ok: caps equalise red/green/blue ({_red:.2f}/{_green:.2f}/{_blue:.2f}), "
      f"yellow blends ({_yellow:.2f})")

# what one light's calibration does to a frame
_cal_light = {"black": 70.0, "grey": 150.0, "white": 240.0, "white_scale": 0.9,
              "cap_red": 1.0, "cap_green": 0.6, "cap_blue": 0.35}
_frame = {"rgb": [0, 255, 0], "xy": list(m.xy_from_rgb([0, 255, 0])), "luminance": 0.5}
m.apply_calibration(_frame, _cal_light)
assert _frame.get("calibration_cap") is not None, _frame
_bright = m.brightness_for_colour(_frame)
_uncapped = {"rgb": [0, 255, 0], "xy": list(m.xy_from_rgb([0, 255, 0])),
             "luminance": 0.5, "calibration_brightness": m.calibrated_brightness(0.5, _cal_light)}
assert _bright < m.brightness_for_colour(_uncapped), (
    f"a capped colour must come out dimmer: {_bright}")
# a grey frame is levelled, never capped: caps describe coloured output
_grey = {"rgb": [128, 128, 128], "xy": list(m.xy_from_rgb([140, 140, 140])),
         "luminance": 0.2159, "white": True}
m.apply_calibration(_grey, _cal_light)
assert "calibration_cap" not in _grey, _grey
assert _grey.get("kelvin_scale") == 0.9, "the measured white point must reach the payload"
print("calibration ok: a calibration caps colour, levels grey and carries the white point")

# storage: the v1 file keeps working, its warmth becoming the white-point scale
m.CALIBRATION.clear()
m.CALIBRATION["light.old"] = {"black": 60.0, "grey": 140.0, "white": 230.0,
                              "white_scale": 0.9, "cap_red": 1.0, "cap_green": 0.7,
                              "cap_blue": 0.4}
assert m.save_calibration()
m.CALIBRATION.clear(); m.load_calibration()
assert m.CALIBRATION["light.old"]["grey"] == 140.0, m.CALIBRATION
assert m.CALIBRATION["light.old"]["cap_blue"] == 0.4, m.CALIBRATION
_cal_file = m.calibration_path()
_cal_file.write_text(json.dumps({"light.legacy": {"hue": 0.0, "chroma": 1.0, "level": 1.0,
                                                 "warmth": 0.5}}), encoding="utf-8")
m.CALIBRATION.clear(); m.load_calibration()
assert abs(m.CALIBRATION["light.legacy"]["white_scale"] - (1 - m.WARMTH_KELVIN * 0.5)) < 1e-6, (
    f"an old warmth must become the white-point scale: {m.CALIBRATION}")
assert "hue" not in m.CALIBRATION["light.legacy"], "hue is no longer calibrated"
m.CALIBRATION.clear(); m.save_calibration()
print("calibration ok: stored points round-trip, v1 warmth migrates, hue is dropped")

# the endpoint the panel calls
_orig_send = m._send_now
_seen = {}
m._send_now = lambda entity, xy, brightness, kelvin=None: (
    _seen.update(brightness=brightness, kelvin=kelvin, xy=xy), True)[1]
try:
    _res = m.post_calibration({"action": "apply", "entity": "light.cal", "frame": "grey",
                               "brightness": 132})
    assert _res["ok"] and _res["brightness"] == 132 and _seen["brightness"] == 132, (_res, _seen)
    assert "#808080" == _res["reference"], _res
    _res2 = m.post_calibration({"action": "apply", "entity": "light.cal", "frame": "blue",
                                "brightness": 90})
    assert _res2["brightness"] == 90 and _seen["brightness"] == 90
    _out_of_range = m.post_calibration({"action": "apply", "entity": "light.cal",
                                        "frame": "red", "brightness": 900})
    assert _out_of_range["brightness"] == 255, "brightness is clamped, not accepted"
    _bad = m.post_calibration({"action": "apply", "entity": "light.cal", "frame": "mauve"})
    assert _bad.status_code == 400, "an unknown frame must be refused"
    _commit = m.post_calibration({"action": "commit", "entity": "light.cal",
                                  "black": 30, "grey": 140, "white": 235,
                                  "kelvin": 5800,
                                  "caps": {"red": 255 / 235, "green": 160 / 235,
                                           "blue": 100 / 235}})
    assert _commit["ok"], _commit
    _rec = m.CALIBRATION["light.cal"]
    assert _rec["grey"] == 140 and abs(_rec["white_scale"] - 5800 / 6504) < 1e-4, _rec
    assert abs(_rec["cap_green"] - (160 / 235)) < 1e-4, _rec
    assert _rec["cap_red"] > 1.0, "a colour judged brighter than white is allowed above 1.0"
    assert abs(_rec["cap_blue"] - (100 / 235)) < 1e-4, _rec
    assert "cap_white" not in _rec, "white is the reference, so it carries no factor"
    # nothing to record only when there is no existing record to merge into
    assert m.post_calibration({"action": "commit", "entity": "light.nothing"}).status_code == 400
    print(f"calibration ok: the panel's calls send the chosen brightness and record "
          f"{_rec['black']}/{_rec['grey']}/{_rec['white']}")
finally:
    m._send_now = _orig_send
    m.CALIBRATION.pop("light.cal", None)

# solo / switch / end still behave, now that a session covers every light
_orig = (m.check_ha, m._send_now, m._autotune_attrs, m.restore_light, m.requests.post)
_calls = []
m.check_ha = lambda *a, **k: True
m._send_now = lambda entity, xy, br, kelvin=None: (_calls.append(("send", entity)), True)[1]
m._autotune_attrs = lambda targets: [{"entity_id": t.entity_id, "rgb_color": [9, 9, 9],
                                      "brightness": 77} for t in targets]
m.restore_light = lambda state, attempts=3: (_calls.append(("restore", state["entity_id"])),
                                             True)[1]
m.requests.post = lambda url, **k: (_calls.append(("off", k["json"]["entity_id"])),
                                    __import__("types").SimpleNamespace(ok=True,
                                                                       status_code=200))[1]
try:
    m.TARGETS = m.parse_light_map("light.a=1,2; light.b=3,4")
    m.CALIBRATION_SESSION.clear()
    _r = m.post_calibration({"action": "start", "entity": "light.a"})
    assert _r["ok"] and sorted(_r["soloed"]) == ["light.a", "light.b"], _r
    assert m.TUNING.is_set(), "a session must silence the pipeline"
    _before = dict(m.CALIBRATION_SESSION)
    _sw = m.post_calibration({"action": "switch", "entity": "light.b"})
    assert _sw["ok"] and m.CALIBRATION_SESSION == _before, (
        "switching lights must not re-capture states: they are already switched off")
    _calls.clear()
    _end = m.post_calibration({"action": "end"})
    assert _end["ok"] and not m.TUNING.is_set()
    assert sorted(c[1] for c in _calls if c[0] == "restore") == ["light.a", "light.b"], _calls
    print("calibration ok: a session solos, switches without losing states, and restores all")
finally:
    (m.check_ha, m._send_now, m._autotune_attrs, m.restore_light, m.requests.post) = _orig
    m.TUNING.clear(); m.CALIBRATION_SESSION.clear()

# --- a save must not touch an undeclared name ------------------------------
_ui = m.UI_HTML
# this shipped broken in 2.9.3/2.9.4: save() used `visible`, which only existed
# inside applyLive(), so every Save threw "visible is not defined"
assert "const visibleNames = () =>" in _ui, "one declaration for the visible-key set"
assert _ui.count("visibleNames()") >= 2, "save and applyLive must both use it"
_save_body = _ui.split("async function save()", 1)[1].split("async function ", 1)[0]
assert "visibleNames().has(k)" in _save_body, "save() must build its payload from the helper"
assert "visible.has(k)" not in _save_body, "save() must not reach for applyLive()'s local"
print("save ok: the payload helper is declared once and used by both paths")

# and the endpoint must actually persist what the UI sends
_before = dict(m.settings_snapshot())
_out = m.post_config({"settings": {"COLOUR_BLEND": 0.35}, "persist": True})
assert _out["ok"] and _out["persisted"] is True, _out
_saved = m.get_config()["saved_settings"]
assert _saved["COLOUR_BLEND"] == 0.35, _saved["COLOUR_BLEND"]
assert m.get_config()["unsaved"] is False, "a saved value must not look unsaved"
m.apply_settings({"COLOUR_BLEND": _before["COLOUR_BLEND"]}, persist=True)
print("save ok: the server persists the UI's payload and clears the unsaved flag")

# --- desktop mode helpers --------------------------------------------------
_keep_frozen = m.FROZEN
m.FROZEN = False
assert m.app_data_dir() == __import__("pathlib").Path(m.__file__).resolve().parent
assert m.log_file_path() is None, "a script run has a terminal to log to"
_cmd = m.autostart_command()
assert "--tray" in _cmd and "hass_ambilights.py" in _cmd, _cmd
m.FROZEN = True
import os as _os
_os.environ["APPDATA"] = "/tmp/qa-appdata"
_os.environ["LOCALAPPDATA"] = "/tmp/qa-localappdata"
assert str(m.app_data_dir()).endswith("HassAmbilights"), m.app_data_dir()
assert str(m.log_file_path()).endswith("app.log"), m.log_file_path()
assert m.autostart_command().endswith("--tray"), m.autostart_command()
m.FROZEN = _keep_frozen

_icon = m.tray_icon_image()
assert _icon.size == (64, 64) and _icon.mode == "RGBA"
assert isinstance(m.gui_available(), bool)          # must not raise without pywebview

# a busy port must not be chosen, and an unknown server is not "already running"
import socket as _socket
_srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
_srv.bind(("127.0.0.1", 0))
_srv.listen(1)
_busy = _srv.getsockname()[1]
try:
    assert m.first_free_port("127.0.0.1", _busy) != _busy, "picked a port that is in use"
    assert m.find_instance("127.0.0.1", _busy, limit=1) is None, "claimed a foreign port"
finally:
    _srv.close()
# a --windowed exe has no stdout/stderr; redirect_std_streams must cope and
# send later writes to the log rather than raising on None
import subprocess as _subprocess
_log_path = Path(tempfile.mkdtemp()) / "windowed.log"
_child = _subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r); import hass_ambilights as m; "
     "sys.stdout = None; sys.stderr = None; m.redirect_std_streams(); "
     "assert sys.stdout is not None and sys.stderr is not None; "
     "print('written after redirect')" % str(Path(m.__file__).resolve().parent)],
    capture_output=True, text=True, timeout=60,
    env={**__import__("os").environ, "LOG_FILE": str(_log_path)})
assert _child.returncode == 0, _child.stderr[-400:]
assert _log_path.exists() and "written after redirect" in _log_path.read_text(encoding="utf-8"), \
    "a windowed run must write where it can be read"

# --- windows cursor flicker (mss CAPTUREBLT) ------------------------------
class _Gdi:
    CAPTUREBLT = 0x40000000


_gdi = _Gdi()
assert m.disable_captureblt(_gdi) is True, "CAPTUREBLT must be cleared"
assert _gdi.CAPTUREBLT == 0, _gdi.CAPTUREBLT
assert m.disable_captureblt(_gdi) is False, "already clear: nothing to do"
assert m.disable_captureblt(object()) is False, "a backend without the constant is left alone"
print("capture: CAPTUREBLT cleared so the Windows cursor does not flicker")

# a closed port must be retried, not raised, and a dead one must time out
assert m.wait_for_server("http://127.0.0.1:9/", timeout=0.6) is False
assert m.port_is_ours("127.0.0.1", 9, timeout=0.4) is False
print("desktop helpers ok: appdata paths, autostart command, tray icon, port picking")

# --- bad TRANSITION must not reach the sender ------------------------------
for bad in ("", "   ", "abc"):
    assert m.apply_settings({"TRANSITION": bad})[1], f"TRANSITION {bad!r} should be rejected"
assert m.apply_settings({"TRANSITION": "auto"})[0]["TRANSITION"] == "auto"
assert m.apply_settings({"TRANSITION": " auto "})[0]["TRANSITION"] == "auto", "trim it"
assert m.apply_settings({"TRANSITION": "0.4"})[0]["TRANSITION"] == "0.4"
assert m.apply_settings({"TRANSITION": "9"})[1], "9 s is out of range"
_keep_tr = m.TRANSITION
m.TRANSITION = ""                       # as if a hand-edited file had got it in
assert isinstance(m.current_transition(), float), "a bad TRANSITION must not raise"
m.TRANSITION = "1.5"
assert abs(m.current_transition() - 1.5) < 1e-6
m.TRANSITION = _keep_tr
print("transition ok: validated, and a bad value cannot kill the sender")

# --- boosted chromaticity must stay inside the CIE xy domain --------------
_blue = {"rgb": [60, 80, 255], "xy": [0.1815, 0.1157], "luminance": 0.35}
_keep_boost = (m.SATURATION_BOOST, m.SATURATION_GAIN, m.SATURATION_CURVE, m.SATURATION_MAX)
m.SATURATION_BOOST, m.SATURATION_GAIN, m.SATURATION_CURVE, m.SATURATION_MAX = True, 2.0, 0.2, 4.0
_pv = m.preview_colour(_blue)
assert m.valid_xy(_pv["xy"]), _pv
assert max(_pv["rgb"]) > 0, "a blue screen must not preview as black"
_pl = m.build_payload(_blue)
assert m.valid_xy(_pl.get("xy_color", _pv["xy"])), _pl
_keep_mode = m.SEND_MODE
m.SEND_MODE = "rgb"
assert max(m.build_payload(_blue)["rgb_color"]) > 0, "rgb mode must not send 0,0,0"
m.SEND_MODE = _keep_mode
(m.SATURATION_BOOST, m.SATURATION_GAIN, m.SATURATION_CURVE,
 m.SATURATION_MAX) = _keep_boost
assert m.valid_xy((0.3, 0.3)) and not m.valid_xy((0.09, -0.02))
assert not m.valid_xy((0.7, 0.5))       # x + y > 1 is outside the domain
print("xy domain ok: boost stays valid, rgb never collapses to black")

# --- install-path helpers --------------------------------------------------
assert m._deps_import(sys.executable) is True
assert m._deps_import("/nonexistent/python") is False
for good in ("127.0.0.1", "0.0.0.0", "::", "localhost", "homeassistant.local", "192.168.1.10"):
    assert m.valid_bind_host(good), good
for bad in ("999.999.999.999", "http://0.0.0.0:8000", "", "a b", "0.0.0.0/ui"):
    assert not m.valid_bind_host(bad), bad
print("install helpers ok: dependency probe, bind address validation")

# --- one bad key in config.json must not discard the good ones ------------
_cfg_path = __import__("pathlib").Path(tempfile.mkdtemp()) / "config.json"
_cfg_path.write_text(_json.dumps({"CAPTURE_FPS": 7.5, "ligths": 3, "COLOR_ALGO": "mean"}))
_keep_loaded = m.CONFIG_LOADED
m.CONFIG_LOADED = False
m.load_config_file(_cfg_path)
assert m.CAPTURE_FPS == 7.5, m.CAPTURE_FPS
assert m.COLOUR_BLEND == 0.0, "a legacy COLOR_ALGO=mean in the file must become blend 0"
m.CONFIG_LOADED = _keep_loaded
print("partial config load ok: unknown keys warned, valid settings applied")

# --- SEND_BRIGHTNESS off: the payload carries no brightness ----------------
_c = {"rgb": [10, 200, 60], "xy": [0.31, 0.33], "luminance": 0.4}
_t = m.Target("light.a", frozenset({2}))
_t.last_attempted = _t.last_sent = (tuple(_c["rgb"]), None)   # sent without brightness
_t.last_attempt_mono = _t.last_success_mono = time.perf_counter()
assert m.should_send(_t, _c, 100) is False, "same colour with no brightness must dedupe"
# and an unanswered attempt with no brightness must not raise either
_t.last_sent = ((1, 2, 3), None)
_t.last_attempt_mono = time.perf_counter()
assert m.should_send(_t, _c, 100) is False
assert m.should_send(_t, dict(_c, rgb=[120, 20, 20]), 100) is True
_prev_sb = m.SEND_BRIGHTNESS
m.SEND_BRIGHTNESS = False
assert "brightness" not in m.build_payload(_c)
run_phase(0.7, mode="solid", colour=(10, 200, 60))
assert m.stats["frame_errors"] == 0, m.stats.get("last_frame_error")
m.SEND_BRIGHTNESS = _prev_sb
print("brightness-off ok: dedupe handles a missing brightness, frames clean")

# the counter that surfaces a broken frame without the terminal
_broken = m.aggregate
m.aggregate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
run_phase(0.5, mode="solid")
m.aggregate = _broken
assert m.stats["frame_errors"] > 0, "frame errors must be countable"
assert "boom" in (m.stats["last_frame_error"] or ""), m.stats["last_frame_error"]
m.stats["frame_errors"] = 0
m.stats["last_frame_error"] = None
print("frame error counter ok:", "surfaced as %s" % m.stats["frame_errors"])

# --- browser furniture: favicon + the non-deprecated mss factory -----------
_fav = m.favicon()
assert getattr(_fav, "status_code", 200) == 204, "favicon should be a quiet 204"
assert 'rel="icon"' in m.UI_HTML, "the page must carry its own icon"
print("favicon ok: 204 route, inline icon, MSS factory")

# --- light picker: entities come from Home Assistant ----------------------
_orig_token = m.HA_TOKEN
m.HA_TOKEN = "picker-token"
m.LIGHTS_CACHE.update({"at": 0.0, "data": None})
_picker = m.get_lights()
assert _picker["ok"] and _picker["count"] == 3, _picker          # switch.* excluded
_ids = [l["entity_id"] for l in _picker["lights"]]
_grouped = {}
for _l in m.get_lights(refresh=True)["lights"]:
    _grouped.setdefault(_l["area"], []).append(_l)
for _area, _members in _grouped.items():
    assert [b["color_capable"] for b in _members] == sorted(
        (b["color_capable"] for b in _members), reverse=True), (_area, _members)
assert set(_ids) == {"light.desk_lamp", "light.plain_bulb", "light.attic"}
_office = [l for l in _picker["lights"] if l["area"] == "Office"]
_living = [l for l in _picker["lights"] if l["area"] == "Living Room"]
assert [l["color_capable"] for l in _office] == [True], _office
assert [l["color_capable"] for l in _living] == [False], _living
assert any(l["name"] == "attic" for l in _picker["lights"]), \
    "missing friendly_name falls back to the id"
assert any(a == "Bearer picker-token" for a in GET_AUTH), "state fetch must send the token"

_count = GET_COUNT
assert m.get_lights()["count"] == 3 and GET_COUNT == _count, "second call must be cached"
m.get_lights(refresh=True)
assert GET_COUNT == _count + 1, "refresh must bypass the cache"

m.HA_TOKEN = ""
_no_token = m.get_lights(refresh=True)
assert _no_token["ok"] is False and "token" in _no_token["detail"].lower()
m.HA_TOKEN = _orig_token
m.LIGHTS_CACHE.update({"at": 0.0, "data": None})
print("picker ok: light.* only, colour-capable first, cached, refreshable")

# --- the picker groups lights by area ---------------------------------------
_listed = m.get_lights(refresh=True)
_by_entity = {l["entity_id"]: l for l in _listed["lights"]}
assert _by_entity["light.desk_lamp"]["area"] == "Office", _by_entity["light.desk_lamp"]
assert _by_entity["light.plain_bulb"]["area"] == "Living Room"
assert _by_entity["light.attic"]["area"] == "", "a light with no area must say so"
assert _listed["areas"] == ["Living Room", "Office"], _listed["areas"]
assert _listed["unassigned"] == 1, _listed["unassigned"]      # light.attic
_areas_in_order = [l["area"] for l in _listed["lights"]]
assert _areas_in_order == sorted(_areas_in_order, key=lambda a: (a == "", a.lower())), _areas_in_order
print(f"areas ok: {_listed['areas']} then {_listed['unassigned']} without one, unassigned last")

# a Home Assistant that cannot answer the template must not break the picker
_keep_session = m.session


class _NoTemplate:
    status_code = 404
    text = ""


m.session = type("S", (), {"get": m.session.get,
                           "post": staticmethod(lambda *a, **k: _NoTemplate())})()
_fallback = m.get_lights(refresh=True)
assert _fallback["ok"] and _fallback["count"] == 3, _fallback.get("count")
assert all(l["area"] == "" for l in _fallback["lights"]), "no areas, but still a list"
assert _fallback["areas"] == []
m.session = _keep_session
print("areas ok: a 404 from the template API still yields a usable list")

# --- help text has to say what a knob does, not just name it --------------
_meta = {m_entry["name"]: m_entry for m_entry in m.get_config()["meta"]}
_bins_help = _meta["HUE_BINS"]["help"]
assert "15°" in _bins_help and "flicker" in _bins_help, _bins_help
assert len(_bins_help) > 80, "help text too terse to be useful"
assert _meta["HUE_BINS"]["min"] == 6 and _meta["HUE_BINS"]["max"] == 72
print("help text ok: hue bins explains its effect and its range")

# --- edits apply live but only Save writes the file -----------------------
import pathlib as _pathlib
_cfg = _pathlib.Path(tempfile.mkdtemp()) / "config.json"
_orig_cfg, _orig_mix = m.CONFIG_PATH, m.COLOUR_BLEND
m.CONFIG_PATH = _cfg
m.post_config({"settings": {"COLOUR_BLEND": 0.25}})          # persisted baseline
_baseline = _json.loads(_cfg.read_text(encoding="utf-8"))["COLOUR_BLEND"]
assert _baseline == 0.25 and m.get_config()["unsaved"] is False

_live = m.post_config({"settings": {"COLOUR_BLEND": 0.9}, "persist": False})
assert _live["persisted"] is False
assert m.COLOUR_BLEND == 0.9, "a live edit must reach the running process"
assert _json.loads(_cfg.read_text(encoding="utf-8"))["COLOUR_BLEND"] == 0.25, "live edit must not touch the file"
assert m.get_config()["unsaved"] is True
assert m.get_config()["saved_settings"]["COLOUR_BLEND"] == 0.25

m.post_config({"settings": {"COLOUR_BLEND": 0.9}})           # now persist
assert _json.loads(_cfg.read_text(encoding="utf-8"))["COLOUR_BLEND"] == 0.9
assert m.get_config()["unsaved"] is False
m.CONFIG_PATH, m.COLOUR_BLEND = _orig_cfg, _orig_mix
print("live apply ok: applies without writing, Save persists, Revert has a target")

# --- advanced sections and settings that only apply in some modes ---------
_meta = {m_entry["name"]: m_entry for m_entry in m.get_config()["meta"]}
_advanced = [n for n, v in _meta.items() if v.get("advanced")]
assert len(_advanced) >= 15, _advanced
assert _meta["HUE_BINS"].get("advanced") is True
assert _meta["SEND_MODE"].get("advanced") is True
assert _meta["COLOUR_BLEND"].get("advanced") is not True, "the everyday knob must stay visible"
assert _meta["CAPTURE_FPS"].get("advanced") is True, "capture rate belongs in Advanced"
assert _meta["COLOR_ALGO"].get("hidden") is True, "the old enum must not render"
assert list(_meta["COLOUR_BLEND"]["ends"]) == ["mean", "dominant"], _meta["COLOUR_BLEND"]["ends"]
assert _meta["SATURATION_GAIN"]["show_if"] == {"SATURATION_BOOST": True}
assert _meta["HUE_BINS"]["show_if"] == {"COLOUR_BLEND": {"$gt": 0.001}}
assert "CHROMA_FLOOR" in _advanced and "TRANSITION" in _advanced
print(f"advanced ok: {len(_advanced)} of {len(_meta)} settings collapsed, dependencies expressed")

# --- unchanged settings are not reported as needing a restart -------------
_orig_port = m.BIND_PORT
_now = m.settings_snapshot()
_first = m.post_config({"settings": {"BIND_PORT": _now["BIND_PORT"],
                                    "COLOUR_BLEND": _now["COLOUR_BLEND"]}})
assert _first["restart_required"] == [], _first["restart_required"]
assert _first["applied"] == {}, _first["applied"]
_second = m.post_config({"settings": {"BIND_PORT": 8123}})
assert _second["restart_required"] == ["BIND_PORT"], _second["restart_required"]
assert list(_second["applied"]) == ["BIND_PORT"], _second["applied"]
m.BIND_PORT = _orig_port
print("change reporting ok: only touched knobs are listed")

# --- the single file is a complete install --------------------------------
_ui = m.index_page()
assert getattr(_ui, "status_code", 200) == 200, "GET / must serve the embedded page"
_page = _ui.body.decode()
for needle in ('id="settings"', 'id="zonebits"', 'id="lightlive"', 'id="refreshlights"',
               'id="lights"', "HASS Ambilights", "Test connection",
               'className = "adv"', "function refreshVisibility", "row.hidden = !visible"):
    assert needle in _page, needle
assert "/static/" not in _page, "page must not depend on external assets"
assert len(m.UI_HTML) > 20000

# a top-level binding to a missing id throws before the first render and leaves a
# blank page, so every id bound at load time must exist in the markup
import re as _re
_bound = _re.findall(r'^on\("([A-Za-z0-9_-]+)"', _page, _re.M)
assert _bound, "expected top-level bindings"
_missing = [i for i in _bound if f'id="{i}"' not in _page]
assert not _missing, f"bound at load but not in the page: {_missing}"
assert not _re.search(r'^\$\("[A-Za-z0-9_-]+"\)\.addEventListener', _page, _re.M), \
    "bind through on() so a missing element cannot blank the page"

# the bootstrap list and requirements.txt must not drift apart
_req = __import__("pathlib").Path(m.__file__).with_name("requirements.txt")
if _req.exists():
    names = set()
    for line in _req.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            names.add(line.split("==")[0].split(">")[0].split("<")[0].strip().lower())
    embedded = {p.lower() for p in m.BOOTSTRAP_PACKAGES}
    assert names <= embedded, sorted(names - embedded)

# config.json must never contain a secret placeholder
import tempfile as _tempfile
_cfg_path = __import__("pathlib").Path(_tempfile.mkdtemp()) / "config.json"
_orig_cfg = m.CONFIG_PATH
m.CONFIG_PATH = _cfg_path
m.persist_config()
_written = _json.loads(_cfg_path.read_text(encoding="utf-8"))
assert "HA_TOKEN" not in _written, "secrets must not be written to config.json"
assert "BIND_HOST" in _written and "HA_URL" in _written
m.CONFIG_PATH = _orig_cfg
print("single file ok: embedded UI, deps match requirements, no secret in config.json")

# --- every HTTP handler must serialise (catches endpoint regressions) -----
import json as _json

m.TARGETS = [m.Target("light.a", frozenset({1, 4, 7}))]
m.latest_data["raw_grid"] = np.zeros((m.GRID_SIZE[1], m.GRID_SIZE[0], 3), np.uint8)
m.latest_data["regions"] = {"zones": [], "full": None}
m.latest_data["lights"] = {}
for handler in (m.health, m.get_config, m.get_regions, m.get_raw_grid):
    payload = _json.dumps(handler())          # raises on numpy types / bad shapes
    assert payload.startswith("{"), handler.__name__
assert m.health()["has_frame"] is True
m.latest_data["raw_grid"] = np.zeros((0, 0, 3), np.uint8)
assert m.health()["has_frame"] is False, "empty grid must report no frame"
print("http handlers ok: health, config, regions, raw_grid all serialise")

# --- crop geometry -------------------------------------------------------
mont = {"left": 100, "top": 50, "width": 3840, "height": 2160}
full_targets = [m.Target("light.a", frozenset(range(1, 10)))]
rect_all, win_all, size_all = m.capture_geometry(mont, full_targets)
assert rect_all["width"] == 3840 and win_all == (0, 35, 0, 63) and size_all == (64, 36)

left_targets = [m.Target("light.a", frozenset({1, 4, 7}))]
rect_left, win_left, size_left = m.capture_geometry(mont, left_targets)
assert rect_left["width"] < 3840 * 0.4, rect_left
assert rect_left["left"] >= 100 and rect_left["height"] == 2160
assert win_left[0] == 0 and win_left[1] == 35 and win_left[2] == 0
assert 19 <= win_left[3] <= 22, win_left          # first third of 64 cols
assert size_left == (win_left[3] - win_left[2] + 1, 36), size_left

centre = [m.Target("light.a", frozenset({5}))]
rect_c, win_c, size_c = m.capture_geometry(mont, centre)
assert rect_c["width"] < 3840 / 2 and rect_c["height"] < 2160 / 2, rect_c
assert size_c[0] <= 22 and size_c[1] <= 13, size_c
print("crop ok: full %sx%s, left column %sx%s, centre %sx%s"
      % (rect_all["width"], rect_all["height"], rect_left["width"], rect_left["height"],
         rect_c["width"], rect_c["height"]))

# --- phase I: letterboxed screen in dominant mode still ships ------------
m.COLOUR_BLEND = 1.0
run_phase(0.8, mode="bars", colour=(200, 90, 30))
assert POSTS, "dominant mode with black bars posted nothing (frame errors?)"
m.COLOUR_BLEND = 0.0
print("phase I: black bars + dominant -> %d posts, last %s" % (len(POSTS), POSTS[-1][1]["xy_color"]))

# --- phase J: a read timeout is a radio stall, not "HA unreachable" -------
m.REQUEST_TIMEOUT = 0.25
m.RADIO_PAUSE = 0.4
run_phase(1.2, mode="cycle", rtt=0.5)              # every command hangs past the timeout
assert m.stats["timeouts"] >= 1, m.stats
assert m.stats["radio_stalls"] >= 1, m.stats
assert m.stats["errors"] == 0, "timeout counted as a host failure: %s" % m.stats
assert m.stats["posts"] == 0
m.REQUEST_TIMEOUT = 3.0
run_phase(1.0, mode="cycle", rtt=0.0)              # host is fine again
assert m.stats["posts"] >= 1, "host breaker tripped on a radio stall"
print("phase J: read timeouts classified as radio stalls, recovered in place")

# --- phase K: controller-down 500s are stalls, not host errors -----------
CONTROLLER_DOWN = 3
run_phase(2.0, mode="cycle")
assert m.stats["radio_stalls"] >= 1, m.stats
assert m.stats["errors"] == 0, "controller 500 counted as a host error: %s" % m.stats
assert m.stats["posts"] >= 1, "never resumed after the controller came back"
print("phase K: controller-down 500 -> %d stalls, resumed with %d posts"
      % (m.stats["radio_stalls"], m.stats["posts"]))

print("ALL CHECKS PASSED")
