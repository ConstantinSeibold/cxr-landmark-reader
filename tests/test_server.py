import json
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "goldkp" / "web"


def answer(card, shift=(0.0, 0.0), status="ok"):
    pts = []
    for p in card["points"]:
        xy = p["xy"] or [100.0, 100.0]
        pts.append({"key": p["key"], "status": status if p["xy"] else "added", "unsure": False,
                    "xy": [xy[0] + shift[0], xy[1] + shift[1]], "n_edits": 0})
    return {"points": pts, "active_ms": 1000, "n_events": 1}


def test_card_payload_and_submit_roundtrip(study, client_factory):
    c = client_factory(*study)
    s = c.get("/api/r/r1/session").json()
    assert s["total"] == 7 and s["done"] == 0 and s["cursor"] == 0
    card = c.get("/api/r/r1/card/0").json()
    assert card["gid"] == "G01" and card["full"] and len(card["points"]) == 2
    assert all(p["status"] == "ok" and p["xy"] for p in card["points"])
    assert "dicom_id" not in card and "image" not in card
    r = c.post("/api/r/r1/card/0/submit", json=answer(card, shift=(3, 0), status="moved")).json()
    assert r["ok"] and r["next"] == 1
    again = c.get("/api/r/r1/card/0").json()
    assert again["done"] and all(p["status"] == "moved" for p in again["points"])
    assert c.get("/api/r/r1/session").json()["cursor"] == 1
    log = (study[0] / "readers/r1/decisions.jsonl").read_text().splitlines()
    assert len(log) == 1 and json.loads(log[0])["points"][0]["prefill"] is not None


def test_scratch_cards_have_no_points(study, client_factory):
    c = client_factory(*study)
    total = c.get("/api/r/r1/session").json()["total"]
    cards = [c.get(f"/api/r/r1/card/{i}").json() for i in range(total)]
    scratch = [x for x in cards if x["mode"] == "scratch"]
    assert scratch and all(p["xy"] is None and p["status"] == "missing" for x in scratch for p in x["points"])


@pytest.mark.parametrize("mutate,msg", [
    (lambda b: b["points"].pop(), "cover"),
    (lambda b: b["points"][0].update(status="missing"), "status"),
    (lambda b: b["points"][0].update(xy=[600, 10]), "outside"),
    (lambda b: b["points"].append({"key": "heart|apex", "status": "ok", "xy": [1, 1]}), "cover"),
])
def test_submit_validation(study, client_factory, mutate, msg):
    c = client_factory(*study)
    body = answer(c.get("/api/r/r1/card/0").json())
    mutate(body)
    r = c.post("/api/r/r1/card/0/submit", json=body)
    assert r.status_code == 422 and msg in r.json()["detail"]


def test_readers_are_blinded(study, client_factory):
    c = client_factory(*study)
    card = c.get("/api/r/r1/card/0").json()
    c.post("/api/r/r1/card/0/submit", json=answer(card, shift=(5, 5), status="moved"))
    s2 = c.get("/api/r/r2/session").json()
    assert s2["done"] == 0
    for i in range(s2["total"]):
        assert not c.get(f"/api/r/r2/card/{i}").json()["done"]
    assert c.get("/api/r/r3/session").status_code == 404


def test_resources_and_headers(study, client_factory):
    c = client_factory(*study)
    r = c.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/api/img/0").headers["content-type"] == "image/jpeg"
    assert c.get("/api/img/99").status_code == 404
    assert c.get("/api/img/..%2Fx").status_code in (404, 422)
    outl = c.get("/api/r/r1/card/0/outlines").json()
    roles = {o["name"]: o["role"] for o in outl}
    assert roles == {"heart": "structure", "vertebrae T3": "rule"}
    assert all(o["polys"] and len(o["polys"][0]) >= 4 for o in outl)
    assert c.get("/api/ref/G01_frontal.png").status_code == 200
    assert c.get("/api/ref/..%2Fcards.json").status_code == 404
    assert c.get("/static/server.py").status_code == 404
    assert c.get("/api/adj/session").status_code == 404           # adjudication only in its mode


def test_web_assets_are_self_contained():
    for f in WEB.iterdir():
        assert not re.search(r"https?://", f.read_text()), f"external URL in {f.name}"


def test_main_refuses_public_host(monkeypatch):
    import sys

    from goldkp import server
    monkeypatch.setattr(sys, "argv", ["x", "--data", ".", "--images", ".", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        server.main()


def test_card_index_for_jumping(study, client_factory):
    c = client_factory(*study)
    d = c.get("/api/r/r1/cards").json()
    assert [x["pos"] for x in d["cards"]] == list(range(7))
    assert set(d["cards"][0]) == {"pos", "gid", "view", "batch", "mode", "block", "done"}     # no image identifiers
    assert d["group_names"]["frontal"]["G19"] == "Vertebrae"
    card = c.get("/api/r/r1/card/0").json()
    assert all("reader" in p and "rule" in p and "method" in p and "note" in p for p in card["points"])


def test_shared_subset_order_and_progress(study):
    from fastapi.testclient import TestClient

    from goldkp.server import create_app
    data, imgs = study
    c = TestClient(create_app(data, imgs, ["r1", "r2"], "read", False, None, {"frontal": 1, "lateral": 1}, ["G19"]))
    for rid in ("r1", "r2"):
        d = c.get(f"/api/r/{rid}/cards").json()["cards"]
        blocks = [x["block"] for x in d]
        assert blocks == sorted(blocks) and set(blocks) == {1, 2}      # shared images first
        assert d[0]["gid"] == "G19"
        card = c.get(f"/api/r/{rid}/card/0").json()
        assert card["block"] == 1
        s = c.get(f"/api/r/{rid}/session").json()
        assert s["per_block_group"][f"1|{card['gid']}"]["total"] < s["per_group"][card["gid"]]["total"]
    with pytest.raises(ValueError):
        create_app(data, imgs, ["r1"], "read", False, None, None, ["G99"])


@pytest.mark.parametrize("flags", [["--image-blocks", "0"], ["--subset", "30,20,5"], ["--subset", "x"],
                                   ["--image-blocks", "5", "--subset", "30"]])
def test_order_flags_are_checked(monkeypatch, flags):
    import sys

    from goldkp import server
    monkeypatch.setattr(sys, "argv", ["x", "--data", ".", "--images", ".", *flags])
    with pytest.raises(SystemExit):
        server.main()


def test_partial_export(study, client_factory):
    data, imgs = study
    import shutil
    shutil.copy(data / "categories.json", data / "static" / "categories.json")
    c = client_factory(data, imgs)
    card = c.get("/api/r/r1/card/0").json()
    c.post("/api/r/r1/card/0/submit", json=answer(card, shift=(2, 0), status="moved"))
    r = c.post("/api/r/r1/export").json()
    assert r["done"] == 1 and r["cards"] == 7 and r["answered"] == 2 and r["dir"].startswith("exports/r1/")
    out = data / r["dir"]
    assert {p.name for p in out.iterdir()} == {"annotations.csv", "annotations_by_image.json",
                                              "annotations_coco.json", "summary.json", "decisions.jsonl"}
    summ = json.load(open(out / "summary.json"))
    assert not summ["complete"] and summ["per_group"]["G01|frontal"]["done"] == 1
    coco = json.load(open(out / "annotations_coco.json"))
    assert sum(a["num_keypoints"] for a in coco["annotations"]) == 2
    assert "file_name" not in coco["images"][0]
    assert c.post("/api/r/r2/export").json()["done"] == 0          # nothing yet, still a valid export
