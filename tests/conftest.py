"""Synthetic study (no real images): a few 512 px PNGs, a tiny vocabulary with a normal group, a
vertebra series and a lateral group, pre-fill with masks, cards."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pycocotools import mask as M

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from goldkp.cards import build_cards, cards_sha  # noqa: E402

VOCAB = {
    "version": "test",
    "groups": {
        "frontal": {
            "G01": {"name": "Cardiothoracic ratio", "region": "Heart", "description": "CTR",
                    "keys": ["heart|left_cardiac_border", "heart|right_cardiac_border"]},
            "G19": {"name": "Vertebrae", "region": "Bones", "description": "spine",
                    "keys": ["vertebrae T3|superior_left", "vertebrae T3|superior_right",
                             "vertebrae T7|superior_left", "vertebrae T7|superior_right"]},
        },
        "lateral": {
            "G02": {"name": "Sulci", "region": "Pleura", "description": "sulci",
                    "keys": ["diaphragm|anterior_costophrenic", "diaphragm|posterior_costophrenic"]},
        },
    },
    "edges": {"frontal": [["heart|left_cardiac_border", "heart|right_cardiac_border"]], "lateral": []},
}
# ground truth used by the scripted readers: class -> kp -> (x, y)
TRUTH = {"heart": {"left_cardiac_border": (330, 300), "right_cardiac_border": (190, 300)},
         "vertebrae T3": {"superior_left": (262, 120), "superior_right": (248, 120)},
         "vertebrae T7": {"superior_left": (264, 220), "superior_right": (246, 220)},
         "diaphragm": {"anterior_costophrenic": (380, 420), "posterior_costophrenic": (120, 430)}}
BOXES = {"heart": (150, 220, 370, 380), "vertebrae T3": (240, 110, 270, 135),
         "vertebrae T7": (238, 210, 272, 236), "diaphragm": (100, 300, 400, 440)}


def _rle(box):
    m = np.zeros((512, 512), np.uint8)
    x0, y0, x1, y1 = box
    m[y0:y1, x0:x1] = 1
    r = M.encode(np.asfortranarray(m))
    r["counts"] = r["counts"].decode()
    return r, int(m.sum())


def make_study(tmp: Path, n_frontal: int = 2, n_lateral: int = 1, scratch_last: bool = True) -> tuple[Path, Path]:
    data, imgs = tmp / "data", tmp / "images"
    (data / "static").mkdir(parents=True)
    (data / "refs").mkdir()
    imgs.mkdir()
    json.dump(VOCAB, open(data / "static/vocab.json", "w"))
    defs = {f"{v}|{k}": {"label": k, "rule": f"rule for {k}", "comment": "", "method": "m", "source": "K0"}
            for v, gs in VOCAB["groups"].items() for g in gs.values() for k in g["keys"]}
    json.dump(defs, open(data / "static/definitions.json", "w"))
    Image.new("RGB", (64, 64), "gray").save(data / "refs/G01_frontal.png")
    json.dump({"colours": {f"frontal|{k}": "#ff0000" for k in VOCAB["groups"]["frontal"]["G01"]["keys"]},
               "files": {"G01": {"frontal": {"main": "G01_frontal.png"}}}}, open(data / "refs/refs_manifest.json", "w"))
    version = "test_v"
    pf_dir = data / "prefill" / version
    pf_dir.mkdir(parents=True)
    json.dump({"version": version}, open(pf_dir / "manifest.json", "w"))
    images, prefill = [], {}
    views = ["frontal"] * n_frontal + ["lateral"] * n_lateral
    for i, view in enumerate(views):
        did = f"img{i:04d}"
        Image.fromarray((np.random.default_rng(i).random((512, 512)) * 255).astype(np.uint8)).save(imgs / f"{did}.png")
        classes = {k.split("|")[0] for g in VOCAB["groups"][view].values() for k in g["keys"]}
        masks, points = {}, {}
        for cls in classes:
            rle, area = _rle(BOXES[cls])
            x0, y0, x1, y1 = BOXES[cls]
            masks[cls] = {"rle": rle, "area": area, "score": 1.0, "bbox": [x0, y0, x1, y1]}
            points[cls] = {kp: [x + 2.0, y - 1.0, 2, "v1_rules"] for kp, (x, y) in TRUTH[cls].items()}
        no_prefill = scratch_last and i == len(views) - 1
        rec = {"dicom_id": did, "view": view, "image": f"{did}.png", "version": version,
               "no_prefill": no_prefill, "orig_wh": [2500, 3000] if view == "frontal" else None,
               "points": points, "masks": masks}
        json.dump(rec, open(pf_dir / f"{did}.json", "w"))
        prefill[did] = rec
        images.append({"dicom_id": did, "view": view, "image": f"{did}.png", "no_prefill": no_prefill})
    cards = build_cards(images, VOCAB, prefill)
    json.dump({"sha": cards_sha(cards), "prefill_version": version, "images": images, "cards": cards},
              open(data / "cards.json", "w"))
    cats = [{"id": i + 1, "name": cls, "keypoints": list(TRUTH[cls]), "skeleton": []} for i, cls in enumerate(TRUTH)]
    json.dump({"images": [], "annotations": [], "categories": cats}, open(data / "categories.json", "w"))
    return data, imgs


@pytest.fixture
def study(tmp_path):
    return make_study(tmp_path)


@pytest.fixture
def client_factory():
    from fastapi.testclient import TestClient

    from goldkp.server import create_app

    def make(data, imgs, readers=("r1", "r2"), mode="read"):
        return TestClient(create_app(data, imgs, list(readers), mode))
    return make
