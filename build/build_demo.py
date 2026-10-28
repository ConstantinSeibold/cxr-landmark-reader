"""Demo study from open PAXRay++ test images (never MIMIC): used for screenshots, the demo GIF and
the Playwright tests.

Pre-fill = ground-truth landmarks + Gaussian noise, a few points dropped so placement can be shown;
masks = ground-truth segmentations. One lateral image is flagged no_prefill (place-from-scratch).

usage: build_demo.py --kp-json paxraypp/annotations/test_kp_v1.json --image-root paxraypp
                     --static DATA/static --refs DATA/refs --out demo_data [--frontal 4 --lateral 2]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from pycocotools import mask as M

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goldkp.cards import build_cards, cards_sha  # noqa: E402

NOISE_PX, DROP = 3.0, 0.05
VERSION = "demo_gt_noise"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kp-json", type=Path, required=True)
    ap.add_argument("--image-root", type=Path, required=True)
    ap.add_argument("--static", type=Path, required=True)
    ap.add_argument("--refs", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--frontal", type=int, default=4)
    ap.add_argument("--lateral", type=int, default=2)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    d = json.load(open(args.kp_json))
    cats = {c["id"]: c for c in d["categories"]}
    by = {}
    for a in d["annotations"]:
        by.setdefault(a["image_id"], []).append(a)
    vocab = json.load(open(args.static / "vocab.json"))
    defs = json.load(open(args.static / "definitions.json"))
    pick = ([i for i in d["images"] if "frontal" in i["file_name"]][5: 5 + args.frontal]
            + [i for i in d["images"] if "lateral" in i["file_name"]][5: 5 + args.lateral])

    if args.out.exists():
        shutil.rmtree(args.out)
    (args.out / "images").mkdir(parents=True)
    shutil.copytree(args.static, args.out / "static")
    shutil.copytree(args.refs, args.out / "refs")
    pf_dir = args.out / "prefill" / VERSION
    pf_dir.mkdir(parents=True)
    images, prefill = [], {}
    for n, im in enumerate(pick):
        view = "lateral" if "lateral" in im["file_name"] else "frontal"
        did = f"demo{n:04d}"
        dst = f"{did}.png"
        shutil.copy(args.image_root / im["file_name"], args.out / "images" / dst)
        keys = {k for g in vocab["groups"][view].values() for k in g["keys"]}
        classes = {k.split("|", 1)[0] for k in keys}
        partners = {v for k in keys for v in defs.get(f"{view}|{k}", {}).get("via", [])}
        points, masks = {}, {}
        for a in by.get(im["id"], []):
            c = cats[a["category_id"]]
            if c["name"] not in classes | partners:
                continue
            k = np.array(a["keypoints"]).reshape(-1, 3)
            pts = {}
            for name, (x, y, v) in zip(c["keypoints"], k):
                if f"{c['name']}|{name}" not in keys:
                    continue
                if v > 0 and rng.random() > DROP:
                    x2, y2 = np.clip(np.array([x, y]) + rng.normal(0, NOISE_PX, 2), 0, 511.9)
                    pts[name] = [float(x2), float(y2), 2, "demo_gt_noise"]
                else:
                    pts[name] = [0.0, 0.0, 0, "none"]
            if c["name"] in classes:
                points[c["name"]] = pts
            s = a["segmentation"]
            r = M.merge(M.frPyObjects(s, 512, 512)) if isinstance(s, list) else (
                M.frPyObjects(s, 512, 512) if isinstance(s.get("counts"), list) else s)
            m = M.decode(r).astype(bool)
            if m.any():
                rle = M.encode(np.asfortranarray(m.astype(np.uint8)))
                rle["counts"] = rle["counts"].decode()
                ys, xs = np.nonzero(m)
                masks[c["name"]] = {"rle": rle, "area": int(m.sum()), "score": 1.0,
                                    "bbox": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]}
        no_prefill = view == "lateral" and n == len(pick) - 1
        rec = {"dicom_id": did, "view": view, "image": dst, "version": VERSION, "no_prefill": no_prefill,
               "orig_wh": None, "points": points, "masks": masks}
        json.dump(rec, open(pf_dir / f"{did}.json", "w"))
        prefill[did] = rec
        images.append({"dicom_id": did, "view": view, "image": dst, "no_prefill": no_prefill,
                       "source": im["file_name"]})
    json.dump({"version": VERSION, "source": "paxraypp_gt_noise"}, open(pf_dir / "manifest.json", "w"))
    cards = build_cards(images, vocab, prefill)
    json.dump({"sha": cards_sha(cards), "prefill_version": VERSION, "images": images, "cards": cards},
              open(args.out / "cards.json", "w"))
    print(f"demo: {len(images)} PAXRay++ images, {len(cards)} cards -> {args.out}")
    print("sources:", [i["source"] for i in images])


if __name__ == "__main__":
    main()
