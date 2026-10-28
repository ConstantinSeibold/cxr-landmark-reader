"""Reader app server (local only).

Readers correct pre-filled landmarks card by card; every submit is appended to the reader's own
log. Images are addressed by an opaque index, never by path, and the page never shows image
identifiers. Adjudication mode serves the queue produced by aggregate.py.

usage: python -m goldkp.server --data DATA_DIR --images IMAGE_ROOT [--readers r1,r2]
                               [--mode read|adjudicate] [--port 8765]
"""
from __future__ import annotations

import argparse
import functools
import io
import json
import time
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image

from .cards import reader_order, rule_partners
from .export import export_reader
from .store import ReaderStore, write_json_atomic

WEB = Path(__file__).resolve().parent / "web"
APP_VERSION = "0.1"
STATUSES = {"ok", "moved", "added", "not_visible"}
MASK_COLOURS = ["#00e5ff", "#7CFC00", "#ff6ec7", "#ffffff", "#c0a0ff", "#40ffa0"]   # own structures
PARTNER_COLOURS = ["#ffb000", "#ff7a00", "#ffd84d", "#ff9e7a", "#e0c060", "#ffaa55"]  # structures the rules read
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self'; connect-src 'self'")


def _decode_rle(rle: dict) -> np.ndarray:
    from pycocotools import mask as M
    r = dict(rle)
    if isinstance(r["counts"], str):
        r["counts"] = r["counts"].encode()
    return M.decode(r).astype(bool)


def create_app(data: Path, images_root: Path, readers: list[str], mode: str = "read",
               allow_version_change: bool = False) -> FastAPI:
    cards_json = json.load(open(data / "cards.json"))
    cards, sha = cards_json["cards"], cards_json["sha"]
    images = cards_json["images"]
    prefill_version = cards_json["prefill_version"]
    vocab = json.load(open(data / "static/vocab.json"))
    defs = json.load(open(data / "static/definitions.json"))
    refs = json.load(open(data / "refs/refs_manifest.json"))
    prefill_dir = data / "prefill" / prefill_version
    stores = {r: ReaderStore(data / "readers", r, sha, prefill_version, allow_version_change) for r in readers}
    orders = {r: reader_order(cards, r) for r in readers}
    ref_files = {f for g in refs["files"].values() for v in g.values() for f in v.values()}

    @functools.lru_cache(maxsize=16)
    def prefill(img_idx: int) -> dict:
        return json.load(open(prefill_dir / f"{images[img_idx]['dicom_id']}.json"))

    @functools.lru_cache(maxsize=256)
    def contours(img_idx: int, cls: str) -> list:
        """Outline polylines of a predicted mask in the 512 frame (simplified, for crisp drawing)."""
        import cv2
        m = prefill(img_idx).get("masks", {}).get(cls)
        if m is None:
            return []
        cs, _ = cv2.findContours(_decode_rle(m["rle"]).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        out = []
        for c in cs:
            if len(c) < 6:
                continue
            c = cv2.approxPolyDP(c, 0.6, True)[:, 0, :].astype(float) + 0.5
            out.append([[round(x, 1), round(y, 1)] for x, y in c])
        return out

    def outline_classes(c: dict) -> list[dict]:
        masks = prefill(c["img"]).get("masks", {})
        own = [{"name": n, "role": "structure", "colour": MASK_COLOURS[i % len(MASK_COLOURS)]}
               for i, n in enumerate(cl for cl in c["classes"] if cl in masks)]
        partners = [{"name": n, "role": "rule", "colour": PARTNER_COLOURS[i % len(PARTNER_COLOURS)]}
                    for i, n in enumerate(p for p in rule_partners(c["keys"], defs, c["view"]) if p in masks)]
        return own + partners

    @functools.lru_cache(maxsize=64)
    def jpeg(img_idx: int) -> bytes:
        p = images_root / images[img_idx]["image"]
        buf = io.BytesIO()
        Image.open(p).convert("L").save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    app = FastAPI(title="cxr-landmark-reader", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    def store_of(rid: str) -> ReaderStore:
        if rid not in stores:
            raise HTTPException(404, "unknown reader")
        return stores[rid]

    def card_at(rid: str, pos: int) -> dict:
        order = orders[rid]
        if not 0 <= pos < len(order):
            raise HTTPException(404, "no such card")
        return cards[order[pos]]

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB / "index.html").read_text()

    @app.get("/static/{name}")
    def static(name: str):
        if name not in {"app.js", "app.css"}:
            raise HTTPException(404)
        return FileResponse(WEB / name, media_type="text/javascript" if name.endswith(".js") else "text/css")

    @app.get("/api/config")
    def config():
        return {"readers": readers, "mode": mode, "app_version": APP_VERSION}

    @app.get("/api/r/{rid}/session")
    def session(rid: str):
        st = store_of(rid)
        per_group: dict = {}
        for i in orders[rid]:
            c = cards[i]
            g = per_group.setdefault(c["gid"], {"total": 0, "done": 0})
            g["total"] += 1
            g["done"] += st.latest(c["id"]) is not None
        first_open = next((p for p, i in enumerate(orders[rid]) if st.latest(cards[i]["id"]) is None),
                          len(cards))
        return {"total": len(cards), "done": st.done, "cursor": first_open, "per_group": per_group,
                "cards_sha": sha, "prefill_version": prefill_version}

    @app.get("/api/r/{rid}/cards")
    def card_index(rid: str):
        """Compact list of this reader's cards for the jump menu (no image identifiers)."""
        st = store_of(rid)
        out = []
        for pos, i in enumerate(orders[rid]):
            c = cards[i]
            out.append({"pos": pos, "gid": c["gid"], "view": c["view"], "batch": c["batch"], "mode": c["mode"],
                        "done": st.latest(c["id"]) is not None})
        names = {v: {g: meta["name"] for g, meta in vocab["groups"][v].items()} for v in vocab["groups"]}
        return {"cards": out, "group_names": names}

    @app.get("/api/r/{rid}/card/{pos}")
    def card(rid: str, pos: int):
        st = store_of(rid)
        c = card_at(rid, pos)
        pf = prefill(c["img"])
        mine = st.latest(c["id"])
        view = c["view"]
        pts = []
        for key, label in zip(c["keys"], c["labels"]):
            cls, kp = key.split("|", 1)
            p = pf["points"].get(cls, {}).get(kp) if c["mode"] == "prefill" else None
            pre = [p[0], p[1]] if p and p[2] > 0 else None
            d = defs.get(f"{view}|{key}", {})
            entry = {"key": key, "label": label, "colour": refs["colours"].get(f"{view}|{key}", "#ffffff"),
                     "reader": d.get("reader", ""), "rule": d.get("rule", ""), "method": d.get("method", ""),
                     "note": d.get("comment", ""),
                     "prefill": pre,
                     "xy": pre, "status": "ok" if pre else "missing", "unsure": False}
            if mine:
                rec = next((q for q in mine["points"] if q["key"] == key), None)
                if rec:
                    entry.update(xy=rec["xy"], status=rec["status"], unsure=rec["unsure"])
            pts.append(entry)
        g = vocab["groups"][view][c["gid"]]
        keyset = set(c["keys"])
        ref = refs["files"].get(c["gid"], {}).get(view, {})
        return {
            "pos": pos, "total": len(cards), "card_id": c["id"], "img": c["img"], "view": view,
            "gid": c["gid"], "group": {"name": g["name"], "region": g["region"], "description": g["description"]},
            "batch": c["batch"], "box": c["box"], "full": c["full"], "mode": c["mode"],
            "orig_wh": pf.get("orig_wh"), "points": pts,
            "edges": [e for e in vocab["edges"][view] if e[0] in keyset and e[1] in keyset],
            "refs": {k: f"/api/ref/{v}" for k, v in ref.items()},
            "outlines": outline_classes(c),
            "flag": mine.get("card_flag") if mine else None, "comment": mine.get("comment", "") if mine else "",
            "done": mine is not None,
        }

    @app.post("/api/r/{rid}/card/{pos}/submit")
    def submit(rid: str, pos: int, body: dict = Body(...)):
        st = store_of(rid)
        c = card_at(rid, pos)
        got = {p.get("key"): p for p in body.get("points", [])}
        if set(got) != set(c["keys"]):
            raise HTTPException(422, "points must cover exactly the card's landmarks")
        pf = prefill(c["img"])
        out = []
        for key in c["keys"]:
            p = got[key]
            status = p.get("status")
            if status not in STATUSES:
                raise HTTPException(422, f"unresolved or invalid status for {key}")
            xy = p.get("xy")
            if status == "not_visible":
                xy = None
            elif not (isinstance(xy, list) and len(xy) == 2 and all(isinstance(v, (int, float)) for v in xy)
                      and 0 <= xy[0] < 512 and 0 <= xy[1] < 512):
                raise HTTPException(422, f"coordinates of {key} outside the image")
            cls, kp = key.split("|", 1)
            pre = pf["points"].get(cls, {}).get(kp)
            out.append({"key": key, "status": status, "unsure": bool(p.get("unsure")), "xy": xy,
                        "prefill": pre if c["mode"] == "prefill" else None,
                        "n_edits": int(p.get("n_edits", 0))})
        rec = {"ts": time.time(), "rid": rid, "pos": pos, "card_id": c["id"], "dicom_id": c["dicom_id"],
               "view": c["view"], "gid": c["gid"], "batch": c["batch"], "mode": c["mode"],
               "prefill_version": prefill_version, "cards_sha": sha, "app_version": APP_VERSION,
               "points": out, "card_flag": body.get("flag"), "comment": str(body.get("comment", ""))[:2000],
               "active_ms": int(body.get("active_ms", 0)), "n_events": int(body.get("n_events", 0))}
        st.append(rec)
        return {"ok": True, "next": min(pos + 1, len(cards) - 1), "done": st.done}

    @app.post("/api/r/{rid}/export")
    def export(rid: str):
        """Snapshot of this reader's annotations so far, written on the server (never downloaded)."""
        store_of(rid)
        r = export_reader(data, rid)
        r["dir"] = str(Path(r["dir"]).relative_to(data))
        return r

    @app.get("/api/img/{idx}")
    def img(idx: int):
        if not 0 <= idx < len(images):
            raise HTTPException(404)
        return Response(jpeg(idx), media_type="image/jpeg")

    @app.get("/api/r/{rid}/card/{pos}/outlines")
    def outlines(rid: str, pos: int):
        store_of(rid)
        c = card_at(rid, pos)
        return [{**o, "polys": contours(c["img"], o["name"])} for o in outline_classes(c)]

    @app.get("/api/ref/{name}")
    def ref(name: str):
        if name not in ref_files:
            raise HTTPException(404)
        return FileResponse(data / "refs" / name, media_type="image/png")

    # ---------------- adjudication ----------------
    queue_path = data / "out" / "adjudication_queue.json"
    adj_dir = data / "adjudication"

    def adj_queue() -> list:
        if mode != "adjudicate":
            raise HTTPException(404)
        return json.load(open(queue_path)) if queue_path.exists() else []

    def adj_state() -> dict:
        p = adj_dir / "state.json"
        return json.load(open(p)) if p.exists() else {}

    @app.get("/api/adj/session")
    def adj_session():
        q = adj_queue()
        st = adj_state()
        return {"total": len(q), "done": sum(1 for it in q if it["item_id"] in st),
                "cursor": next((i for i, it in enumerate(q) if it["item_id"] not in st), len(q))}

    @app.get("/api/adj/item/{i}")
    def adj_item(i: int):
        q = adj_queue()
        if not 0 <= i < len(q):
            raise HTTPException(404)
        it = q[i]
        c = next(cc for cc in cards if cc["id"] == it["card_id"])
        view = c["view"]
        keys = []
        for k in it["keys"]:
            d = defs.get(f"{view}|{k['key']}", {})
            keys.append({**k, "label": c["labels"][c["keys"].index(k["key"])],
                         "colour": refs["colours"].get(f"{view}|{k['key']}", "#ffffff"),
                         "reader": d.get("reader", ""), "rule": d.get("rule", ""), "method": d.get("method", "")})
        ref = refs["files"].get(c["gid"], {}).get(view, {})
        g = vocab["groups"][view][c["gid"]]
        return {"pos": i, "total": len(q), "item_id": it["item_id"], "img": c["img"], "view": view,
                "gid": c["gid"], "group": {"name": g["name"], "region": g["region"], "description": g["description"]},
                "batch": c["batch"], "box": c["box"], "full": c["full"], "orig_wh": prefill(c["img"]).get("orig_wh"),
                "keys": keys, "refs": {k: f"/api/ref/{v}" for k, v in ref.items()},
                "resolved": adj_state().get(it["item_id"])}

    @app.post("/api/adj/item/{i}/resolve")
    def adj_resolve(i: int, body: dict = Body(...)):
        q = adj_queue()
        if not 0 <= i < len(q):
            raise HTTPException(404)
        it = q[i]
        allowed = {k["key"] for k in it["keys"]}
        res = body.get("keys", {})
        if set(res) != allowed:
            raise HTTPException(422, "resolve every flagged landmark")
        for k, v in res.items():
            if v.get("choice") not in {"A", "B", "mean", "new", "not_visible"}:
                raise HTTPException(422, f"invalid choice for {k}")
            if v["choice"] == "new" and not (isinstance(v.get("xy"), list) and len(v["xy"]) == 2):
                raise HTTPException(422, f"new position missing for {k}")
        rec = {"ts": time.time(), "item_id": it["item_id"], "card_id": it["card_id"], "keys": res}
        adj_dir.mkdir(parents=True, exist_ok=True)
        with open(adj_dir / "decisions.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        st = adj_state()
        st[it["item_id"]] = rec
        write_json_atomic(adj_dir / "state.json", st)
        return {"ok": True, "next": min(i + 1, len(q) - 1)}

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--images", type=Path, required=True, help="root the image paths in cards.json are relative to")
    ap.add_argument("--readers", default="r1,r2")
    ap.add_argument("--mode", choices=["read", "adjudicate"], default="read")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow-version-change", action="store_true")
    args = ap.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("the reader app only binds to loopback; use an SSH tunnel for remote readers")
    import uvicorn
    app = create_app(args.data, args.images, [r.strip() for r in args.readers.split(",") if r.strip()],
                     args.mode, args.allow_version_change)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
