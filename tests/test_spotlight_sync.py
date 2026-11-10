"""Offline verification for screen_to_lights.py: fake screen + mock HA.

Run from anywhere:  python tests/test_spotlight_sync.py
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

    def do_POST(self):
        global BUSY_LEFT, FAIL_POSTS
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

os.environ["HA_URL"] = f"http://127.0.0.1:{port}"
os.environ.setdefault("HA_TOKEN", "test-token")
os.environ["CAPTURE_FPS"] = "30"
os.environ["MONITOR_INDEX"] = "1"

from PIL import Image

import screen_to_lights as m

m.mss = lambda: FakeMSS()
m.HA_URL = f"http://127.0.0.1:{port}"
m.session.headers.update({"Authorization": "Bearer test-token"})

def brightness_of(rgb, *extra):
    """Brightness for a plain sRGB colour, honouring the current BRIGHTNESS_FROM."""
    lin = m._srgb_to_linear(np.asarray(rgb, dtype=np.float32) / 255.0)
    return m.brightness_for_colour({"rgb": list(rgb),
                                    "luminance": float(m._linear_to_xyz(lin)[1])})



# --- shipped defaults -----------------------------------------------------
assert m.COLOR_ALGO == "dominant", m.COLOR_ALGO
assert m.DOMINANT_MIX == 0.5, m.DOMINANT_MIX
assert m.SATURATION_BOOST is False, m.SATURATION_BOOST
assert m.DOMINANT_LUMINANCE == "cluster"
print("defaults ok: %s / mix %.1f / boost %s" % (m.COLOR_ALGO, m.DOMINANT_MIX, m.SATURATION_BOOST))

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
assert lum_red < 80 and lum_blue < 40, (lum_red, lum_blue)

m.BRIGHTNESS_FROM = "lstar"
ls_white = m.brightness_for_colour(white_c)
ls_red, ls_blue = m.brightness_for_colour(red_c), m.brightness_for_colour(blue_c)
assert ls_red > lum_red * 1.5 and ls_blue > lum_blue * 2, (ls_red, ls_blue)
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
tg.last_success_mono = time.perf_counter() - (m.RESEND_AFTER + 1)
assert m.should_send(tg, colour, 100) is True
tg.last_success_mono = time.perf_counter()              # too soon: wait, don't retry
assert m.should_send(tg, colour, 100) is False
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
m.COLOR_ALGO = "mean"
mean_r = m.aggregate(m._srgb_to_linear(page.astype(np.float32) / 255.0))
m.COLOR_ALGO = "dominant"
dom_r = m.aggregate(m._srgb_to_linear(page.astype(np.float32) / 255.0))
assert "dominant" not in mean_r and "dominant" in dom_r
assert m._neo(dom_r["dominant"], m.D65_XY) > 3 * m._neo(mean_r["xy"], m.D65_XY)
gray = np.full((9, 16, 3), 128, dtype=np.uint8)
assert m.aggregate(m._srgb_to_linear(gray / 255.0))["dominant"] == m.aggregate(m._srgb_to_linear(gray / 255.0))["xy"]
m.DOMINANT_MIX = 0.0
assert m.aggregate(m._srgb_to_linear(page.astype(np.float32) / 255.0))["dominant"] == mean_r["xy"]
m.DOMINANT_MIX = 1.0
m.COLOR_ALGO = "mean"
print("mean vs dominant ok: %.4f vs %.4f" % (m._neo(mean_r["xy"], m.D65_XY),
                                             m._neo(dom_r["dominant"], m.D65_XY)))

# --- dominant mode: dedupe/brightness must follow the colour we SEND ------
dark = np.full((36, 64, 3), 30, dtype=np.uint8)
dark[10:26, 18:48] = (255, 140, 40)             # vivid photo panel on a dark page
dark_lin = m._srgb_to_linear(dark.astype(np.float32) / 255.0)
m.COLOR_ALGO = "dominant"
m.DOMINANT_MIX = 1.0
m.DOMINANT_LUMINANCE = "region"
d_region = m.aggregate(dark_lin)
m.DOMINANT_LUMINANCE = "cluster"
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
m.COLOR_ALGO = "mean"
m.DOMINANT_LUMINANCE = "cluster"
m.DOMINANT_MIX = 0.5
print("dominant ok: brightness region %d -> cluster %d (Y %.3f -> %.3f)"
      % (b_region, b_cluster, d_cluster["luminance"], d_cluster["dominant_luminance"]))

# --- black cells (letterbox bars, black windows) must not break dominant --
bars = np.zeros((12, 22, 3), dtype=np.uint8)          # 264 cells, 132 black
bars[3:9, :] = (255, 120, 30)
bars_lin = m._srgb_to_linear(bars.astype(np.float32) / 255.0)
m.COLOR_ALGO = "dominant"
m.DOMINANT_LUMINANCE = "cluster"
m.DOMINANT_MIX = 1.0
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
m.COLOR_ALGO = "mean"
m.DOMINANT_MIX = 0.5
print("black cells ok: dominant %s, cluster Y %.3f vs region %.3f"
      % (out["dominant"], out["dominant_luminance"], out["luminance"]))

# --- saturation can be switched off entirely ----------------------------
assert m.DOMINANT_MIX == 0.5, "default mix should be 0.5, got %s" % m.DOMINANT_MIX
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
        t.last_success_mono = 0.0
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
run_phase(0.8, mode="solid", colour=(200, 60, 40))
assert len(POSTS) == 1, "static screen should post once, got %d" % len(POSTS)
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
m.COLOR_ALGO = "mean"
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
run_phase(1.0, mode="zones", rtt=0.12, targets=targets)
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
assert m.SECRETS_PATH.exists() and m.SECRETS_PATH.stat().st_mode & 0o777 == 0o600, oct(
    m.SECRETS_PATH.stat().st_mode)
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
assert m.BIND_PORT == 65535, m.BIND_PORT            # clamped, not crashed
m.BIND_HOST, m.BIND_PORT = _orig_bind
m.BIND_HOST, m.BIND_PORT = _orig_bind
print("bind ok: loopback default, URL rejected, port clamped, restart-tagged")

# --- light picker: entities come from Home Assistant ----------------------
_orig_token = m.HA_TOKEN
m.HA_TOKEN = "picker-token"
m.LIGHTS_CACHE.update({"at": 0.0, "data": None})
_picker = m.get_lights()
assert _picker["ok"] and _picker["count"] == 3, _picker          # switch.* excluded
_ids = [l["entity_id"] for l in _picker["lights"]]
assert _ids[0] == "light.desk_lamp", _ids                        # colour-capable first
assert set(_ids) == {"light.desk_lamp", "light.plain_bulb", "light.attic"}
assert _picker["lights"][0]["color_capable"] is True
assert _picker["lights"][1]["color_capable"] is False
assert _picker["lights"][1]["name"] == "attic", "missing friendly_name falls back to the id"
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

# --- unchanged settings are not reported as needing a restart -------------
_orig_port = m.BIND_PORT
_now = m.settings_snapshot()
_first = m.post_config({"settings": {"BIND_PORT": _now["BIND_PORT"],
                                    "COLOR_ALGO": _now["COLOR_ALGO"]}})
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
               'id="lights"', "Screen → Lights", "Test connection"):
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
    for line in _req.read_text().splitlines():
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
_written = _json.loads(_cfg_path.read_text())
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
m.COLOR_ALGO = "dominant"
run_phase(0.8, mode="bars", colour=(200, 90, 30))
assert POSTS, "dominant mode with black bars posted nothing (frame errors?)"
m.COLOR_ALGO = "mean"
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
