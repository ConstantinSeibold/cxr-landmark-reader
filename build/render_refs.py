"""Ground-truth reference image per landmark group and view (PAXRay++ silver landmarks).

For each group of vocab.json: a crop of one PAXRay++ frontal / lateral image with the structure
outlines, the group's landmarks in their legend colours, the skeleton edges and a legend.
Series groups (G19 vertebrae, G20 ribs) additionally get a `_series` image with every instance
numbered, because counting is the point of those cards.

Colours are assigned per landmark *name* within a group, so e.g. `superior_left` has the same
colour on every vertebra; refs_manifest.json carries them for the app.

usage: render_refs.py --vocab DATA/static/vocab.json --kp-json paxraypp/annotations/val_kp_v1.json
                      --image-root paxraypp --out DATA/refs [--frontal NAME --lateral NAME]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from pycocotools import mask as M  # noqa: E402

PALETTE = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4", "#42d4f4", "#f032e6",
           "#bfef45", "#fabed4", "#469990", "#dcbeff", "#9a6324", "#fffac8", "#800000", "#aaffc3",
           "#808000", "#ffd8b1", "#000075", "#a9a9a9"]
SERIES_GROUPS = {"G19", "G20"}
REP = {"G19": ("vertebrae T7",), "G20": ("7th rib",)}
FORBIDDEN = "kp_prep/paper"


def instance_label(cls: str) -> str:
    m = re.search(r"vertebrae ([TL]\d+)", cls)
    if m:
        return m.group(1)
    m = re.search(r"(posterior|anterior) (\d+)\w* rib (left|right)", cls)
    if m:
        return f"{m.group(2)}{m.group(3)[0].upper()}"
    return ""


def colours_for(keys: list[str]) -> dict[str, str]:
    names = []
    for k in keys:
        kp = k.split("|", 1)[1]
        if kp not in names:
            names.append(kp)
    by_name = {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(names)}
    return {k: by_name[k.split("|", 1)[1]] for k in keys}


def load(kp_json: Path, root: Path, fname: str):
    d = json.load(open(kp_json))
    im = next(i for i in d["images"] if i["file_name"].endswith(fname))
    cats = {c["id"]: c for c in d["categories"]}
    anns = {cats[a["category_id"]]["name"]: a for a in d["annotations"] if a["image_id"] == im["id"]}
    gray = np.asarray(Image.open(root / im["file_name"]).convert("L"))
    return gray, anns, cats, im["height"], im["width"]


def ann_mask(a, h, w):
    s = a["segmentation"]
    r = M.merge(M.frPyObjects(s, h, w)) if isinstance(s, list) else (
        M.frPyObjects(s, h, w) if isinstance(s.get("counts"), list) else s)
    return M.decode(r).astype(bool)


def render(view_data, keys, colours, edges, out: Path, series: bool, title: str) -> bool:
    gray, anns, cats, h, w = view_data
    pts, masks = [], {}
    for key in keys:
        cls, kp = key.split("|", 1)
        a = anns.get(cls)
        if a is None:
            continue
        names = cats[a["category_id"]]["keypoints"]
        if kp not in names:
            continue
        s = names.index(kp)
        x, y, v = a["keypoints"][3 * s: 3 * s + 3]
        if v > 0:
            pts.append((x, y, key))
            masks.setdefault(cls, ann_mask(a, h, w))
    if not pts:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    for m in masks.values():
        yy, xx = np.nonzero(m)
        if len(xx):
            xs += [xx.min(), xx.max()]
            ys += [yy.min(), yy.max()]
    pad = 20
    x0, x1 = max(0, min(xs) - pad), min(w, max(xs) + pad)
    y0, y1 = max(0, min(ys) - pad), min(h, max(ys) + pad)
    side = max(x1 - x0, y1 - y0, 120)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    x0, x1 = max(0, cx - side / 2), min(w, cx + side / 2)
    y0, y1 = max(0, cy - side / 2), min(h, cy + side / 2)

    legend = []
    for key in keys:
        kp = key.split("|", 1)[1]
        if kp not in [l[0] for l in legend] and any(p[2] == key for p in pts):
            legend.append((kp, colours[key]))
    leg_h = 0.22 * ((len(legend) + 1) // 2) + 0.15
    fig_h = 6.4 + leg_h + 0.35
    fig = plt.figure(figsize=(6.4, fig_h), dpi=110)
    ax = fig.add_axes([0, leg_h / fig_h, 1, 6.4 / fig_h])          # legend below, title above
    ax.imshow(gray, cmap="gray", vmin=0, vmax=255, interpolation="lanczos")
    for m in masks.values():
        ax.contour(m, levels=[0.5], colors=["#9ad1d4"], linewidths=0.5, alpha=0.8)
    at = {p[2]: (p[0], p[1]) for p in pts}
    for a, b in edges:
        if a in at and b in at:
            ax.plot([at[a][0], at[b][0]], [at[a][1], at[b][1]], color="#ffd166", lw=0.9, zorder=4)
    for x, y, key in pts:
        ax.scatter(x, y, s=26, c=colours[key], edgecolors="white", linewidths=0.6, zorder=5)
    if series:
        done = set()
        for x, y, key in pts:
            cls = key.split("|", 1)[0]
            if cls in done:
                continue
            done.add(cls)
            ax.text(x + 4, y - 3, instance_label(cls), color="white", fontsize=7, zorder=6,
                    bbox=dict(boxstyle="round,pad=0.1", fc="black", ec="none", alpha=0.6))
    ax.set_xlim(x0, x1)
    ax.set_ylim(y1, y0)
    ax.axis("off")
    ax.set_title(title, fontsize=10)
    handles = [plt.Line2D([], [], marker="o", ls="", color=c, markeredgecolor="white", label=n.replace("_", " "))
               for n, c in legend]
    fig.legend(handles=handles, loc="lower center", ncol=2, fontsize=8, frameon=False)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vocab", type=Path, required=True)
    ap.add_argument("--kp-json", type=Path, required=True)
    ap.add_argument("--image-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--frontal", default="COVID19AR_113_frontal.png")
    ap.add_argument("--lateral", default="RSNAPE_406539bcd44e_e0fd55ae1726_lateral.png")
    args = ap.parse_args()
    if FORBIDDEN in str(args.out.resolve()):
        raise SystemExit(f"refusing to write into {FORBIDDEN} (synced to Overleaf)")
    vocab = json.load(open(args.vocab))
    data = {"frontal": load(args.kp_json, args.image_root, args.frontal),
            "lateral": load(args.kp_json, args.image_root, args.lateral)}
    manifest = {"colours": {}, "files": {}}
    for view, groups in vocab["groups"].items():
        edges = vocab["edges"][view]
        for gid, g in groups.items():
            keys = g["keys"]
            colours = colours_for(keys)
            manifest["colours"].update({f"{view}|{k}": c for k, c in colours.items()})
            entry = {}
            if gid in SERIES_GROUPS:
                rep = [k for k in keys if any(r in k.split("|")[0] and not k.split("|")[0][len(r):len(r) + 1].isdigit()
                                              for r in REP[gid])]
                if render(data[view], rep, colours, edges, args.out / f"{gid}_{view}.png", False,
                          f"{gid} {g['name']} – one instance"):
                    entry["main"] = f"{gid}_{view}.png"
                if render(data[view], keys, colours, edges, args.out / f"{gid}_{view}_series.png", True,
                          f"{gid} {g['name']} – all levels"):
                    entry["series"] = f"{gid}_{view}_series.png"
            elif render(data[view], keys, colours, edges, args.out / f"{gid}_{view}.png", False, f"{gid} {g['name']}"):
                entry["main"] = f"{gid}_{view}.png"
            manifest["files"].setdefault(gid, {})[view] = entry
            if not entry:
                print(f"  no reference for {gid} {view} (landmarks not present on the reference image)")
    json.dump(manifest, open(args.out / "refs_manifest.json", "w"), indent=1)
    n = sum(len(v) for g in manifest["files"].values() for v in g.values())
    print(f"wrote {n} reference images to {args.out}")


if __name__ == "__main__":
    main()
