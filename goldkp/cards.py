"""Cards: one image x one landmark group (series groups split into level batches), plus the
per-reader reading order.

A card carries its landmark keys ("class|kp"), short labels, and a crop box in the 512 frame
derived from the predicted masks of the card's classes -- the same framing whether or not the
image is pre-filled, so the anchoring comparison is not confounded by the view.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path

FULL_VIEW_GROUPS = {"G01", "G05", "G21", "G26"}
MAX_BOX_FRAC = 0.55
PAD_MIN, PAD_FRAC, MIN_SIDE = 16, 0.15, 96
IMG = 512
# reading priority: clinical groups first, then the long series, then the abstract groups
PRIORITY_LAST = ["G19", "G20", "G21", "G25", "G26"]


def _vert_level(cls: str) -> tuple[str, int] | None:
    m = re.search(r"vertebrae ([TLC])(\d+)", cls)
    return (m.group(1), int(m.group(2))) if m else None


def series_batch(gid: str, view: str, cls: str) -> str:
    """Batch label within a series group ('' for non-series groups)."""
    if gid == "G19":
        lvl = _vert_level(cls)
        if lvl is None:
            return ""
        kind, n = lvl
        if kind == "L":
            return "L1-L2"
        return "T1-T4" if n <= 4 else "T5-T8" if n <= 8 else "T9-T12"
    if gid == "G20" and view == "frontal":
        m = re.search(r"(posterior|anterior) (\d+)\w* rib (left|right)", cls)
        if not m:
            return ""
        part, n, side = m.group(1), int(m.group(2)), m.group(3)
        if part == "anterior":
            return f"anterior {side}"
        return f"posterior {side} {'1-6' if n <= 6 else '7-12'}"
    return ""


def short_label(cls: str, kp: str) -> str:
    lvl = _vert_level(cls)
    if lvl:
        return f"{lvl[0]}{lvl[1]} {kp.replace('_', ' ')}"
    m = re.search(r"(posterior|anterior) (\d+)\w* rib (left|right)", cls)
    if m:
        return f"{m.group(1)[:4]}. rib {m.group(2)}{m.group(3)[0].upper()} {kp.replace('_', ' ')}"
    return f"{cls} – {kp.replace('_', ' ')}"


def crop_box(masks: dict, classes: list[str], gid: str) -> tuple[list[float], bool]:
    """Crop box [x0, y0, x1, y1] in the 512 frame and whether the card is shown full-view."""
    boxes = [masks[c]["bbox"] for c in classes if c in masks]
    if gid in FULL_VIEW_GROUPS or not boxes:
        return [0, 0, IMG, IMG], True
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    pad = max(PAD_MIN, PAD_FRAC * max(x1 - x0, y1 - y0))
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    side = min(max(x1 - x0, y1 - y0, MIN_SIDE), IMG)
    # a structure at the image border shifts the square inward instead of cutting it into a strip
    x0 = min(max(0, (x0 + x1) / 2 - side / 2), IMG - side)
    y0 = min(max(0, (y0 + y1) / 2 - side / 2), IMG - side)
    x1, y1 = x0 + side, y0 + side
    if (x1 - x0) * (y1 - y0) > MAX_BOX_FRAC * IMG * IMG:
        return [0, 0, IMG, IMG], True
    return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)], False


def build_cards(images: list[dict], vocab: dict, prefill: dict) -> list[dict]:
    """images: [{dicom_id, view, image (path), no_prefill}]; prefill: dicom_id -> prefill record."""
    cards = []
    for img_idx, im in enumerate(images):
        view = im["view"]
        masks = prefill.get(im["dicom_id"], {}).get("masks", {})
        for gid, g in vocab["groups"][view].items():
            batches: dict[str, list[str]] = {}
            for key in g["keys"]:
                cls = key.split("|", 1)[0]
                batches.setdefault(series_batch(gid, view, cls), []).append(key)
            for batch, keys in batches.items():
                classes = list(dict.fromkeys(k.split("|", 1)[0] for k in keys))
                box, full = crop_box(masks, classes, gid)
                cards.append({
                    "id": f"{im['dicom_id'][:8]}|{view}|{gid}|{batch}",
                    "img": img_idx, "dicom_id": im["dicom_id"], "view": view, "gid": gid, "batch": batch,
                    "keys": keys, "labels": [short_label(*k.split("|", 1)) for k in keys],
                    "classes": classes, "box": box, "full": full,
                    "mode": "scratch" if im.get("no_prefill") else "prefill",
                })
    return cards


def cards_sha(cards: list[dict]) -> str:
    return hashlib.sha256(json.dumps(cards, sort_keys=True).encode()).hexdigest()[:16]


def _group_major(cards: list[dict], subset: set[int] | None, rng: random.Random,
                 groups: list[str] | None = None) -> list[int]:
    gids = sorted({c["gid"] for c in cards}, key=lambda g: (g in PRIORITY_LAST, PRIORITY_LAST.index(g)
                                                           if g in PRIORITY_LAST else 0, g))
    if groups:
        gids = [g for g in dict.fromkeys(groups) if g in gids] + [g for g in gids if g not in groups]
    order = []
    for gid in gids:
        for view in ("frontal", "lateral"):
            idx = [i for i, c in enumerate(cards) if c["gid"] == gid and c["view"] == view
                   and (subset is None or c["img"] in subset)]
            imgs = sorted({cards[i]["img"] for i in idx})
            rng.shuffle(imgs)
            for im in imgs:
                order += [i for i in idx if cards[i]["img"] == im]
    return order


def _image_order(cards: list[dict]) -> list[int]:
    """Reader-independent image order that interleaves the views by fractional rank."""
    rng = random.Random(int(hashlib.sha256(b"image-blocks").hexdigest()[:8], 16))
    views = {c["img"]: c["view"] for c in cards}
    keyed = []
    for view in sorted(set(views.values())):
        imgs = sorted(i for i, v in views.items() if v == view)
        rng.shuffle(imgs)
        keyed += [((k + 0.5) / len(imgs), view, im) for k, im in enumerate(imgs)]
    return [im for _, _, im in sorted(keyed)]


def image_blocks(cards: list[dict], sizes: list[int]) -> list[list[int]]:
    """Split the images into blocks: the first of sizes[0] images, ..., then blocks of sizes[-1].
    The image order is the same for every reader (so their finished blocks overlap) and
    interleaves the views, so each block keeps the frontal/lateral mix of the study."""
    if not sizes or min(sizes) < 1:
        raise ValueError("image block sizes must be positive")
    imgs = _image_order(cards)
    blocks, n = [], 0
    while imgs:
        size = sizes[min(n, len(sizes) - 1)]
        blocks.append(imgs[:size])
        imgs, n = imgs[size:], n + 1
    return blocks


def shared_subset(cards: list[dict], sizes: dict[str, int]) -> set[int]:
    """The first sizes[view] images of each view in the reader-independent image order, so every
    reader reads the same images first (e.g. {"frontal": 30, "lateral": 20})."""
    views = {c["img"]: c["view"] for c in cards}
    out, n = set(), dict.fromkeys(sizes, 0)
    for im in _image_order(cards):
        v = views[im]
        if n.get(v, 0) < sizes.get(v, 0):
            out.add(im)
            n[v] += 1
    return out


def reader_order(cards: list[dict], rid: str, blocks: list[int] | None = None,
                 subset: set[int] | None = None, groups: list[str] | None = None) -> list[int]:
    """Group-major order; frontal block before lateral; images shuffled per reader; batches of an
    image stay consecutive in anatomical order. groups puts these groups first, in this order.
    With subset (see shared_subset) every group is read on the subset images first, then all
    groups again on the remaining images. With blocks (e.g. [10, 5]) the images are read in
    image blocks (see image_blocks), each finished in full, in that order, before the next."""
    rng = random.Random(int(hashlib.sha256(rid.encode()).hexdigest()[:8], 16))
    unknown = set(groups or []) - {c["gid"] for c in cards}
    if unknown:
        raise ValueError(f"unknown groups: {', '.join(sorted(unknown))}")
    if blocks:
        return [i for b in image_blocks(cards, blocks) for i in _group_major(cards, set(b), rng, groups)]
    if subset:
        rest = {c["img"] for c in cards} - subset
        return _group_major(cards, subset, rng, groups) + _group_major(cards, rest, rng, groups)
    return _group_major(cards, None, rng, groups)


def load_cards(path: Path) -> tuple[list[dict], str]:
    d = json.load(open(path))
    return d["cards"], d["sha"]


MAX_PARTNERS = 6


def rule_partners(keys: list[str], defs: dict, view: str) -> list[str]:
    """Structures the rules of these landmarks read besides their own (e.g. the visible lung for
    the costophrenic vertex). Rules that scan a whole series (the rib that crosses the thoracic
    margin) would list every rib; above MAX_PARTNERS the ribs are left out."""
    owners = {k.split("|", 1)[0] for k in keys}
    out = []
    for k in keys:
        for v in defs.get(f"{view}|{k}", {}).get("via", []):
            if v not in owners and v not in out:
                out.append(v)
    if len(out) > MAX_PARTNERS:
        out = [v for v in out if "rib" not in v]
    return out[:MAX_PARTNERS]
