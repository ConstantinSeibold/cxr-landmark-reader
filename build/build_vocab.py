"""Snapshot the final landmark vocabulary for the reader app.

Reads the kp_prep taxonomy (final vocabulary, group table, rule changes, rating outcomes) and the
keypoint schema, and writes self-contained JSON so the app never imports the research code:

  vocab.json        groups[view][gid] = {name, region, description, keys: ["class|kp", ...]}
                    plus edges[view] (skeleton pairs per class, v1-pruned)
  definitions.json  "view|class|kp" -> {label, rule, comment, method, source}

usage: build_vocab.py --kp-prep /path/to/kp_prep --out DATA_DIR/static
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import re
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path

FALLBACK_GROUP = "G16"  # provenance leaves lateral heart|hoffman_rigler ungrouped; the paper files it here


def load_taxonomy_helpers(path: Path) -> tuple[dict, callable]:
    """GROUPS and method_text from build_taxonomy_sheet.py without importing it (it needs openpyxl)."""
    tree = ast.parse(path.read_text())
    keep = [n for n in tree.body
            if (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "GROUPS" for t in n.targets))
            or (isinstance(n, ast.FunctionDef) and n.name == "method_text")]
    ns = {"OrderedDict": OrderedDict}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), ns)
    return ns["GROUPS"], ns["method_text"]


def pretty(kp: str) -> str:
    return kp.replace("_", " ")


# readable text for extraction methods introduced with the v1 rules (the taxonomy helper predates them)
V1_METHODS = {
    "endplate": lambda a: "vertebral-body corner: {} endplate, {} end".format(
        {"S": "superior", "I": "inferior"}.get(a[:1], a[:1]),
        {"L": "left", "R": "right", "A": "anterior", "P": "posterior"}.get(a[1:2], a[1:2])),
    "rib_slope_limb": lambda a: "point along the posterior rib's centreline, a fixed distance from the rib head",
    "cph_limb": lambda a: "cardiophrenic angle limb: point along the heart border / diaphragm from the vertex",
    "cp_limb": lambda a: "costophrenic angle limb: point along the chest wall / diaphragm from the vertex",
    "sulcus_limb": lambda a: "sulcus limb: point along the chest wall / diaphragm from the sulcus vertex",
    "sulcus_vertex": lambda a: "deepest point of the costophrenic sulcus on the lateral view",
    "visible_corner": lambda a: "corner of the visible (not obscured) lung outline",
    "sternodiaphragmatic": lambda a: "sternodiaphragmatic angle: vertex or limb point",
    "manubriosternal_limb": lambda a: "manubriosternal angle limb (manubrium / sternal body)",
    "subcarinal_limb": lambda a: "subcarinal angle limb along the main bronchus",
    "trachea_axis": lambda a: "point on the tracheal axis",
    "plateau": lambda a: "end of the flat part of the contour",
    "lmc": lambda a: "lateral lung margin at the given level",
    "dome_lateral": lambda a: "highest point of the hemidiaphragm dome",
    "lateral_at_hilum": lambda a: "lateral lung margin at the height of the hilum",
    "hilar_point": lambda a: "hilar point (where the upper-lobe vein crosses the lower-lobe artery)",
    "retrosternal": lambda a: "border of the retrosternal clear space",
    "bronchus_mid": lambda a: "midpoint of the main bronchus",
    "tb_angle": lambda a: "right tracheobronchial angle",
    "mw_right": lambda a: "right mediastinal border at the level of the widest mediastinum",
    "knob_silhouette": lambda a: "most lateral point of the aortic knob silhouette",
    "summit_distal": lambda a: "summit of the aortic arch",
    "carina_notch": lambda a: "carina (notch between the main bronchi)",
    "trachea_at_clavicles": lambda a: "trachea at the level of the medial clavicle ends",
    "fundus_under_diaphragm": lambda a: "apex of the gastric fundus under the left hemidiaphragm",
    "heart_apex": lambda a: "cardiac apex",
    "ridge_mid": lambda a: "middle of the superior diaphragm contour",
    "apex_lateral": lambda a: "lung apex",
    "hoffman_rigler": lambda a: "posterior heart border 2 cm above the IVC–heart intersection (Hoffman–Rigler)",
}


def readable_method(kpdef, method_text) -> str:
    if kpdef is None:
        return ""
    head, _, arg = kpdef.method.partition(":")
    if head in V1_METHODS:
        return V1_METHODS[head](arg)
    return method_text(kpdef.method, kpdef.via)


def rule_index(path: Path) -> list[tuple[set, str, bool, str]]:
    """Rows of kp_rule_changes_v1.csv as (base K-IDs, suffix, is_review, rule)."""
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rid = r["ID"]
            ks = set()
            for a, b in re.findall(r"K(\d+)-K(\d+)", rid):
                ks |= {f"K{i:03d}" for i in range(int(a), int(b) + 1)}
            ks |= set(re.findall(r"K\d+", rid))
            suffix = rid.split("+", 1)[1].split()[0] if "+" in rid else ""
            rows.append((ks, suffix, "(" in rid, r["v1 rule"].strip()))
    return rows


def rule_for(source: str, kp: str, rows) -> str:
    base, _, suffix = source.partition("+")
    if source == "added":
        return next((r for ks, sfx, rev, r in rows if kp in r or "K120" in ks), "")
    cand = [(rev, r) for ks, sfx, rev, r in rows if base in ks and bool(sfx) == bool(suffix)
            and (not suffix or suffix in ("L", "F") and sfx == suffix or suffix not in ("L", "F"))]
    cand.sort(key=lambda c: c[0])            # prefer implementation rows over review notes
    return cand[0][1] if cand else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kp-prep", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--categories", type=Path, help="COCO json whose categories define the gold slots "
                                                     "(copied to static/categories.json for exports)")
    args = ap.parse_args()
    tax = args.kp_prep / "taxonomy"

    os.environ.setdefault("KP_FINAL_VOCAB", str(tax / "final_vocab_v1.json"))
    sys.path.insert(0, str(args.kp_prep))
    import keypoint_schema as KS  # v1-pruned schema

    groups_meta, method_text = load_taxonomy_helpers(tax / "build_taxonomy_sheet.py")
    final = json.load(open(tax / "final_vocab_v1.json"))
    vocab = final["vocab"]

    rule_rows = rule_index(tax / "kp_rule_changes_v1.csv")
    comments = {}
    with open(tax / "kp_final_v1.csv") as f:
        for r in csv.DictReader(f):
            comments[r["ID"]] = (r.get("resident_comment") or "").strip()

    groups: dict = {"frontal": OrderedDict(), "lateral": OrderedDict()}
    defs = {}
    order = {(v, c, k): i for v in vocab for i, (c, k) in
             enumerate((c, k) for c, kps in vocab[v].items() for k in kps)}
    for p in sorted(final["provenance"], key=lambda p: order.get((p["view"], p["class"], p["kp"]), 1e9)):
        view, cls, kp = p["view"], p["class"], p["kp"]
        gid = p["group"] or FALLBACK_GROUP
        name, region, desc = groups_meta.get(gid, (gid, "", ""))
        g = groups[view].setdefault(gid, {"name": name, "region": region, "description": desc, "keys": []})
        g["keys"].append(f"{cls}|{kp}")
        schema = KS.SCHEMA_LATERAL if view == "lateral" else KS.SCHEMA_FRONTAL
        kpdef = next((k for k in schema[cls].keypoints if k.name == kp), None) if cls in schema else None
        method = readable_method(kpdef, method_text)
        defs[f"{view}|{cls}|{kp}"] = {
            "label": f"{cls} – {pretty(kp)}",
            "rule": rule_for(p["source"], kp, rule_rows),
            "comment": comments.get(p["source"].partition("+")[0], ""),
            "method": method,
            "source": p["source"],
            # partner structures the rule reads (e.g. the visible lung for the costophrenic vertex)
            "via": [v for v in (kpdef.via if kpdef else ()) if v != cls],
        }
    for view in groups:
        groups[view] = OrderedDict(sorted(groups[view].items()))

    edges = {}
    for view, schema in (("frontal", KS.SCHEMA_FRONTAL), ("lateral", KS.SCHEMA_LATERAL)):
        keys = {k for g in groups[view].values() for k in g["keys"]}
        edges[view] = [[f"{c}|{a}", f"{c}|{b}"] for c, s in schema.items() for a, b in s.skeleton
                       if f"{c}|{a}" in keys and f"{c}|{b}" in keys]

    args.out.mkdir(parents=True, exist_ok=True)
    json.dump({"version": final["version"], "groups": groups, "edges": edges},
              open(args.out / "vocab.json", "w"), indent=1)
    json.dump(defs, open(args.out / "definitions.json", "w"), indent=1)
    if args.categories:
        json.dump(json.load(open(args.categories))["categories"], open(args.out / "categories.json", "w"))

    for view in groups:
        n = sum(len(g["keys"]) for g in groups[view].values())
        sizes = {gid: len(g["keys"]) for gid, g in groups[view].items()}
        print(f"{view}: {n} landmarks in {len(sizes)} groups {sizes}")
    missing = [k for k, d in defs.items() if not d["rule"] and not d["method"]]
    print(f"landmarks without rule or method text: {len(missing)}")
    print(f"with an explicit v1 rule: {sum(bool(d['rule']) for d in defs.values())}/{len(defs)}; edges "
          f"frontal {len(edges['frontal'])}, lateral {len(edges['lateral'])}")


if __name__ == "__main__":
    main()
