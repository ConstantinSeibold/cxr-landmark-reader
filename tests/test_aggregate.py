import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

import aggregate as A
from tests.conftest import TRUTH, make_study

ROOT = Path(__file__).resolve().parents[1]


def read_all(c, rid, sigma, s_of, rng, outlier_card=None):
    """Scripted reader: truth + isotropic noise sigma * s per coordinate."""
    total = c.get(f"/api/r/{rid}/session").json()["total"]
    for pos in range(total):
        card = c.get(f"/api/r/{rid}/card/{pos}").json()
        pts = []
        for p in card["points"]:
            cls, kp = p["key"].split("|", 1)
            x, y = TRUTH[cls][kp]
            e = rng.normal(0, sigma * s_of[cls], 2)
            if outlier_card == card["card_id"] and rid == "r2":
                e = e + 60
            pts.append({"key": p["key"], "status": "added" if p["xy"] is None else "moved",
                        "unsure": False, "xy": [float(x + e[0]), float(y + e[1])]})
        r = c.post(f"/api/r/{rid}/card/{pos}/submit", json={"points": pts, "active_ms": 500})
        assert r.status_code == 200, r.text


def test_kappa_pabak():
    a = np.array([1, 1, 1, 1, 0, 0, 1, 1])
    k, po, pabak = A.kappa_pabak(a, a)
    assert k == 1 and po == 1 and pabak == 1
    k, po, pabak = A.kappa_pabak(np.array([1, 1, 0, 0]), np.array([1, 0, 1, 0]))
    assert abs(k) < 1e-9 and po == 0.5 and pabak == 0


def test_sigma_recovery_gold_and_queue(tmp_path, client_factory):
    data, imgs = make_study(tmp_path, n_frontal=80, n_lateral=0, scratch_last=False)
    c = client_factory(data, imgs)
    s_of = {cls: math.sqrt(json.load(open(next((data / "prefill/test_v").glob("img0000.json"))))["masks"][cls]["area"])
            for cls in ("heart", "vertebrae T3", "vertebrae T7")}
    rng = np.random.default_rng(0)
    card0 = json.load(open(data / "cards.json"))["cards"][0]["id"]
    read_all(c, "r1", 0.02, s_of, rng)
    read_all(c, "r2", 0.02, s_of, rng, outlier_card=card0)
    out = subprocess.run([sys.executable, str(ROOT / "aggregate.py"), "--data", str(data),
                          "--categories", str(data / "categories.json")], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    sig = {r["ltype"]: r for r in __import__("csv").DictReader(open(data / "out/sigma_table.csv"))}
    for lt in ("frontal|heart|left_cardiac_border", "frontal|G19|superior_left"):
        assert abs(float(sig[lt]["sigma_robust"]) - 0.02) < 0.004, (lt, sig[lt]["sigma_robust"])
    q = json.load(open(data / "out/adjudication_queue.json"))
    assert any(it["card_id"] == card0 and all(k["reason"] == "distance" for k in it["keys"]) for it in q)
    gold = json.load(open(data / "out/gold_kp_v1.json"))
    cats = {x["id"]: x for x in gold["categories"]}
    assert all(len(a["keypoints"]) == 3 * len(cats[a["category_id"]]["keypoints"]) for a in gold["annotations"])
    assert "file_name" not in gold["images"][0]


def test_adjudication_merge(tmp_path, client_factory):
    data, imgs = make_study(tmp_path, n_frontal=30, n_lateral=0, scratch_last=False)
    c = client_factory(data, imgs)
    s_of = {cls: 50.0 for cls in TRUTH}
    rng = np.random.default_rng(1)
    card0 = json.load(open(data / "cards.json"))["cards"][0]["id"]
    read_all(c, "r1", 0.02, s_of, rng)
    read_all(c, "r2", 0.02, s_of, rng, outlier_card=card0)
    run = lambda *a: subprocess.run([sys.executable, str(ROOT / "aggregate.py"), "--data", str(data),
                                     "--categories", str(data / "categories.json"), *a],
                                    capture_output=True, text=True, cwd=ROOT)
    assert run().returncode == 0
    # the bare category list in static/ works as well
    json.dump(json.load(open(data / "categories.json"))["categories"], open(data / "static/categories.json", "w"))
    alt = subprocess.run([sys.executable, str(ROOT / "aggregate.py"), "--data", str(data),
                          "--categories", str(data / "static/categories.json")], capture_output=True, text=True, cwd=ROOT)
    assert alt.returncode == 0, alt.stderr
    adj = client_factory(data, imgs, mode="adjudicate")
    s = adj.get("/api/adj/session").json()
    assert s["total"] >= 1
    item = adj.get("/api/adj/item/0").json()
    keys = {k["key"]: {"choice": "A"} for k in item["keys"]}
    assert adj.post("/api/adj/item/0/resolve", json={"keys": keys}).status_code == 200
    assert run("--final").returncode == 0
    by = json.load(open(data / "out/gold_by_dicom.json"))
    assert by and all(len(v) for v in by.values())
