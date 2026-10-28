import json
import shutil

import pytest

from goldkp.export import export_reader
from goldkp.project import import_reader, pack, unpack, verify
from tests.test_server import answer


def submit_some(client, rid, n, shift=(0, 0)):
    for pos in range(n):
        card = client.get(f"/api/r/{rid}/card/{pos}").json()
        client.post(f"/api/r/{rid}/card/{pos}/submit", json=answer(card, shift=shift, status="moved"))


def test_pack_unpack_verify_and_tampering(study, client_factory, tmp_path):
    data, imgs = study
    submit_some(client_factory(data, imgs), "r1", 2)
    z = tmp_path / "study.zip"
    r = pack(data, imgs, z)
    assert r["images"] == 3
    out = unpack(z, tmp_path / "copy", imgs)
    assert out["missing"] == 0 and out["changed"] == 0
    v = verify(tmp_path / "copy", imgs)
    assert v["readers_cards_done"] == {"r1": 2} and v["checksums_checked"]
    # the copy is a working study
    c = client_factory(tmp_path / "copy", imgs)
    assert c.get("/api/r/r1/session").json()["done"] == 2
    # a different image under the same name is caught
    imgs2 = tmp_path / "imgs2"
    shutil.copytree(imgs, imgs2)
    (imgs2 / "img0000.png").write_bytes(b"not the same image")
    assert verify(tmp_path / "copy", imgs2)["changed"] == ["img0000.png"]
    with pytest.raises(SystemExit):
        unpack(z, tmp_path / "copy", imgs)                        # refuses a non-empty target


def test_import_reader_from_export_merges_and_renames(study, client_factory, tmp_path):
    data, imgs = study
    # a second copy of the study where 'pilot' reads 3 cards
    other = tmp_path / "other"
    shutil.copytree(data, other)
    c2 = client_factory(other, imgs, readers=("pilot",))
    submit_some(c2, "pilot", 3)
    snap = export_reader(other, "pilot")
    # import into the main study as r1, which already has card 0 from an older session
    c = client_factory(data, imgs)
    submit_some(c, "r1", 1, shift=(9, 9))
    res = import_reader(data, tmp_path / "other" / snap["dir"].split(str(other) + "/")[-1], "pilot", as_rid="r1")
    assert res["added"] == 3 and res["skipped_duplicates"] == 0
    st = json.load(open(data / "readers/r1/state.json"))
    assert len(st["latest"]) == 3 and all(r["rid"] == "r1" for r in st["latest"].values())
    # importing the same snapshot again adds nothing
    again = import_reader(data, tmp_path / "other" / snap["dir"].split(str(other) + "/")[-1], "pilot", as_rid="r1")
    assert again["added"] == 0 and again["skipped_duplicates"] == 3
    assert any(p.name.startswith("decisions.before_import_") for p in (data / "readers/r1").iterdir())


def test_import_refuses_other_versions(study, client_factory, tmp_path):
    data, imgs = study
    submit_some(client_factory(data, imgs), "r1", 1)
    log = data / "readers/r1/decisions.jsonl"
    rec = json.loads(log.read_text().splitlines()[0])
    rec["cards_sha"] = "different"
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps(rec) + "\n")
    with pytest.raises(SystemExit, match="version mismatch"):
        import_reader(data, bad, "r1")


def test_bundle_with_images_runs_on_its_own(study, client_factory, tmp_path):
    data, imgs = study
    submit_some(client_factory(data, imgs), "r1", 1)
    z = tmp_path / "with_images.zip"
    assert pack(data, imgs, z, with_images=True)["images_included"]
    out = unpack(z, tmp_path / "copy")                           # no --images: uses the bundled files
    assert out["missing"] == 0 and out["changed"] == 0 and out["images_root"].endswith("copy/images")
    c = client_factory(tmp_path / "copy", tmp_path / "copy" / "images")
    assert c.get("/api/img/0").status_code == 200 and c.get("/api/r/r1/session").json()["done"] == 1
    # an image-free bundle cannot be unpacked without an image root
    z2 = tmp_path / "no_images.zip"
    pack(data, imgs, z2)
    with pytest.raises(SystemExit, match="no images"):
        unpack(z2, tmp_path / "copy2")


def test_pack_contains_only_the_study(study, client_factory, tmp_path):
    import zipfile
    data, imgs = study
    submit_some(client_factory(data, imgs), "r1", 1)
    (data / "exports").mkdir()
    (data / "exports" / "x.csv").write_text("x")
    (data / "prefill" / "old_version").mkdir()
    (data / "prefill" / "old_version" / "a.json").write_text("{}")
    (data / "preview.png").write_bytes(b"x")
    z = tmp_path / "data.zip"
    pack(data, imgs, z, with_images=True, with_readers=False)
    names = zipfile.ZipFile(z).namelist()
    assert not any(n.startswith(("exports/", "readers/", "prefill/old_version")) or n == "preview.png" for n in names)
    assert {"cards.json", "project_manifest.json"} <= set(names) and sum(n.startswith("images/") for n in names) == 3
    out = unpack(z, tmp_path / "fresh")
    assert out["missing"] == 0 and out["changed"] == 0
    c = client_factory(tmp_path / "fresh", tmp_path / "fresh" / "images", readers=("r2",))
    assert c.get("/api/r/r2/session").json()["done"] == 0
