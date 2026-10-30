import json

import pytest

from goldkp.cards import build_cards, crop_box, image_blocks, reader_order, series_batch, shared_subset
from goldkp.store import ReaderStore, VersionMismatch
from tests.conftest import VOCAB


def test_cards_cover_vocabulary_once(study):
    data, _ = study
    cj = json.load(open(data / "cards.json"))
    for i, im in enumerate(cj["images"]):
        got = sorted(k for c in cj["cards"] if c["img"] == i for k in c["keys"])
        want = sorted(k for g in VOCAB["groups"][im["view"]].values() for k in g["keys"])
        assert got == want


def test_series_batches_and_modes(study):
    data, _ = study
    cards = json.load(open(data / "cards.json"))["cards"]
    g19 = [c for c in cards if c["gid"] == "G19" and c["img"] == 0]
    assert sorted(c["batch"] for c in g19) == ["T1-T4", "T5-T8"]
    assert [c["mode"] for c in cards if c["img"] == 2] == ["scratch"]  # lateral image flagged no_prefill
    assert series_batch("G20", "frontal", "posterior 8th rib left") == "posterior left 7-12"
    assert series_batch("G20", "frontal", "anterior 3rd rib right") == "anterior right"


def test_full_view_groups_and_crop():
    masks = {"heart": {"bbox": [150, 220, 370, 380]}, "vertebrae T3": {"bbox": [240, 110, 270, 135]}}
    assert crop_box(masks, ["heart"], "G01") == ([0, 0, 512, 512], True)
    box, full = crop_box(masks, ["vertebrae T3"], "G19")
    assert not full and box[2] - box[0] >= 96 and box[0] <= 240 and box[2] >= 270


def test_crop_at_the_image_border_stays_square():
    masks = {"vertebrae L1": {"bbox": [235, 490, 275, 512]}}
    (x0, y0, x1, y1), full = crop_box(masks, ["vertebrae L1"], "G19")
    assert not full and x1 - x0 == y1 - y0 >= 96 and y1 == 512 and y0 <= 490


def test_reader_order_is_a_permutation_and_differs():
    images = [{"dicom_id": f"d{i}", "view": "frontal", "no_prefill": False} for i in range(12)]
    cards = build_cards(images, VOCAB, {})
    o1, o2 = reader_order(cards, "r1"), reader_order(cards, "r2")
    assert sorted(o1) == sorted(o2) == list(range(len(cards)))
    assert o1 != o2
    assert reader_order(cards, "r1") == o1                      # deterministic
    gids = [cards[i]["gid"] for i in o1]
    assert gids.index("G19") > max(i for i, g in enumerate(gids) if g == "G01")  # clinical groups first



def test_reader_order_in_image_blocks():
    images = [{"dicom_id": f"d{i}", "view": "frontal" if i < 14 else "lateral", "no_prefill": False}
              for i in range(20)]
    cards = build_cards(images, VOCAB, {})
    blocks = image_blocks(cards, [10, 5])
    assert [len(b) for b in blocks] == [10, 5, 5]
    assert sorted(i for b in blocks for i in b) == list(range(20))
    assert sum(images[i]["view"] == "lateral" for i in blocks[0]) == 3      # keeps the view mix
    o1, o2 = reader_order(cards, "r1", [10, 5]), reader_order(cards, "r2", [10, 5])
    assert sorted(o1) == list(range(len(cards))) and o1 != o2
    for o in (o1, o2):                                  # every block finished before the next
        seen = [blocks.index(next(b for b in blocks if cards[i]["img"] in b)) for i in o]
        assert seen == sorted(seen)
    assert reader_order(cards, "r1", None) == reader_order(cards, "r1")   # default unchanged


def test_reader_order_on_shared_subset():
    images = [{"dicom_id": f"d{i}", "view": "frontal" if i < 14 else "lateral", "no_prefill": False}
              for i in range(20)]
    cards = build_cards(images, VOCAB, {})
    sub = shared_subset(cards, {"frontal": 5, "lateral": 2})
    assert sum(images[i]["view"] == "frontal" for i in sub) == 5 and len(sub) == 7
    groups = ["G19", "G01"]
    o1, o2 = (reader_order(cards, r, subset=sub, groups=groups) for r in ("r1", "r2"))
    assert sorted(o1) == list(range(len(cards))) and o1 != o2
    for o in (o1, o2):
        n = sum(cards[i]["img"] in sub for i in o)
        assert all(cards[i]["img"] in sub for i in o[:n])                 # shared images first
        gids = [cards[i]["gid"] for i in o[:n]]
        assert gids[0] == "G19" and gids.index("G01") > max(k for k, g in enumerate(gids) if g == "G19")
    first = lambda o: [cards[i]["img"] for i in o if cards[i]["gid"] == "G01"][:5]
    assert set(first(o1)) == set(first(o2)) and first(o1) != first(o2)    # same images, own order
    assert reader_order(cards, "r1", None, None, None) == reader_order(cards, "r1")

def test_store_rebuild_and_latest_wins(tmp_path):
    st = ReaderStore(tmp_path, "r1", "sha", "v")
    for n in range(3):
        st.append({"card_id": "c1", "pos": 0, "points": [], "n": n, "cards_sha": "sha", "prefill_version": "v"})
    st.append({"card_id": "c2", "pos": 1, "points": [], "n": 9, "cards_sha": "sha", "prefill_version": "v"})
    (tmp_path / "r1" / "state.json").unlink()
    st2 = ReaderStore(tmp_path, "r1", "sha", "v")
    assert st2.latest("c1")["n"] == 2 and st2.done == 2 and st2.state["cursor"] == 2


def test_store_version_lock(tmp_path):
    st = ReaderStore(tmp_path, "r1", "sha", "v")
    st.append({"card_id": "c1", "pos": 0, "points": [], "cards_sha": "sha", "prefill_version": "v"})
    with pytest.raises(VersionMismatch):
        ReaderStore(tmp_path, "r1", "other", "v")
    ReaderStore(tmp_path, "r1", "other", "v", allow_version_change=True)
