"""One self-contained zip per reader: app code, study (with images), start scripts, instructions.

The reader unzips it, runs start.sh / start.bat and reads in the browser; the reader id is fixed in
the start script, so it cannot be mixed up. Reader logs, exports and outputs of the source study are
left out, as are pre-fill versions the cards do not use.

The package CONTAINS THE IMAGES: for credentialed data every reader must hold their own credentials
and data use agreement, and the zip must only travel through channels covered by that agreement.

usage: make_reader_package.py --data DATA --images IMAGE_ROOT --readers r1,r2 --out-dir OUT [--port 8765]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUDY_SKIP = {"readers", "exports", "out", "adjudication"}
READER_REQUIREMENTS = """fastapi>=0.110
uvicorn>=0.29
numpy>=1.24
pillow>=10.0
pycocotools>=2.0.7
opencv-python-headless>=4.8
"""

START_SH = """#!/usr/bin/env bash
# Landmark reader -- reader {rid}. Needs Python 3.10-3.14 (or uv) and internet access the first time.
set -e
cd "$(dirname "$0")"
if [ ! -f .venv/.installed ]; then
  echo "First start: installing the app (one-off, needs internet, about a minute)..."
  rm -rf .venv
  if command -v uv >/dev/null 2>&1; then
    uv venv -q --python 3.12 .venv || uv venv -q .venv
    uv pip install -q --python .venv/bin/python -r app/requirements.txt
  else
    PY=""
    for c in python3.12 python3.11 python3.10 python3.13 python3.14 python3; do
      if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) else 1)' 2>/dev/null; then
        PY="$c"; break
      fi
    done
    if [ -z "$PY" ]; then
      echo "No Python 3.10-3.14 found. Install Python 3.12 (https://www.python.org/downloads/) and run this script again."
      exit 1
    fi
    if ! "$PY" -m venv .venv >/dev/null 2>&1; then
      rm -rf .venv
      echo "Could not create a Python environment with $PY."
      echo "  Ubuntu/Debian: sudo apt install python3-venv   (or python3.X-venv for your Python version)"
      echo "  or install uv: https://docs.astral.sh/uv/getting-started/installation/  -- then run this script again."
      exit 1
    fi
    .venv/bin/python -m pip install -q --upgrade pip
    .venv/bin/python -m pip install -q -r app/requirements.txt
  fi
  touch .venv/.installed
fi
echo "Open http://127.0.0.1:{port} in Chrome or Edge. Stop with Ctrl+C."
PYTHONPATH=app .venv/bin/python -m goldkp.server --data study --images study/images --readers {rid} --port {port}
"""

START_BAT = """@echo off
rem Landmark reader -- reader {rid}. Needs Python 3.10-3.14 (or uv) and internet access the first time.
cd /d "%~dp0"
if exist .venv\\.installed goto run
echo First start: installing the app (one-off, needs internet, about a minute)...
if exist .venv rmdir /s /q .venv
where uv >nul 2>nul
if errorlevel 1 goto pyvenv
uv venv -q --python 3.12 .venv
uv pip install -q --python .venv\\Scripts\\python.exe -r app\\requirements.txt
if errorlevel 1 goto fail
goto installed
:pyvenv
py -3 -m venv .venv >nul 2>nul
if not exist .venv\\Scripts\\python.exe python -m venv .venv
if not exist .venv\\Scripts\\python.exe goto fail
.venv\\Scripts\\python -m pip install -q --upgrade pip
.venv\\Scripts\\python -m pip install -q -r app\\requirements.txt
if errorlevel 1 goto fail
:installed
echo ok> .venv\\.installed
:run
echo Open http://127.0.0.1:{port} in Chrome or Edge. Close this window to stop.
set PYTHONPATH=app
.venv\\Scripts\\python -m goldkp.server --data study --images study\\images --readers {rid} --port {port}
pause
exit /b 0
:fail
echo Could not install the app. Install Python 3.12 from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" during installation) and start this file again.
pause
exit /b 1
"""

README = """# Landmark reading -- reader {rid}

This folder contains the reading app, the {n_images} images and everything needed to read them.

**Data use.** The images come from {dataset}. You may only open them if you hold your own
credentials and have signed the data use agreement. Do not copy the folder to cloud storage, and
send results back only through the channel agreed with the study coordinator.

## Start
1. You need Python 3.10–3.14. If you do not have it, install Python 3.12 from python.org
   (Windows: tick "Add python.exe to PATH"). On Ubuntu/Debian you may also need
   `sudo apt install python3-venv`; the start script tells you if so.
2. Start the app:
   - macOS / Linux: open a terminal in this folder and run `bash start.sh`
   - Windows: double-click `start.bat`
   The first start installs the app (internet needed, about a minute); later starts are immediate.
3. Open <http://127.0.0.1:{port}> in Chrome or Edge and click **{rid}**.

Keep the terminal window open while reading; closing it stops the app. Every card is saved the
moment you submit it -- you can stop at any time and continue later where you left off.

## Reading
- One card = one image and one group of landmarks. Correct the points, then press **Enter**.
- Drag a point to move it; click to place a missing one; arrow keys nudge the selected point.
- **n** not visible, **u** unsure, **r** back to the suggestion, **f** structure not assessable,
  **Ctrl+Z** undo. **m** shows the outlines of the structures the landmark is defined on,
  **c** switches between the zoomed crop and the whole image. **?** lists every shortcut.
- The panel under the list gives the definition of the selected landmark; the image below it shows
  a reference example of the group.
- Some images have no suggested points ("place from scratch"): place each point in the order shown.
- Use **unsure** rather than guessing; use **not visible** when a landmark cannot be seen.

## Sending your results
Click **Export** (top right) and send the folder it names -- `study/exports/{rid}/<date-time>/`
-- to the study coordinator through the agreed channel. You can export at any time (for example once a
week as a backup); each export contains everything read so far.

## Problems
- "port already in use": another copy of the app is running; close it, or edit the port in the
  start script.
- Nothing happens on the first start: check the internet connection (the app needs to be installed once).
- Contact the study coordinator for anything else; mention the card number shown at the top.
"""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(data: Path, images: Path, rid: str, out_dir: Path, port: int, dataset: str) -> Path:
    cj = json.load(open(data / "cards.json"))
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / f"landmark_reader_{rid}"
        # app code: the runtime package only
        (pkg / "app").mkdir(parents=True)
        shutil.copytree(ROOT / "goldkp", pkg / "app" / "goldkp", ignore=shutil.ignore_patterns("__pycache__"))
        (pkg / "app" / "requirements.txt").write_text(READER_REQUIREMENTS)
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        # study without reader logs / exports / outputs; only the pre-fill version the cards use
        study = pkg / "study"
        study.mkdir()
        for item in data.iterdir():
            if item.name in STUDY_SKIP or item.name.endswith(".png"):
                continue
            if item.name == "prefill":
                shutil.copytree(item / cj["prefill_version"], study / "prefill" / cj["prefill_version"])
            elif item.is_dir():
                shutil.copytree(item, study / item.name)
            else:
                shutil.copy2(item, study / item.name)
        manifest = {"cards_sha": cj["sha"], "prefill_version": cj["prefill_version"], "images_included": True,
                    "app_commit": rev, "reader": rid, "images": []}
        for im in cj["images"]:
            src = images / im["image"]
            dst = study / "images" / im["image"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            manifest["images"].append({"path": im["image"], "sha256": sha256(src)})
        json.dump(manifest, open(study / "project_manifest.json", "w"), indent=1)
        # start scripts and instructions
        (pkg / "start.sh").write_text(START_SH.format(rid=rid, port=port))
        (pkg / "start.sh").chmod(0o755)
        (pkg / "start.bat").write_text(START_BAT.format(rid=rid, port=port).replace("\n", "\r\n"))
        (pkg / "README.md").write_text(README.format(rid=rid, port=port, n_images=len(cj["images"]), dataset=dataset))
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"landmark_reader_{rid}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(pkg.rglob("*")):
                if f.is_file():
                    info = zipfile.ZipInfo.from_file(f, f.relative_to(pkg.parent))
                    if f.suffix in (".jpg", ".png"):
                        info.compress_type = zipfile.ZIP_STORED
                        z.writestr(info, f.read_bytes())
                    else:
                        z.write(f, f.relative_to(pkg.parent), compress_type=zipfile.ZIP_DEFLATED)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--images", type=Path, required=True)
    ap.add_argument("--readers", default="r1,r2")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dataset", default="MIMIC-CXR (PhysioNet, credentialed access)")
    args = ap.parse_args()
    if "kp_prep/paper" in str(args.out_dir.resolve()):
        sys.exit("refusing to write into a folder that syncs to Overleaf")
    for rid in [r.strip() for r in args.readers.split(",") if r.strip()]:
        out = build(args.data, args.images, rid, args.out_dir, args.port, args.dataset)
        print(f"{rid}: {out} ({out.stat().st_size / 1e6:.1f} MB)")
    print("The packages contain the images: credentialed recipients only, agreed channels only.")


if __name__ == "__main__":
    main()
