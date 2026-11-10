#!/usr/bin/env python3
"""Live probe: push a synthetic frame through the real sender against real lights.

Reads HA_URL / HA_TOKEN from the environment, runs for N seconds, prints the
achieved post rate, cycle time and per-light counters, then restores each light to
the state it had when the probe started.

    HA_URL=http://homeassistant.local:8123 HA_TOKEN=... python tools/live_probe.py
    LIGHTS="light.a=1,4,7; light.b=all" SECONDS=8 python tools/live_probe.py
"""
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import screen_to_lights as sync  # noqa: E402


def main() -> int:
    if not sync.HA_TOKEN:
        print("HA_TOKEN is not set", file=sys.stderr)
        return 2
    sync.load_config_file()
    sync.TARGETS = sync.parse_light_map(os.getenv("LIGHTS") or sync.LIGHT_MAP)
    if not sync.check_ha():
        return 1

    class Cycler:
        monitors = [{}, {"left": 0, "top": 0, "width": 640, "height": 360}]
        n = 0

        def grab(self, monitor):
            import colorsys

            Cycler.n += 1
            r, g, b = colorsys.hsv_to_rgb((Cycler.n * 0.05) % 1.0, 0.6, 0.85)
            left, top = monitor.get("left", 0), monitor.get("top", 0)
            w, h = monitor["width"], monitor["height"]
            sub = np.full((h, w, 3), (int(r * 255), int(g * 255), int(b * 255)), np.uint8)
            bgra = np.dstack([sub[..., 2], sub[..., 1], sub[..., 0],
                              np.full((h, w), 255, np.uint8)])

            class Shot:
                pass

            shot = Shot()
            shot.size = (w, h)
            shot.raw = bytearray(bgra.tobytes())
            return shot

        def close(self):
            pass

    sync.mss = lambda: Cycler()

    saved = {}
    for target in sync.TARGETS:
        try:
            saved[target.entity_id] = sync.session.get(
                f"{sync.HA_URL}/api/states/{target.entity_id}", timeout=5).json()["attributes"]
        except Exception:                       # noqa: BLE001 - probe is best effort
            saved[target.entity_id] = {}

    seconds = float(os.getenv("SECONDS", "8"))
    sync.stop_event.clear()
    worker = threading.Thread(target=sync.capture_loop, daemon=True)
    poster = threading.Thread(target=sync.sender_loop, daemon=True)
    worker.start()
    poster.start()
    time.sleep(seconds)
    sync.stop_event.set()
    worker.join(timeout=3)
    poster.join(timeout=3)

    stats = sync.stats
    print(f"posts {stats['posts']}  busy {stats['busy']}  stalls {stats['radio_stalls']}  "
          f"timeouts {stats['timeouts']}  errors {stats['errors']}  dropped {stats['dropped']}")
    print(f"cycle {stats['cycle_ms'] or 0:.0f} ms for {len(sync.TARGETS)} light(s) | "
          f"per-light interval {stats['interval_ms'] or 0:.0f} ms | "
          f"transition {sync.current_transition():.2f} s | prep {stats['prep_ms'] or 0:.2f} ms")
    for target in sync.TARGETS:
        print(f"   {target.entity_id:<40} zones {target.zone_label:<18} "
              f"posts={target.posts} busy={target.busy} errors={target.errors}")

    for entity, attrs in saved.items():
        restore = {"entity_id": entity}
        if attrs.get("color_temp_kelvin"):
            restore["color_temp_kelvin"] = attrs["color_temp_kelvin"]
        elif attrs.get("xy_color"):
            restore["xy_color"] = attrs["xy_color"]
        if attrs.get("brightness"):
            restore["brightness"] = attrs["brightness"]
        sync.restore_light(restore)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
