"""Regression tests for bugs found in review: wrong-card submits, torn logs, stale state,
loose validation, adjudication ids and import checks."""
import json

import pytest
from fastapi.testclient import TestClient

from goldkp.export import export_reader
from goldkp.project import import_reader, study_files
from goldkp.server import create_app
from tests.test_server import answer


def test_submit_for_another_card_is_refused(study, client_factory):
    """The reading order can change between loading and submitting (restart with other flags):
    an answer made for one card must never be stored on the card now at that position."""
    data, imgs = study
    c = client_factory(data, imgs)
    other = answer(c.get("/api/r/r1/card/1").json())
    assert c.post("/api/r/r1/card/0/submit", json=other).status_code == 409
    assert not (data / "readers/r1/decisions.jsonl").exists()


def test_torn_last_line_does_not_swallow_the_next_submit(study, client_factory):
    data, imgs = study
    c = client_factory(data, imgs)
    c.post("/api/r/r1/card/0/submit", json=answer(c.get("/api/r/r1/card/0").json()))
    with open(data / "readers/r1/decisions.jsonl", "a") as f:
        f.write('{"ts": 1, "card_id": "trunc')                       # crash mid-write
    card = c.get("/api/r/r1/card/1").json()
    assert c.post("/api/r/r1/card/1/submit", json=answer(card)).status_code == 200
    (data / "readers/r1/state.json").unlink()
    c2 = client_factory(data, imgs)
    assert c2.get("/api/r/r1/session").json()["done"] == 2


def test_export_after_state_lost(study, client_factory):
    data, imgs = study
    c = client_factory(data, imgs)
    c.post("/api/r/r1/card/0/submit", json=answer(c.get("/api/r/r1/card/0").json()))
    (data / "readers/r1/state.json").unlink()
    assert export_reader(data, "r1")["done"] == 1
    (data / "readers/r1/state.json").write_text("{corrupt")
    assert export_reader(data, "r1")["done"] == 1


def test_import_while_server_runs_keeps_imported_cards(study, client_factory, tmp_path):
    data, imgs = study
    c = client_factory(data, imgs)
    for pos in range(2):
        c.post("/api/r/r2/card/%d/submit" % pos, json=answer(c.get(f"/api/r/r2/card/{pos}").json()))
    ex = export_reader(data, "r2", tmp_path / "ex")
    import_reader(data, tmp_path / "ex" / "r2" / ex["dir"].rsplit("/", 1)[-1], "r2", as_rid="r1")
    c.post("/api/r/r1/card/3/submit", json=answer(c.get("/api/r/r1/card/3").json()))   # server still running
    assert export_reader(data, "r1")["done"] == 3


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(active_ms="abc"),
    lambda b: b.update(active_ms=-5),
    lambda b: b.update(points=None),
    lambda b: b["points"].append("x"),
    lambda b: b["points"][0].update(n_edits="x"),
    lambda b: b["points"][0].update(xy=[True, False], status="moved"),
    lambda b: b["points"].append(dict(b["points"][0])),
    lambda b: b.update(flag="whatever"),
])
def test_bad_submit_fields_are_422(study, client_factory, mutate):
    c = client_factory(*study)
    body = answer(c.get("/api/r/r1/card/0").json())
    mutate(body)
    assert c.post("/api/r/r1/card/0/submit", json=body).status_code == 422


def _adjudication(tmp_path, client_factory):
    import subprocess
    import sys
    from pathlib import Path

    import numpy as np

    from tests.conftest import TRUTH, make_study
    from tests.test_aggregate import read_all
    data, imgs = make_study(tmp_path, n_frontal=30, n_lateral=0, scratch_last=False)
    c = client_factory(data, imgs)
    rng = np.random.default_rng(1)
    card0 = json.load(open(data / "cards.json"))["cards"][0]["id"]
    read_all(c, "r1", 0.02, {k: 50.0 for k in TRUTH}, rng)
    read_all(c, "r2", 0.02, {k: 50.0 for k in TRUTH}, rng, outlier_card=card0)
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(root / "aggregate.py"), "--data", str(data), "--categories",
                    str(data / "categories.json")], check=True, capture_output=True, cwd=root)
    return client_factory(data, imgs, mode="adjudicate")


def test_resolve_validation(tmp_path, client_factory):
    adj = _adjudication(tmp_path, client_factory)
    item = adj.get("/api/adj/item/0").json()
    k = item["keys"][0]["key"]
    others = {q["key"]: {"choice": "A"} for q in item["keys"]}

    def post(**kw):
        return adj.post("/api/adj/item/0/resolve", json={"item_id": item["item_id"], "keys": {**others, k: kw}})
    assert post(choice="new", xy=["a", 9999]).status_code == 422
    assert post(choice="new", xy=[600, 10]).status_code == 422
    assert adj.post("/api/adj/item/0/resolve", json={"item_id": "other", "keys": others}).status_code == 409
    assert post(choice="new", xy=[10, 10]).status_code == 200


def test_adjudication_ids_depend_on_the_card_only():
    import pandas as pd

    import aggregate as A
    rows = [dict(card_id=c, key="k", reason="far", xy_a=[1, 1], xy_b=[2, 2], vis_a=True, vis_b=True)
            for c in ("c1", "c2", "c3")]
    q1, _ = A.adjudication_queue(pd.DataFrame(rows))
    q2, _ = A.adjudication_queue(pd.DataFrame(rows[1:]))                 # c1 settled after a revision
    ids1 = {it["card_id"]: (it["item_id"], it["keys"][0]["a"]) for it in q1}
    assert all(ids1[it["card_id"]] == (it["item_id"], it["keys"][0]["a"]) for it in q2)


def test_import_refuses_records_of_another_reader(study, client_factory, tmp_path):
    data, imgs = study
    c = client_factory(data, imgs)
    c.post("/api/r/r2/card/0/submit", json=answer(c.get("/api/r/r2/card/0").json()))
    log = data / "readers/r2/decisions.jsonl"
    with pytest.raises(SystemExit):
        import_reader(data, log, "r1")


def test_pack_leaves_out_import_backups(study, client_factory):
    data, imgs = study
    c = client_factory(data, imgs)
    c.post("/api/r/r1/card/0/submit", json=answer(c.get("/api/r/r1/card/0").json()))
    (data / "readers/r1/decisions.before_import_20260101-000000.jsonl").write_text("")
    assert not any("before_import" in f.name for f in study_files(data, True))


def test_replaced_log_of_the_same_size_is_noticed(study, client_factory):
    from goldkp.store import ReaderStore
    data, imgs = study
    c = client_factory(data, imgs)
    c.post("/api/r/r1/card/0/submit", json=answer(c.get("/api/r/r1/card/0").json()))
    log = data / "readers/r1/decisions.jsonl"
    rec = json.loads(log.read_text())
    st = ReaderStore(data / "readers", "r1", rec["cards_sha"], rec["prefill_version"])
    other = dict(rec, card_id=rec["card_id"][:-1] + "X")                  # same length, other card
    tmp = log.with_suffix(".new")
    tmp.write_text(json.dumps(other) + "\n")
    tmp.replace(log)                                                      # as import-reader does
    st.sync()
    assert set(st.state["latest"]) == {other["card_id"]}


def test_adjudication_reopens_when_the_positions_change(tmp_path, client_factory):
    adj = _adjudication(tmp_path, client_factory)
    item = adj.get("/api/adj/item/0").json()
    keys = {k["key"]: {"choice": "A"} for k in item["keys"]}
    assert adj.post("/api/adj/item/0/resolve", json={"item_id": item["item_id"], "keys": keys}).status_code == 200
    assert adj.get("/api/adj/session").json()["done"] == 1
    data = tmp_path / "data"
    qpath = next(data.rglob("adjudication_queue.json"))
    q = json.loads(qpath.read_text())
    q[0]["keys"][0]["a"] = [q[0]["keys"][0]["a"][0] + 5, q[0]["keys"][0]["a"][1]]   # a reader revised it
    qpath.write_text(json.dumps(q))
    assert adj.get("/api/adj/session").json()["done"] == 0


def test_find_card_position_and_verify_is_read_only(study, client_factory):
    from goldkp.project import verify
    data, imgs = study
    c = client_factory(data, imgs)
    card = c.get("/api/r/r1/card/2").json()
    assert c.get("/api/r/r1/find", params={"card_id": card["card_id"]}).json() == {"pos": 2}
    assert c.get("/api/r/r1/find", params={"card_id": "nope"}).status_code == 404
    c.post("/api/r/r1/card/2/submit", json=answer(card))
    (data / "readers/r1/state.json").unlink()
    assert verify(data, imgs)["readers_cards_done"]["r1"] == 1
    assert not (data / "readers/r1/state.json").exists()


def _adj_df():
    import math
    import pandas as pd
    rows = []
    for k, (a, b) in {"heart|l": ([100, 100], [110, 100]), "heart|r": ([200, 200], [230, 200])}.items():
        rows.append(dict(card_id="c0", key=k, ltype="frontal|" + k, s=10.0, xy_a=a, xy_b=b, vis_a=True,
                         vis_b=True, un_a=False, un_b=False, d=math.dist(a, b)))
    return pd.DataFrame(rows)


def _sig(l):
    import pandas as pd
    return pd.DataFrame([{"ltype": "frontal|heart|l", "sigma_robust": l},
                         {"ltype": "frontal|heart|r", "sigma_robust": 0.2}])


def test_adjudication_survives_sigma_change(tmp_path):
    import math
    import aggregate as A
    factor = 3 * math.sqrt(2)
    df = _adj_df()
    q, _ = A.adjudication_queue(A.decide(df, _sig(0.2), factor))
    it = q[0]
    rec = {"item_id": it["item_id"], "card_id": "c0",
           "keys": {k["key"]: {"choice": "A", "xy": None} for k in it["keys"]},
           "basis": json.loads(json.dumps(it["keys"]))}
    (tmp_path / "adjudication").mkdir()
    (tmp_path / "adjudication" / "state.json").write_text(json.dumps({it["item_id"]: rec}))
    # more cards read: sigma of heart|l grows, heart|l is no longer flagged, heart|r keeps its positions
    out = A.merge_adjudication(A.decide(df, _sig(0.3), factor), tmp_path)
    assert set(out.reason) == {"adjudicated:A"}
    assert all(g is not None for g in out.gold)
    # a reader moved heart|r: only that landmark is reopened
    df2 = df.copy()
    df2.at[1, "xy_b"] = [240, 200]
    out = A.merge_adjudication(A.decide(df2, _sig(0.2), factor), tmp_path)
    assert out.reason.iloc[1] != "adjudicated:A"
    assert out.reason.iloc[0] == "adjudicated:A"
    # decisions saved before the basis was recorded are kept
    rec.pop("basis")
    (tmp_path / "adjudication" / "state.json").write_text(json.dumps({it["item_id"]: rec}))
    out = A.merge_adjudication(A.decide(df, _sig(0.2), factor), tmp_path)
    assert set(out.reason) == {"adjudicated:A"}
