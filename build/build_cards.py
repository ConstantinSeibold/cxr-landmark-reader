"""Freeze the card list for a reading study.

usage: build_cards.py --selection selection.csv --prefill DATA/prefill/<version> --vocab DATA/static/vocab.json
                      --out DATA/cards.json
selection.csv needs columns dicom_id, path, image (frontal|lateral), no_prefill.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goldkp.cards import build_cards, cards_sha, reader_order  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", type=Path, required=True)
    ap.add_argument("--prefill", type=Path, required=True)
    ap.add_argument("--vocab", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    sel = pd.read_csv(args.selection)
    vocab = json.load(open(args.vocab))
    images = [{"dicom_id": r.dicom_id, "view": "lateral" if r.image == "lateral" else "frontal",
               "image": r.path, "no_prefill": bool(r.no_prefill)} for r in sel.itertuples()]
    prefill = {im["dicom_id"]: json.load(open(args.prefill / f"{im['dicom_id']}.json")) for im in images}
    cards = build_cards(images, vocab, prefill)

    # every image's cards must cover its view's vocabulary exactly once
    for i, im in enumerate(images):
        got = sorted(k for c in cards if c["img"] == i for k in c["keys"])
        want = sorted(k for g in vocab["groups"][im["view"]].values() for k in g["keys"])
        assert got == want, f"coverage mismatch on image {i}"
    sha = cards_sha(cards)
    json.dump({"sha": sha, "prefill_version": json.load(open(args.prefill / "manifest.json"))["version"],
               "images": images, "cards": cards}, open(args.out, "w"))
    per_view = Counter(c["view"] for c in cards)
    full = sum(c["full"] for c in cards)
    print(f"{len(cards)} cards ({dict(per_view)}), {full} full-view, "
          f"{sum(len(c['keys']) for c in cards)} points, sha {sha}")
    o1, o2 = reader_order(cards, "r1"), reader_order(cards, "r2")
    print(f"reader orders differ: {o1 != o2}; same set: {sorted(o1) == sorted(o2) == list(range(len(cards)))}")


if __name__ == "__main__":
    main()
