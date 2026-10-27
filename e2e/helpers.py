"""Start the app on a copy of the demo study (PAXRay++ only) and talk to it."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo_data"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def copy_demo(dst: Path) -> Path:
    if not (DEMO / "cards.json").exists():
        raise RuntimeError("demo_data missing: run build/build_demo.py first")
    shutil.copytree(DEMO, dst, ignore=shutil.ignore_patterns("readers", "adjudication", "out"))
    return dst


@contextmanager
def server(data: Path, mode: str = "read", readers: str = "r1,r2"):
    port = free_port()
    p = subprocess.Popen([sys.executable, "-m", "goldkp.server", "--data", str(data), "--images", str(data / "images"),
                          "--readers", readers, "--mode", mode, "--port", str(port)], cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/api/config", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError(p.stdout.read().decode() if p.stdout else "server did not start")
        yield url
    finally:
        p.terminate()
        p.wait(timeout=10)


def get(url: str):
    return json.load(urllib.request.urlopen(url))


def post(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))


def canvas_point(page, i: int) -> tuple[float, float]:
    """Page coordinates of point i of the current card."""
    return page.evaluate("""(i) => {
        const r = document.querySelector('#view').getBoundingClientRect();
        const cv = document.querySelector('#view');
        const [x, y] = toCanvas(...S.pts[i].xy);
        return [r.left + x * r.width / cv.width, r.top + y * r.height / cv.height];
    }""", i)


def wait_card(page, pos: int | None = None):
    page.wait_for_function("(p) => S.card && (p === null || S.pos === p) && S.img", arg=pos)
