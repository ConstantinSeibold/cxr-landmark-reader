"""Move studies between machines and bring readers' work back.

  pack           study folder -> one zip; manifest with the SHA-256 of every image. Images are left
                 out unless --with-images (then the bundle runs on its own)
  unpack         zip -> study folder; verifies every image -- from the bundle, or under the local
                 image root -- is present and byte-identical to the one the study was built on
  verify         check a study folder: images (presence + checksum), versions, reader logs
  import-reader  merge a reader's records (an export folder, a decisions.jsonl or another study's
                 readers/<rid>) into a study: same card/pre-fill versions required, records already
                 present are skipped, the newest answer per card wins, optional renaming (--as)

A bundle holds image identifiers and landmark coordinates derived from the images: move it only
between machines covered by the data use agreement of the source data.

usage: python -m goldkp.project pack --data DATA --images ROOT --out study.zip [--with-images] [--without-readers]
       python -m goldkp.project unpack --zip study.zip --to DATA [--images ROOT]
       python -m goldkp.project verify --data DATA --images ROOT
       python -m goldkp.project import-reader --data DATA --source PATH --rid r1 [--as r1]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import zipfile
from pathlib import Path

from .store import ReaderStore

MANIFEST = "project_manifest.json"
NOTICE = ("This bundle contains image identifiers and landmark coordinates derived from the images. "
          "Move it only between machines covered by the source data's use agreement.")
NOTICE_IMAGES = ("This bundle CONTAINS THE IMAGES. For credentialed data (e.g. MIMIC-CXR) every recipient must "
                 "hold their own credentials and data use agreement; never put it on public or cloud storage.")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def image_checks(data: Path, images: Path, manifest: dict | None = None) -> dict:
    cj = json.load(open(data / "cards.json"))
    want = {i["path"]: i["sha256"] for i in manifest["images"]} if manifest else {}
    missing, changed = [], []
    for im in cj["images"]:
        p = images / im["image"]
        if not p.exists():
            missing.append(im["image"])
        elif want and sha256(p) != want.get(im["image"]):
            changed.append(im["image"])
    return {"n": len(cj["images"]), "missing": missing, "changed": changed}


def read_records(source: Path, rid: str) -> list[dict]:
    """Records from an export folder, a decisions.jsonl, a readers/<rid> folder or a study folder."""
    cands = [source] if source.is_file() else [source / "decisions.jsonl", source / "readers" / rid / "decisions.jsonl",
                                                source / rid / "decisions.jsonl"]
    path = next((c for c in cands if c.is_file()), None)
    if path is None:
        raise SystemExit(f"no decisions.jsonl for reader {rid} under {source}")
    recs = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                recs.append(json.loads(line))
            except ValueError:
                print(f"skipping an unreadable line in {path}", file=sys.stderr)
    return recs


def import_reader(data: Path, source: Path, rid: str, as_rid: str | None = None) -> dict:
    cj = json.load(open(data / "cards.json"))
    target = as_rid or rid
    recs = read_records(source, rid)
    bad = {(r.get("cards_sha"), r.get("prefill_version")) for r in recs} - {(cj["sha"], cj["prefill_version"])}
    if bad:
        raise SystemExit(f"version mismatch: records were made on {sorted(bad)}, this study is "
                         f"({cj['sha']}, {cj['prefill_version']}); refusing to mix")
    card_ids = {c["id"] for c in cj["cards"]}
    unknown = [r["card_id"] for r in recs if r["card_id"] not in card_ids]
    if unknown:
        raise SystemExit(f"{len(unknown)} records refer to cards not in this study")
    log = data / "readers" / target / "decisions.jsonl"
    existing = []
    if log.exists():
        existing = [json.loads(line) for line in open(log) if line.strip()]
    seen = {(r["card_id"], r.get("ts")) for r in existing}
    new = [dict(r, rid=target) for r in recs if (r["card_id"], r.get("ts")) not in seen]
    merged = sorted(existing + new, key=lambda r: r.get("ts", 0))     # replay order = time: newest answer wins
    log.parent.mkdir(parents=True, exist_ok=True)
    tmp = log.with_suffix(".jsonl.tmp")
    with open(tmp, "w") as f:
        for r in merged:
            f.write(json.dumps(r) + "\n")
    if log.exists():
        log.rename(log.with_name(f"decisions.before_import_{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
    tmp.rename(log)
    (log.parent / "state.json").unlink(missing_ok=True)               # rebuilt from the merged log
    st = ReaderStore(data / "readers", target, cj["sha"], cj["prefill_version"])
    from .store import write_json_atomic
    write_json_atomic(st.state_path, st.state)
    return {"reader": target, "records_in_source": len(recs), "added": len(new),
            "skipped_duplicates": len(recs) - len(new), "cards_done": st.done, "cards_total": len(cj["cards"])}


def study_files(data: Path, with_readers: bool) -> list[Path]:
    """What a study consists of -- nothing else is bundled (no exports, outputs, backups or stray files)."""
    cj = json.load(open(data / "cards.json"))
    roots = [data / "static", data / "refs", data / "prefill" / cj["prefill_version"]]
    if with_readers:
        roots.append(data / "readers")
    files = [data / "cards.json"]
    for r in roots:
        if r.exists():
            files += [f for f in sorted(r.rglob("*")) if f.is_file() and not f.name.endswith(".tmp")]
    return files


def pack(data: Path, images: Path, out: Path, with_images: bool = False, with_readers: bool = True) -> dict:
    cj = json.load(open(data / "cards.json"))
    imgs = []
    for im in cj["images"]:
        p = images / im["image"]
        if not p.exists():
            raise SystemExit(f"image missing: {im['image']} (pack needs the images to record checksums)")
        imgs.append({"path": im["image"], "sha256": sha256(p)})
    manifest = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "cards_sha": cj["sha"],
                "prefill_version": cj["prefill_version"], "images": imgs, "images_included": with_images,
                "readers_included": with_readers, "notice": NOTICE_IMAGES if with_images else NOTICE}
    files = study_files(data, with_readers)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps(manifest, indent=1))
        for f in files:
            z.write(f, str(f.relative_to(data)))
        if with_images:
            for im in imgs:                                    # stored as-is (JPEG/PNG do not compress)
                z.write(images / im["path"], f"images/{im['path']}", compress_type=zipfile.ZIP_STORED)
    return {"files": len(files), "images": len(imgs), "images_included": with_images,
            "readers_included": with_readers, "zip": str(out)}


def unpack(zpath: Path, to: Path, images: Path | None = None) -> dict:
    if to.exists() and any(to.iterdir()):
        raise SystemExit(f"{to} is not empty; unpack into a new folder")
    to.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if name.startswith("/") or ".." in Path(name).parts:
                raise SystemExit(f"unsafe path in bundle: {name}")
        z.extractall(to)
    manifest = json.load(open(to / MANIFEST))
    if images is None:
        if not manifest.get("images_included"):
            raise SystemExit("this bundle has no images: pass --images with the local image root")
        images = to / "images"
    chk = image_checks(to, images, manifest)
    return {"to": str(to), "images_root": str(images),
            **{k: (len(v) if isinstance(v, list) else v) for k, v in chk.items()}, "problems": chk}


def verify(data: Path, images: Path) -> dict:
    mpath = data / MANIFEST
    chk = image_checks(data, images, json.load(open(mpath)) if mpath.exists() else None)
    cj = json.load(open(data / "cards.json"))
    readers = {}
    for d in sorted((data / "readers").glob("*")) if (data / "readers").exists() else []:
        st = ReaderStore(data / "readers", d.name, cj["sha"], cj["prefill_version"])
        readers[d.name] = st.done
    return {"images": chk["n"], "missing": chk["missing"], "changed": chk["changed"],
            "checksums_checked": mpath.exists(), "readers_cards_done": readers, "cards_total": len(cj["cards"])}


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pack"); a.add_argument("--data", type=Path, required=True)
    a.add_argument("--images", type=Path, required=True); a.add_argument("--out", type=Path, required=True)
    a.add_argument("--with-images", action="store_true", help="include the image files (see the notice)")
    a.add_argument("--without-readers", action="store_true", help="leave the readers' answers out (data for new readers)")
    a = sub.add_parser("unpack"); a.add_argument("--zip", type=Path, required=True)
    a.add_argument("--to", type=Path, required=True)
    a.add_argument("--images", type=Path, help="local image root; default: the images inside the bundle")
    a = sub.add_parser("verify"); a.add_argument("--data", type=Path, required=True)
    a.add_argument("--images", type=Path, required=True)
    a = sub.add_parser("import-reader"); a.add_argument("--data", type=Path, required=True)
    a.add_argument("--source", type=Path, required=True); a.add_argument("--rid", required=True)
    a.add_argument("--as", dest="as_rid")
    args = ap.parse_args()
    if args.cmd == "pack":
        r = pack(args.data, args.images, args.out, args.with_images, not args.without_readers)
        what = "images" if r["images_included"] else "image checksums"
        print(f"packed {r['files']} files and {r['images']} {what} -> {r['zip']}\n"
              f"{NOTICE_IMAGES if r['images_included'] else NOTICE}")
    elif args.cmd == "unpack":
        r = unpack(args.zip, args.to, args.images)
        print(f"unpacked to {r['to']}: {r['n']} images, missing {r['missing']}, different {r['changed']}\n"
              f"start: python -m goldkp.server --data {r['to']} --images {r['images_root']}")
        if r["missing"] or r["changed"]:
            sys.exit(1)
    elif args.cmd == "verify":
        r = verify(args.data, args.images)
        print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in r.items()}, indent=1))
        if r["missing"] or r["changed"]:
            sys.exit(1)
    else:
        print(json.dumps(import_reader(args.data, args.source, args.rid, args.as_rid), indent=1))


if __name__ == "__main__":
    main()
