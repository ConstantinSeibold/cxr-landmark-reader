"""Pre-fill points and assist masks for the reader app, one JSON per image.

Sources (--source):
  hybrid         learned landmark of the current (v0-trained) model where it is safe -- the slot
                 exists in v0, the landmark was not redefined for v1 (no row in the v1 rule-change
                 table), the class is not one of the side-swapped pairs, and the model calls it
                 visible -- otherwise the v1 rules applied to the model's predicted masks
  rules_v0masks  v1 rules on the predicted masks for every landmark
  v1_learned     learned landmarks of a v1-trained model (slots of the v1 categories)

Masks are side-corrected on frontals (patient-left = image-right, as in the v1 export) and
stored as COCO RLE for the toggleable mask overlay and the crop boxes. Images flagged
no_prefill keep their masks; the server never sends their points.

output: OUT/<dicom_id>.json  {dicom_id, view, image, orig_wh, version, no_prefill,
                              points: {class: {kp: [x, y, vis, source]}},
                              masks: {class: {rle, area, bbox, score}}}
        OUT/manifest.json, OUT/coverage.csv, OUT/learned_vs_rules.csv (overlapping landmarks)

usage: build_prefill.py --kp-prep KP_PREP --selection selection.csv --preds prefill_preds.pkl
         --v0-json test_kp.json --vocab DATA/static/vocab.json --images IMAGES_512
         --metadata mimic-cxr-2.0.0-metadata.csv.gz --out DATA/prefill/<version> --version NAME
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pickle
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from pycocotools import mask as M

MIN_SCORE = 0.3
MIN_VIS = 0.5
RELABEL_PAIRS = [("clavicle left", "clavicle right"),
                 ("left hemidiaphragm", "right hemidiaphragm"),
                 ("less obstructed left lung", "less obstructed right lung")]
SWAPPED = {c for pair in RELABEL_PAIRS for c in pair}


def redefined_keys(kp_prep: Path) -> set[tuple[str, str, str]]:
    """(view, class, kp) whose K-ID has a row in the v1 rule-change table."""
    ids = set()
    with open(kp_prep / "taxonomy/kp_rule_changes_v1.csv") as f:
        for r in csv.DictReader(f):
            for a, b in re.findall(r"K(\d+)-K(\d+)", r["ID"]):
                ids |= {f"K{i:03d}" for i in range(int(a), int(b) + 1)}
            ids |= set(re.findall(r"K\d+", r["ID"]))
    prov = json.load(open(kp_prep / "taxonomy/final_vocab_v1.json"))["provenance"]
    return {(p["view"], p["class"], p["kp"]) for p in prov
            if p["source"].partition("+")[0] in ids or p["source"] == "added"}


def side_fix(masks: dict) -> None:
    """Swap left/right names of the inconsistent PAXRay++ pairs by image position (in place)."""
    def cx(m):
        xs = np.nonzero(m)[1]
        return xs.mean() if len(xs) else None
    verts = [cx(m) for n, m in masks.items() if n.startswith("vertebrae T") and m.any()]
    mid = float(np.median(verts)) if verts else 256.0
    for l, r in RELABEL_PAIRS:
        ml, mr = masks.get(l), masks.get(r)
        if ml is None and mr is None:
            continue
        if ml is not None and mr is not None:
            wrong = cx(ml) < cx(mr)
        else:
            wrong = (cx(ml if ml is not None else mr) > mid) != (ml is not None)
        if wrong:
            if ml is not None:
                masks[r] = ml
            else:
                masks.pop(r, None)
            if mr is not None:
                masks[l] = mr
            else:
                masks.pop(l, None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kp-prep", type=Path, required=True)
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--preds", type=Path, required=True)
    ap.add_argument("--v0-json", type=Path, required=True, help="categories of the model's slots")
    ap.add_argument("--vocab", type=Path, required=True)
    ap.add_argument("--metadata", type=Path, required=True)
    ap.add_argument("--source", choices=["hybrid", "rules_v0masks", "v1_learned"], default="hybrid")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--version", required=True)
    args = ap.parse_args()

    os.environ.setdefault("KP_FINAL_VOCAB", str(args.kp_prep / "taxonomy/final_vocab_v1.json"))
    sys.path.insert(0, str(args.kp_prep))
    import extract_keypoints as E
    import keypoint_schema as KS

    sel = pd.read_csv(args.selection)
    preds = pickle.load(open(args.preds, "rb"))
    cats = {c["id"]: c for c in json.load(open(args.v0_json))["categories"]}
    vocab = json.load(open(args.vocab))
    redefined = redefined_keys(args.kp_prep)
    meta = pd.read_csv(args.metadata, usecols=["dicom_id", "Rows", "Columns"]).set_index("dicom_id")
    args.out.mkdir(parents=True, exist_ok=True)

    cover = defaultdict(lambda: defaultdict(int))
    compare = []
    for r in sel.itertuples():
        view = "lateral" if r.image == "lateral" else "frontal"
        per = preds[r.path]
        masks, info = {}, {}
        for cid, v in per.items():
            if v["score"] < MIN_SCORE:
                continue
            m = M.decode(v["rle"]).astype(bool)
            if m.any():
                masks[cats[cid]["name"]] = E.clean_mask(m)
                info[cats[cid]["name"]] = float(v["score"])
        learned = {cats[cid]["name"]: (cats[cid]["keypoints"], v["kpts"], v["vis"])
                   for cid, v in per.items() if v["score"] >= MIN_SCORE}
        if view == "frontal":
            side_fix(masks)
        ctx = E.make_ctx(masks, 512, 512)
        schema = KS.SCHEMA_LATERAL if view == "lateral" else KS.SCHEMA_FRONTAL
        keys = [k for g in vocab["groups"][view].values() for k in g["keys"]]
        rules_cache: dict = {}
        points = defaultdict(dict)
        for key in keys:
            cls, kp = key.split("|", 1)
            if cls not in rules_cache:
                try:
                    rules_cache[cls] = E.extract_for_class(cls, masks[cls], schema, ctx, masks) if cls in masks else {}
                except Exception:
                    rules_cache[cls] = {}
            rp = rules_cache[cls].get(kp)
            rule_pt = [float(rp[0]), float(rp[1]), 2, "v1_rules"] if rp and rp[2] > 0 else None
            lp = None
            if cls in learned and kp in learned[cls][0]:
                i = learned[cls][0].index(kp)
                x, y = learned[cls][1][i]
                vis = float(learned[cls][2][i])
                lp = [float(x), float(y), 2 if vis >= MIN_VIS else 0, "v0_learned"]
                if rule_pt:
                    s = np.sqrt(masks[cls].sum()) if cls in masks else np.nan
                    compare.append(dict(view=view, cls=cls, kp=kp, redefined=(view, cls, kp) in redefined,
                                        d_over_s=float(np.hypot(x - rule_pt[0], y - rule_pt[1]) / s)))
            use_learned = (args.source == "hybrid" and lp is not None and lp[2] > 0
                           and (view, cls, kp) not in redefined and cls not in SWAPPED)
            if args.source == "v1_learned":
                use_learned = lp is not None and lp[2] > 0
            pt = lp if use_learned else rule_pt
            points[cls][kp] = pt if pt else [0.0, 0.0, 0, "none"]
            cover[(view, key)]["n"] += 1
            cover[(view, key)][pt[3] if pt else "none"] += 1
        out_masks = {}
        for cls, m in masks.items():
            if not any(k.split("|", 1)[0] == cls for k in keys):
                continue
            rle = M.encode(np.asfortranarray(m.astype(np.uint8)))
            rle["counts"] = rle["counts"].decode()
            ys, xs = np.nonzero(m)
            out_masks[cls] = {"rle": rle, "area": int(m.sum()), "score": info.get(cls, 1.0),
                              "bbox": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]}
        orig = meta.loc[r.dicom_id] if r.dicom_id in meta.index else None
        rec = {"dicom_id": r.dicom_id, "view": view, "image": r.path, "version": args.version,
               "no_prefill": bool(r.no_prefill),
               "orig_wh": [int(orig.Columns), int(orig.Rows)] if orig is not None else None,
               "points": points, "masks": out_masks}
        json.dump(rec, open(args.out / f"{r.dicom_id}.json", "w"))

    rows = [dict(view=v, key=k, **c) for (v, k), c in cover.items()]
    cov = pd.DataFrame(rows).fillna(0)
    cov.to_csv(args.out / "coverage.csv", index=False)
    pd.DataFrame(compare).to_csv(args.out / "learned_vs_rules.csv", index=False)
    json.dump({"version": args.version, "source": args.source, "n_images": len(sel),
               "min_score": MIN_SCORE, "min_vis": MIN_VIS}, open(args.out / "manifest.json", "w"), indent=1)
    for view, g in cov.groupby("view"):
        tot = g["n"].sum()
        print(f"{view}: " + ", ".join(f"{c} {g[c].sum() / tot:.1%}" for c in ("v0_learned", "v1_rules", "none") if c in g))
    cmp = pd.DataFrame(compare)
    if len(cmp):
        print("learned vs v1 rules, median distance / instance scale:",
              cmp.groupby("redefined").d_over_s.median().round(3).to_dict(), "(False = unchanged definition)")


if __name__ == "__main__":
    main()
