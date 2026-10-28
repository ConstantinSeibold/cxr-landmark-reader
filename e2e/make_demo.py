"""Record docs/screenshots/*.png and docs/demo.gif from a scripted session on the demo study
(PAXRay++ images only -- never run this on real study data).

usage: python -m e2e.make_demo --gt paxraypp/annotations/test_kp_v1.json
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

from e2e.helpers import ROOT, canvas_point, copy_demo, get, post, server, wait_card

DOCS = ROOT / "docs"
W_GIF = 1000


class Recorder:
    def __init__(self, page):
        self.page, self.frames, self.mouse = page, [], None

    def shot(self, ms=900, name=None):
        img = Image.open(io.BytesIO(self.page.screenshot())).convert("RGB")
        if self.mouse:                      # screenshots carry no cursor; draw one
            x, y = self.mouse
            d = ImageDraw.Draw(img)
            d.polygon([(x, y), (x + 13, y + 13), (x + 6, y + 13), (x + 3, y + 20)], fill="white", outline="black")
        if name:
            img.save(DOCS / "screenshots" / f"{name}.png")
        self.frames.append((img, ms))

    def move(self, x, y, steps=6, ms=120):
        x0, y0 = self.mouse or (x - 80, y - 60)
        for k in range(1, steps + 1):
            self.mouse = (x0 + (x - x0) * k / steps, y0 + (y - y0) * k / steps)
            self.page.mouse.move(*self.mouse)
            if k % 2 == 0:
                self.shot(ms)

    def save_gif(self, path: Path):
        frames, durs = [], []
        for img, ms in self.frames:
            f = img.resize((W_GIF, int(img.height * W_GIF / img.width)), Image.LANCZOS)
            frames.append(f.convert("P", palette=Image.ADAPTIVE, colors=128))
            durs.append(ms)
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=durs, loop=0, optimize=True)


def gt_points(gt_json: Path, source: str) -> dict:
    d = json.load(open(gt_json))
    im = next(i for i in d["images"] if i["file_name"] == source)
    cats = {c["id"]: c for c in d["categories"]}
    out = {}
    for a in d["annotations"]:
        if a["image_id"] == im["id"]:
            c = cats[a["category_id"]]
            for j, kp in enumerate(c["keypoints"]):
                x, y, v = a["keypoints"][3 * j: 3 * j + 3]
                if v > 0:
                    out[f"{c['name']}|{kp}"] = (x, y)
    return out


def to_page(page, xy):
    return page.evaluate("""(xy) => {
        const r = document.querySelector('#view').getBoundingClientRect(), cv = document.querySelector('#view');
        const [x, y] = toCanvas(xy[0], xy[1]);
        return [r.left + x * r.width / cv.width, r.top + y * r.height / cv.height];
    }""", list(xy))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", type=Path, required=True)
    args = ap.parse_args()
    (DOCS / "screenshots").mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp()) / "demo"
    data = copy_demo(tmp)
    cj = json.load(open(data / "cards.json"))
    with server(data) as url, sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context(bypass_csp=True, viewport={"width": 1400, "height": 900}).new_page()
        rec = Recorder(page)
        page.goto(url)
        rec.shot(1400, "00_pick")
        page.get_by_role("button", name="r1", exact=True).click()
        wait_card(page)
        rec.shot(1600, "01_card")

        # correct a pre-filled point: drag it to the ground-truth position
        truth = gt_points(args.gt, cj["images"][page.evaluate("S.card.img")]["source"])
        i = 0
        key = page.evaluate("S.pts[0].key")
        x, y = canvas_point(page, i)
        rec.move(x, y)
        page.mouse.down()
        tx, ty = to_page(page, truth.get(key, (page.evaluate("S.pts[0].xy[0]") + 6, page.evaluate("S.pts[0].xy[1]"))))
        tx, ty = tx + 14, ty + 6                              # exaggerate a little so the move is visible
        rec.move(tx, ty, steps=6, ms=100)
        page.mouse.up()
        rec.shot(1200, "02_moved")
        page.keyboard.press("m")
        page.wait_for_function("S.outlines !== null")
        rec.shot(1600, "03_masks")
        page.keyboard.press("m")
        page.keyboard.press("Enter")
        wait_card(page, 1)
        rec.shot(1200)

        # a vertebra series card with the all-levels reference
        total = get(f"{url}/api/r/r1/session")["total"]
        series = next(k for k in range(total) if get(f"{url}/api/r/r1/card/{k}")["gid"] == "G19"
                      and get(f"{url}/api/r/r1/card/{k}")["batch"] == "T5-T8")
        page.evaluate(f"load({series})")
        wait_card(page, series)
        rec.shot(1400)
        page.keyboard.press("2")
        page.wait_for_function("document.querySelector('#ref').src.includes('_series') && document.querySelector('#ref').complete")
        rec.shot(2000, "04_series_reference")

        # a card without pre-fill: place the points in the guided order
        scratch = next(k for k in range(total) if get(f"{url}/api/r/r1/card/{k}")["mode"] == "scratch"
                       and get(f"{url}/api/r/r1/card/{k}")["gid"] == "G02")
        page.evaluate(f"load({scratch})")
        wait_card(page, scratch)
        rec.shot(1600, "05_scratch_start")
        truth = gt_points(args.gt, cj["images"][page.evaluate("S.card.img")]["source"])
        for k in range(page.evaluate("S.pts.length")):
            key = page.evaluate(f"S.pts[{k}].key")
            if key not in truth:
                page.keyboard.press("n")
                rec.shot(500)
                continue
            px, py = to_page(page, truth[key])
            rec.move(px, py, steps=4, ms=90)
            page.mouse.click(px, py)
            rec.shot(450)
        rec.shot(1600, "06_scratch_done")
        page.keyboard.press("?")
        rec.shot(1800, "07_help")
        page.keyboard.press("Escape")
        b.close()
    rec.save_gif(DOCS / "demo.gif")

    # adjudication screenshot: two scripted readers with one disagreement
    tmp2 = Path(tempfile.mkdtemp()) / "demo"
    data2 = copy_demo(tmp2)
    with server(data2) as url:
        total = get(f"{url}/api/r/r1/session")["total"]
        for rid, shift in (("r1", 0.0), ("r2", 30.0)):   # one disagreement: reader 2's first card
            for pos in range(total):
                c = get(f"{url}/api/r/{rid}/card/{pos}")
                pts = [{"key": q["key"], "status": "added" if q["xy"] is None else "ok", "unsure": False,
                        "xy": [min(511.0, (q["xy"] or [200.0 + j, 200.0])[0] + (shift if pos == 0 else 0)),
                               (q["xy"] or [200.0, 200.0 + j])[1]]} for j, q in enumerate(c["points"])]
                post(f"{url}/api/r/{rid}/card/{pos}/submit", {"points": pts})
    subprocess.run([sys.executable, str(ROOT / "aggregate.py"), "--data", str(data2), "--categories", str(args.gt)],
                   check=True, capture_output=True, cwd=ROOT)
    with server(data2, mode="adjudicate") as url, sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context(bypass_csp=True, viewport={"width": 1400, "height": 900}).new_page()
        page.goto(url)
        page.get_by_role("button", name="adjudicator").click()
        wait_card(page, 0)
        page.locator("#points li").first.get_by_role("button", name="mean", exact=True).click()
        page.screenshot(path=DOCS / "screenshots" / "08_adjudication.png")
        b.close()
    print("wrote", sorted(p.name for p in (DOCS / "screenshots").iterdir()), "and docs/demo.gif",
          f"({(DOCS / 'demo.gif').stat().st_size / 1e6:.1f} MB)")
    print("image sources:", sorted({im['source'] for im in cj['images']}))


if __name__ == "__main__":
    main()
