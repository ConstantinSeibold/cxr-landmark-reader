"""Two readers -> agreement, per-landmark tolerances, gold landmarks, adjudication queue.

Per landmark type (view|class|kp; vertebra and rib series pooled over levels as view|gid|kp):
  visibility    Cohen's kappa, raw agreement and PABAK (kappa is unstable when nearly everything
                is visible)
  tolerance     d = distance between the readers, s = sqrt(predicted mask area of the class);
                with independent isotropic errors E[d^2] = 4 sigma^2 s^2, so
                sigma = sqrt(mean(d^2/s^2)/4). Robust version: d^2/s^2 ~ 2 sigma^2 chi2_2, whose
                median is 4 ln2 sigma^2, so sigma_med = sqrt(median/(4 ln2)); pairs beyond the
                agreement limit under sigma_med are dropped and the mean estimate is redone on the
                rest (a single gross disagreement cannot inflate the tolerance that should flag it).
                kappa_k = 2 sigma_k for the OKS of Eq. (1).
  gold          both visible, neither unsure, d <= factor * sigma_k * s  -> mean of the readers
                both not visible                                         -> not visible
                anything else                                            -> adjudication queue
                factor defaults to 3*sqrt(2) (3 SD of the per-axis reader difference).
Adjudication shows the two readers' points as A/B in a random order per item (map kept aside).

usage: aggregate.py --data DATA --readers r1,r2 --categories test_kp_v1.json
                    [--selection selection.csv] [--factor 4.243] [--final]
Outputs (DATA/out): gold_kp_v1.json, gold_by_dicom.json, sigma_table.csv, agreement_report.md,
                    adjudication_queue.json, adjudication_map.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

VISIBLE = {"ok", "moved", "added"}
SERIES = {"G19", "G20"}


def landmark_type(view: str, gid: str, cls: str, kp: str) -> str:
    return f"{view}|{gid}|{kp}" if gid in SERIES else f"{view}|{cls}|{kp}"


def kappa_pabak(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    po = float(np.mean(a == b))
    pa, pb = a.mean(), b.mean()
    pe = pa * pb + (1 - pa) * (1 - pb)
    kappa = (po - pe) / (1 - pe) if pe < 1 else float("nan")
    return kappa, po, 2 * po - 1


def sigma_of(r: np.ndarray) -> float:
    """r = d / s for pairs where both readers placed the point."""
    return float(math.sqrt(np.mean(r ** 2) / 4)) if len(r) else float("nan")


def sigma_robust(r: np.ndarray, factor: float) -> float:
    if not len(r):
        return float("nan")
    sig_med = math.sqrt(np.median(r ** 2) / (4 * math.log(2)))
    inl = r[r <= factor * sig_med]
    return sigma_of(inl) if len(inl) >= 3 else sig_med


def pairs_table(data: Path, readers: list[str]) -> tuple[pd.DataFrame, dict]:
    cj = json.load(open(data / "cards.json"))
    pf_dir = data / "prefill" / cj["prefill_version"]
    states = {r: json.load(open(data / "readers" / r / "state.json"))["latest"] for r in readers}
    areas_by_class = defaultdict(list)
    prefill = {}
    for im in cj["images"]:
        pf = json.load(open(pf_dir / f"{im['dicom_id']}.json"))
        prefill[im["dicom_id"]] = pf
        for cls, m in pf.get("masks", {}).items():
            areas_by_class[cls].append(m["area"])
    med_area = {c: float(np.median(v)) for c, v in areas_by_class.items()}
    rows = []
    for c in cj["cards"]:
        recs = [states[r].get(c["id"]) for r in readers]
        if any(x is None for x in recs):
            continue
        pf = prefill[c["dicom_id"]]
        byk = [{p["key"]: p for p in rec["points"]} for rec in recs]
        for key in c["keys"]:
            cls, kp = key.split("|", 1)
            area = pf.get("masks", {}).get(cls, {}).get("area") or med_area.get(cls)
            pa, pb = byk[0][key], byk[1][key]
            pre = pf["points"].get(cls, {}).get(kp)
            rows.append(dict(card_id=c["id"], dicom_id=c["dicom_id"], img=c["img"], view=c["view"], gid=c["gid"],
                             key=key, cls=cls, kp=kp, ltype=landmark_type(c["view"], c["gid"], cls, kp),
                             mode=c["mode"], s=math.sqrt(area) if area else float("nan"),
                             st_a=pa["status"], st_b=pb["status"], un_a=pa["unsure"], un_b=pb["unsure"],
                             xy_a=pa["xy"], xy_b=pb["xy"],
                             flag_a=recs[0].get("card_flag"), flag_b=recs[1].get("card_flag"),
                             t_a=recs[0].get("active_ms", 0) / len(c["keys"]), t_b=recs[1].get("active_ms", 0) / len(c["keys"]),
                             pre=pre[:2] if pre and pre[2] > 0 else None, pre_src=pre[3] if pre else "none"))
    df = pd.DataFrame(rows)
    df["vis_a"] = df.st_a.isin(VISIBLE)
    df["vis_b"] = df.st_b.isin(VISIBLE)
    both = df.vis_a & df.vis_b
    df["d"] = np.where(both, [math.dist(a, b) if v else np.nan for a, b, v in zip(df.xy_a, df.xy_b, both)], np.nan)
    df["r"] = df.d / df.s
    return df, cj


def sigma_table(df: pd.DataFrame, factor: float) -> pd.DataFrame:
    out = []
    for lt, g in df.groupby("ltype"):
        kappa, po, pabak = kappa_pabak(g.vis_a.values.astype(int), g.vis_b.values.astype(int))
        ok = g[g.vis_a & g.vis_b & ~g.un_a & ~g.un_b]
        r = ok.r.dropna().values
        sig = sigma_of(r)
        sig_rob = sigma_robust(r, factor)
        out.append(dict(ltype=lt, view=g.view.iloc[0], gid=g.gid.iloc[0], n=len(g), n_pairs=len(r),
                        sigma=sig, sigma_robust=sig_rob, kappa_oks=2 * sig_rob, vis_kappa=kappa, vis_agree=po,
                        pabak=pabak, unsure=float((g.un_a | g.un_b).mean()),
                        median_d_px=float(np.nanmedian(g.d)) if g.d.notna().any() else float("nan")))
    return pd.DataFrame(out)


def decide(df: pd.DataFrame, sig: pd.DataFrame, factor: float) -> pd.DataFrame:
    s = sig.set_index("ltype").sigma_robust
    reasons, gold = [], []
    for row in df.itertuples():
        lim = factor * s.get(row.ltype, np.nan) * row.s
        if row.vis_a and row.vis_b:
            if row.un_a or row.un_b:
                reasons.append("unsure"); gold.append(None)
            elif np.isfinite(lim) and row.d <= lim:
                reasons.append(""); gold.append([(row.xy_a[0] + row.xy_b[0]) / 2, (row.xy_a[1] + row.xy_b[1]) / 2])
            else:
                reasons.append("distance"); gold.append(None)
        elif not row.vis_a and not row.vis_b:
            reasons.append(""); gold.append("not_visible")
        else:
            reasons.append("visibility"); gold.append(None)
    df = df.copy()
    df["reason"], df["gold"] = reasons, gold
    return df


def adjudication_queue(df: pd.DataFrame, seed: int = 0) -> tuple[list, dict]:
    rng = random.Random(seed)
    queue, amap = [], {}
    for cid, g in df[df.reason != ""].groupby("card_id", sort=False):
        swap = rng.random() < 0.5
        item_id = f"adj{len(queue):05d}"
        amap[item_id] = {"card_id": cid, "A": "b" if swap else "a"}
        keys = []
        for row in g.itertuples():
            xa = row.xy_a if row.vis_a else None
            xb = row.xy_b if row.vis_b else None
            a, b = (xb, xa) if swap else (xa, xb)
            keys.append({"key": row.key, "reason": row.reason, "a": a, "b": b})
        queue.append({"item_id": item_id, "card_id": cid, "keys": keys})
    return queue, amap


def merge_adjudication(df: pd.DataFrame, data: Path, amap: dict) -> pd.DataFrame:
    st_path = data / "adjudication" / "state.json"
    if not st_path.exists():
        return df
    st = json.load(open(st_path))
    df = df.copy()
    idx = {(r.card_id, r.key): i for i, r in zip(df.index, df.itertuples())}
    for item_id, rec in st.items():
        first = amap[item_id]["A"]
        for key, v in rec["keys"].items():
            i = idx.get((rec["card_id"], key))
            if i is None:
                continue
            row = df.loc[i]
            A, B = (row.xy_a, row.xy_b) if first == "a" else (row.xy_b, row.xy_a)
            ch = v["choice"]
            df.at[i, "gold"] = ("not_visible" if ch == "not_visible" else A if ch == "A" else B if ch == "B"
                                else v["xy"] if ch == "new" else [(A[0] + B[0]) / 2, (A[1] + B[1]) / 2])
            df.at[i, "reason"] = f"adjudicated:{ch}"
    return df


def write_gold(df: pd.DataFrame, cj: dict, categories: Path, out: Path) -> None:
    cats = json.load(open(categories))["categories"]
    cat_by_name = {c["name"]: c for c in cats}
    images, anns, by_dicom = [], [], defaultdict(lambda: defaultdict(dict))
    for i, im in enumerate(cj["images"]):
        images.append({"id": i + 1, "dicom_id": im["dicom_id"], "view": im["view"], "width": 512, "height": 512})
    for (img, cls), g in df.groupby(["img", "cls"]):
        cat = cat_by_name.get(cls)
        if cat is None:
            continue
        kps = [0.0] * (3 * len(cat["keypoints"]))
        src = {}
        for row in g.itertuples():
            if row.kp not in cat["keypoints"] or row.gold is None:
                continue
            j = cat["keypoints"].index(row.kp)
            if row.gold == "not_visible":
                kps[3 * j: 3 * j + 3] = [0.0, 0.0, 0]
                by_dicom[row.dicom_id][cls][row.kp] = [0, 0, 0]
            else:
                kps[3 * j: 3 * j + 3] = [float(row.gold[0]), float(row.gold[1]), 2]
                by_dicom[row.dicom_id][cls][row.kp] = [float(row.gold[0]), float(row.gold[1]), 2]
            src[row.kp] = row.reason or "mean"
        vis = [(kps[3 * j], kps[3 * j + 1]) for j in range(len(cat["keypoints"])) if kps[3 * j + 2] > 0]
        bbox = ([min(p[0] for p in vis), min(p[1] for p in vis), max(p[0] for p in vis) - min(p[0] for p in vis),
                 max(p[1] for p in vis) - min(p[1] for p in vis)] if vis else [0, 0, 0, 0])
        anns.append({"id": len(anns) + 1, "image_id": int(img) + 1, "category_id": cat["id"], "keypoints": kps,
                     "num_keypoints": len(vis), "bbox": bbox, "area": float(bbox[2] * bbox[3]), "iscrowd": 0,
                     "segmentation": [], "kp_source": src})
    json.dump({"info": {"description": "gold landmarks (two readers + adjudication); images not included"},
               "images": images, "annotations": anns, "categories": cats}, open(out / "gold_kp_v1.json", "w"))
    json.dump(by_dicom, open(out / "gold_by_dicom.json", "w"))


def report(df: pd.DataFrame, sig: pd.DataFrame, out: Path, selection: Path | None) -> str:
    lines = ["# Reader agreement", "", f"pairs: {len(df)} landmark readings on {df.card_id.nunique()} cards", ""]
    g = df.groupby("gid")
    per = pd.DataFrame({
        "landmarks": g.size(),
        "median d [px]": g.d.median().round(2),
        "median d/s": g.r.median().round(3),
        "both visible": (df.vis_a & df.vis_b).groupby(df.gid).mean().round(3),
        "unsure": (df.un_a | df.un_b).groupby(df.gid).mean().round(3),
        "to adjudicate": (df.reason.str.len() > 0).groupby(df.gid).mean().round(3),
        "s/landmark A": (g.t_a.median() / 1000).round(1),
        "s/landmark B": (g.t_b.median() / 1000).round(1),
    })
    lines += ["## Per group", "", per.to_markdown(), ""]
    gold = df[df.gold.apply(lambda v: isinstance(v, list))]
    if len(gold):
        e = []
        for row in gold.itertuples():
            if row.pre is not None:
                e.append(dict(src=row.pre_src, r=math.dist(row.pre, row.gold) / row.s))
        if e:
            e = pd.DataFrame(e)
            lines += ["## Pre-fill error against gold (median distance / instance scale)", "",
                      e.groupby("src").r.agg(["count", "median"]).round(3).to_markdown(), ""]
    if selection is not None and selection.exists():
        sel = pd.read_csv(selection).set_index("dicom_id")
        rnd = df[df.dicom_id.map(lambda d: sel.loc[d, "stratum"] == "random" if d in sel.index else False)]
        if len(rnd):
            a = []
            for mode, x in rnd.groupby("mode"):
                ok = x[x.vis_a & x.vis_b]
                a.append(dict(mode=mode, images=x.dicom_id.nunique(), pairs=len(ok), sigma=sigma_of(ok.r.dropna().values),
                              median_d_px=ok.d.median(), s_per_landmark=(x.t_a.median() + x.t_b.median()) / 2000))
            lines += ["## Anchoring check (random stratum; scratch = no pre-fill)", "",
                      "Few scratch images: treat as indicative only.", "",
                      pd.DataFrame(a).round(4).to_markdown(index=False), ""]
    text = "\n".join(lines)
    (out / "agreement_report.md").write_text(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--readers", default="r1,r2")
    ap.add_argument("--categories", type=Path, required=True, help="COCO json whose categories define the gold slots")
    ap.add_argument("--selection", type=Path)
    ap.add_argument("--factor", type=float, default=3 * math.sqrt(2))
    ap.add_argument("--final", action="store_true", help="merge adjudication decisions into the gold files")
    args = ap.parse_args()
    readers = [r.strip() for r in args.readers.split(",")]
    assert len(readers) == 2, "exactly two readers"
    out = args.data / "out"
    out.mkdir(parents=True, exist_ok=True)
    df, cj = pairs_table(args.data, readers)
    if df.empty:
        raise SystemExit("no card has been read by both readers yet")
    sig = sigma_table(df, args.factor)
    sig.to_csv(out / "sigma_table.csv", index=False)
    df = decide(df, sig, args.factor)
    if args.final:
        amap = json.load(open(out / "adjudication_map.json"))
        df = merge_adjudication(df, args.data, amap)
    else:
        queue, amap = adjudication_queue(df)
        json.dump(queue, open(out / "adjudication_queue.json", "w"))
        json.dump(amap, open(out / "adjudication_map.json", "w"))
        print(f"adjudication queue: {len(queue)} cards, {int((df.reason != '').sum())} landmarks")
    write_gold(df, cj, args.categories, out)
    print(report(df, sig, out, args.selection))


if __name__ == "__main__":
    main()
