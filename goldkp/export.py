"""Export one reader's annotations as they stand -- at any point, finished or not.

Writes to OUT/<rid>/<timestamp>/:
  annotations.csv            one row per landmark of every answered card
  annotations_by_image.json  {dicom_id: {class: {kp: [x, y, v, status, unsure]}}}
  annotations_coco.json      COCO keypoints with the gold categories (static/categories.json);
                             unanswered or not-visible landmarks have v = 0
  summary.json               progress per group and view, versions, timestamp
  decisions.jsonl            the reader's raw log (lossless; python -m goldkp.project import-reader)

usage: python -m goldkp.export --data DATA [--readers r1,r2] [--out DATA/exports]
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from collections import defaultdict
from pathlib import Path

VISIBLE = {"ok", "moved", "added"}


def export_reader(data: Path, rid: str, out_root: Path | None = None) -> dict:
    cj = json.load(open(data / "cards.json"))
    state_path = data / "readers" / rid / "state.json"
    latest = json.load(open(state_path))["latest"] if state_path.exists() else {}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = (out_root or data / "exports") / rid / stamp
    out.mkdir(parents=True, exist_ok=True)
    pf_dir = data / "prefill" / cj["prefill_version"]
    prefill_src: dict = {}

    rows, by_image = [], defaultdict(lambda: defaultdict(dict))
    progress = defaultdict(lambda: {"cards": 0, "done": 0, "landmarks": 0, "answered": 0})
    for c in cj["cards"]:
        g = progress[f"{c['gid']}|{c['view']}"]
        g["cards"] += 1
        g["landmarks"] += len(c["keys"])
        rec = latest.get(c["id"])
        if rec is None:
            continue
        g["done"] += 1
        g["answered"] += len(c["keys"])
        if c["dicom_id"] not in prefill_src:
            pf = json.load(open(pf_dir / f"{c['dicom_id']}.json"))
            prefill_src[c["dicom_id"]] = pf["points"]
        for p in rec["points"]:
            cls, kp = p["key"].split("|", 1)
            pre = prefill_src[c["dicom_id"]].get(cls, {}).get(kp)
            vis = p["status"] in VISIBLE
            x, y = (p["xy"] if vis else (None, None))
            rows.append({"reader": rid, "dicom_id": c["dicom_id"], "view": c["view"], "gid": c["gid"],
                         "batch": c["batch"], "class": cls, "kp": kp, "status": p["status"], "unsure": p["unsure"],
                         "x": x, "y": y, "prefill_x": pre[0] if pre and pre[2] > 0 and c["mode"] == "prefill" else None,
                         "prefill_y": pre[1] if pre and pre[2] > 0 and c["mode"] == "prefill" else None,
                         "prefill_source": pre[3] if pre and c["mode"] == "prefill" else "none", "mode": c["mode"],
                         "card_flag": rec.get("card_flag"), "active_ms": rec.get("active_ms", 0),
                         "submitted": rec.get("ts")})
            by_image[c["dicom_id"]][cls][kp] = [x, y, 2 if vis else 0, p["status"], p["unsure"]]

    with open(out / "annotations.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["reader"])
        w.writeheader()
        w.writerows(rows)
    json.dump(by_image, open(out / "annotations_by_image.json", "w"))

    cats_path = data / "static" / "categories.json"
    if cats_path.exists():
        cats = json.load(open(cats_path))
        cats = cats["categories"] if isinstance(cats, dict) else cats       # a full COCO file works too
        cat_by_name = {c["name"]: c for c in cats}
        images = [{"id": i + 1, "dicom_id": im["dicom_id"], "view": im["view"], "width": 512, "height": 512}
                  for i, im in enumerate(cj["images"])]
        img_id = {im["dicom_id"]: im["id"] for im in images}
        anns = []
        for did, classes in by_image.items():
            for cls, kps in classes.items():
                cat = cat_by_name.get(cls)
                if cat is None:
                    continue
                flat = [0.0] * (3 * len(cat["keypoints"]))
                for kp, (x, y, v, _, _) in kps.items():
                    if kp in cat["keypoints"] and v:
                        j = cat["keypoints"].index(kp)
                        flat[3 * j: 3 * j + 3] = [float(x), float(y), 2]
                anns.append({"id": len(anns) + 1, "image_id": img_id[did], "category_id": cat["id"], "keypoints": flat,
                             "num_keypoints": sum(1 for v in flat[2::3] if v), "iscrowd": 0, "segmentation": [],
                             "bbox": [0, 0, 0, 0], "area": 0.0})
        json.dump({"info": {"description": f"reader {rid}, partial export {stamp}; images not included"},
                   "images": images, "annotations": anns, "categories": cats}, open(out / "annotations_coco.json", "w"))

    total = {k: sum(g[k] for g in progress.values()) for k in ("cards", "done", "landmarks", "answered")}
    summary = {"reader": rid, "exported": stamp, "cards_sha": cj["sha"], "prefill_version": cj["prefill_version"],
               "complete": total["done"] == total["cards"], "total": total, "per_group": progress}
    json.dump(summary, open(out / "summary.json", "w"), indent=1)
    # the raw log too: lossless, so the snapshot can be imported into another copy of the study
    log = data / "readers" / rid / "decisions.jsonl"
    if log.exists():
        (out / "decisions.jsonl").write_bytes(log.read_bytes())
    return {"dir": str(out), **total}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--readers", default="r1,r2")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    for rid in [r.strip() for r in args.readers.split(",") if r.strip()]:
        r = export_reader(args.data, rid, args.out)
        print(f"{rid}: {r['done']}/{r['cards']} cards, {r['answered']} landmarks -> {r['dir']}")


if __name__ == "__main__":
    main()
