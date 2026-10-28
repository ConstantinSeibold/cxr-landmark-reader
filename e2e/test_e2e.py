"""End-to-end tests in headless Chromium against the demo study (PAXRay++ images only).

run: python -m pytest e2e -q   (needs `pip install playwright` and `playwright install chromium`)
Screenshots of each step go to e2e/artifacts/.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")

from e2e.helpers import ROOT, canvas_point, copy_demo, get, post, server, wait_card  # noqa: E402

ART = Path(__file__).resolve().parent / "artifacts"
ART.mkdir(exist_ok=True)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def study(tmp_path):
    return copy_demo(tmp_path / "demo")


def new_page(browser):
    # the app's CSP forbids eval, which Playwright's wait/evaluate helpers rely on; the header
    # itself is covered by tests/test_server.py
    return browser.new_context(bypass_csp=True, viewport={"width": 1400, "height": 980}).new_page()


def open_reader(browser, url, rid="r1"):
    page = new_page(browser)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(url)
    page.get_by_role("button", name=rid, exact=True).click()
    wait_card(page)
    return page, errors


def statuses(page):
    return page.evaluate("S.pts.map(p => p.status)")


def test_read_edit_submit_resume(browser, study):
    with server(study) as url:
        page, errors = open_reader(browser, url)
        assert page.locator("#points li").count() == page.evaluate("S.pts.length") > 0
        assert page.evaluate("document.querySelector('#ref').naturalWidth") > 0
        page.screenshot(path=ART / "01_card.png")

        # drag the first point -> moved
        x, y = canvas_point(page, 0)
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 25, y + 10, steps=5)
        page.mouse.up()
        assert statuses(page)[0] == "moved"
        assert page.locator("#points li").first.locator(".st").inner_text() == "moved"

        # second point: not visible; unsure on the first
        page.evaluate("S.sel = 1; renderList(); draw();")
        page.keyboard.press("n")
        assert statuses(page)[1] == "not_visible"
        page.evaluate("S.sel = 0; renderList();")
        page.keyboard.press("u")
        assert page.evaluate("S.pts[0].unsure")

        # masks and crop toggles
        page.keyboard.press("m")
        page.wait_for_function("S.outlines !== null")
        assert page.locator("#btnMasks").inner_text() == "Outlines: on"
        assert page.locator("#maskLegend .chip").count() == page.evaluate("S.outlines.length") > 0
        page.screenshot(path=ART / "02_masks.png")
        full = page.evaluate("S.full")
        page.keyboard.press("c")
        assert page.evaluate("S.full") != full

        # submit -> next card; the record is stored
        page.keyboard.press("Enter")
        wait_card(page, 1)
        rec = get(f"{url}/api/r/r1/card/0")
        assert rec["done"] and rec["points"][0]["status"] == "moved" and rec["points"][1]["status"] == "not_visible"

        # reload: the session resumes at the first open card, back shows the stored state
        page.reload()
        page.get_by_role("button", name="r1", exact=True).click()
        wait_card(page, 1)
        page.keyboard.press("Backspace")
        wait_card(page, 0)
        assert statuses(page)[:2] == ["moved", "not_visible"]
        assert not errors, errors


def test_drag_accuracy_and_grab_offset(browser, study):
    with server(study) as url:
        page, errors = open_reader(browser, url)
        i = page.evaluate("S.pts.findIndex(p => p.xy)")
        before = page.evaluate(f"S.pts[{i}].xy.slice()")
        x, y = canvas_point(page, i)
        # off-centre grab, then a sub-threshold wiggle: no change, still ok
        page.mouse.move(x + 6, y + 5)
        page.mouse.down()
        page.mouse.move(x + 7, y + 5)
        page.mouse.up()
        assert page.evaluate(f"S.pts[{i}].xy") == before and statuses(page)[i] == "ok"
        # real drag from an off-centre grab: the point moves by exactly the cursor delta
        page.mouse.move(x + 6, y + 5)
        page.mouse.down()
        page.mouse.move(x + 36, y - 15, steps=8)
        page.mouse.up()
        sx, sy = page.evaluate("[S.view.sx, S.view.sy]")
        after = page.evaluate(f"S.pts[{i}].xy")
        assert abs(after[0] - (before[0] + 30 / sx)) < 0.05 and abs(after[1] - (before[1] - 20 / sy)) < 0.05
        assert statuses(page)[i] == "moved"
        assert not errors, errors


def test_undo(browser, study):
    with server(study) as url:
        page, errors = open_reader(browser, url)
        i = page.evaluate("S.pts.findIndex(p => p.xy)")
        before = page.evaluate(f"S.pts[{i}].xy.slice()")
        x, y = canvas_point(page, i)
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 30, y + 30, steps=6)
        page.mouse.up()
        page.evaluate(f"S.sel = {i}; renderList();")
        page.keyboard.press("n")
        page.keyboard.press("u")
        assert statuses(page)[i] == "not_visible" and page.evaluate(f"S.pts[{i}].unsure")
        page.keyboard.press("Control+z")                     # undo unsure
        assert not page.evaluate(f"S.pts[{i}].unsure")
        page.locator("#btnUndo").click()                     # undo not visible
        assert statuses(page)[i] == "moved"
        page.keyboard.press("Control+z")                     # undo the drag
        assert statuses(page)[i] == "ok" and page.evaluate(f"S.pts[{i}].xy") == before
        page.keyboard.press("Control+z")                     # nothing left: no change, no error
        assert statuses(page)[i] == "ok"
        # a click on a point without moving it adds nothing to undo
        page.mouse.click(x, y)
        assert page.evaluate("S.hist.length") == 0
        assert not errors, errors


def test_keyboard_nudge_redo_flag_space(browser, study):
    with server(study) as url:
        page, errors = open_reader(browser, url)
        i = page.evaluate("S.pts.findIndex(p => p.xy)")
        page.evaluate(f"S.sel = {i}; renderList();")
        before = page.evaluate(f"S.pts[{i}].xy.slice()")
        sx, sy = page.evaluate("[S.view.sx, S.view.sy]")
        for _ in range(3):
            page.keyboard.press("ArrowRight")
        page.keyboard.press("Shift+ArrowDown")
        after = page.evaluate(f"S.pts[{i}].xy")
        assert abs(after[0] - (before[0] + 3 / sx)) < 1e-6 and abs(after[1] - (before[1] + 5 / sy)) < 1e-6
        assert statuses(page)[i] == "moved"
        assert page.evaluate("S.hist.length") == 1                 # consecutive nudges = one undo step
        page.keyboard.press("Control+z")
        assert page.evaluate(f"S.pts[{i}].xy") == before and statuses(page)[i] == "ok"
        page.keyboard.press("Control+Shift+z")                      # redo
        assert page.evaluate(f"S.pts[{i}].xy") == after
        page.keyboard.press("Control+z")
        page.keyboard.press("Control+y")                            # redo, alternative binding
        assert page.evaluate(f"S.pts[{i}].xy") == after
        page.keyboard.press("f")                                    # flag: all not visible
        assert page.locator("#flag").is_checked() and set(statuses(page)) == {"not_visible"}
        page.keyboard.press("Control+z")
        assert not page.locator("#flag").is_checked() and statuses(page)[i] == "moved"
        # Space jumps to the next unresolved point
        page.evaluate("S.pts[1].status = 'missing'; S.sel = 0; renderList();")
        page.keyboard.press(" ")
        assert page.evaluate("S.sel") == 1
        assert not errors, errors


def test_missing_point_and_scratch_placement(browser, study):
    with server(study) as url:
        total = get(f"{url}/api/r/r1/session")["total"]
        cards = [get(f"{url}/api/r/r1/card/{i}") for i in range(total)]
        scratch = next(i for i, c in enumerate(cards) if c["mode"] == "scratch" and len(c["points"]) >= 3)
        page, errors = open_reader(browser, url)
        page.evaluate(f"load({scratch})")
        wait_card(page, scratch)
        assert set(statuses(page)) == {"missing"}
        assert page.locator("#btnSubmit").is_disabled()
        assert page.locator("#banner").inner_text().startswith("Place:")
        box = page.locator("#view").bounding_box()
        n = page.evaluate("S.pts.length")
        for k in range(n):                      # guided sequence: each click places the next point
            page.mouse.click(box["x"] + 40 + 12 * k, box["y"] + 60 + 9 * k)
        assert set(statuses(page)) == {"added"}
        assert page.locator("#btnSubmit").is_enabled()
        page.screenshot(path=ART / "03_scratch.png")
        page.keyboard.press("Enter")
        wait_card(page, scratch + 1)
        assert all(p["status"] == "added" for p in get(f"{url}/api/r/r1/card/{scratch}")["points"])
        assert not errors, errors


def test_series_card_and_reference_toggle(browser, study):
    with server(study) as url:
        total = get(f"{url}/api/r/r1/session")["total"]
        series = next(i for i in range(total) if get(f"{url}/api/r/r1/card/{i}")["gid"] == "G19")
        page, errors = open_reader(browser, url)
        page.evaluate(f"load({series})")
        wait_card(page, series)
        assert page.locator("#refSeries").is_visible()
        page.keyboard.press("2")
        page.wait_for_function("document.querySelector('#ref').src.includes('_series')")
        page.wait_for_function("document.querySelector('#ref').complete && document.querySelector('#ref').naturalWidth > 0")
        page.screenshot(path=ART / "04_series.png")
        assert not errors, errors


def test_second_reader_is_blinded(browser, study):
    with server(study) as url:
        page, _ = open_reader(browser, url, "r1")
        page.keyboard.press("Enter")
        wait_card(page, 1)
        page2, errors = open_reader(browser, url, "r2")
        assert page2.evaluate("S.pos") == 0
        assert get(f"{url}/api/r/r2/session")["done"] == 0
        assert not page2.evaluate("S.card.done")
        assert not errors, errors


def test_adjudication_flow(browser, study):
    # two scripted readers, one systematic disagreement, then adjudicate in the browser
    with server(study) as url:
        total = get(f"{url}/api/r/r1/session")["total"]
        for rid, shift in (("r1", 0.0), ("r2", 40.0)):
            for pos in range(total):
                c = get(f"{url}/api/r/{rid}/card/{pos}")
                pts = []
                for j, p in enumerate(c["points"]):
                    xy = p["xy"] or [200.0 + j, 200.0 + j]
                    d = shift if pos == 0 else 0.0
                    pts.append({"key": p["key"], "status": "added" if p["xy"] is None else "ok", "unsure": False,
                                "xy": [min(511.0, xy[0] + d), xy[1]]})
                post(f"{url}/api/r/{rid}/card/{pos}/submit", {"points": pts})
    cats = Path(os.environ.get("PAXRAY_KP_JSON", "missing"))
    if not cats.exists():
        pytest.skip("set PAXRAY_KP_JSON to the PAXRay++ keypoint json (categories for the gold COCO)")
    out = subprocess.run([sys.executable, str(ROOT / "aggregate.py"), "--data", str(study), "--categories", str(cats)],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    with server(study, mode="adjudicate") as url:
        s = get(f"{url}/api/adj/session")
        assert s["total"] >= 1
        page = new_page(browser)
        page.goto(url)
        page.get_by_role("button", name="adjudicator").click()
        wait_card(page, 0)
        for li in page.locator("#points li").all():
            li.get_by_role("button", name="A", exact=True).click()
        page.screenshot(path=ART / "05_adjudication.png")
        page.keyboard.press("Enter")
        page.wait_for_function("S.pos === 1 || document.querySelector('#banner').textContent.includes('done')")
        assert get(f"{url}/api/adj/session")["done"] == 1
