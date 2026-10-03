"""Tests for batch-group comparison: alignment, exclusions, snapshots.

Covers the five acceptance cases:
  1. deterministic local control + the two existing demo batches align on
     first crack -> median trend + dispersion band WITH member detail, while
     each original raw curve remains individually available;
  2. a member missing the anchor, or whose anchor sits inside a long dropout,
     is excluded with an explicit reason — never bridged by interpolation;
  3. duplicate adds and stale-revision edits neither duplicate members nor
     silently overwrite;
  4. a manual correction of an included member's key event leaves old
     snapshots replayable and marks the current group stale; a new snapshot
     can be published;
  5. replay, export and independent recompute reproduce the identical member
     set, parameters, exclusions and aggregate.
"""
import warnings

warnings.filterwarnings("ignore")

from sqlalchemy.orm import Session

from app import synth
from app.groups import GroupParams, analyze_group
from app.main import _persist_spec
from app.models import (
    ORIGIN_LOCAL_SYNTHETIC_CONTROL,
    engine,
)
from app.models import GroupMember as GMRow


def _three_batches(client):
    client.post("/api/seed")
    client.post("/api/seed/control")
    batches = client.get("/api/batches").json()
    # locate by name: the session DB may also hold exclusion-fixture batches
    a = next(b for b in batches if b["name"] == "SYN-2026-0920-A")
    b = next(b for b in batches if b["name"] == "SYN-2026-0920-B")
    c = next(b for b in batches if b["name"] == "SYN-LOCAL-CONTROL-C")
    return a, b, c


def _make_fixture_batch(*, name, seed, drop_fc=False, fc_t=None, dropouts=None):
    kwargs = {"name": name, "seed": seed}
    if dropouts is not None:
        kwargs["dropout_ranges_s"] = dropouts
    spec = synth.generate_batch(**kwargs)
    if drop_fc:
        spec["events"] = [e for e in spec["events"] if e["event_type"] != "first_crack_start"]
    if fc_t is not None:
        spec["events"] = [e for e in spec["events"] if e["event_type"] != "first_crack_start"]
        spec["events"].append(
            {"event_type": "first_crack_start", "t_s": fc_t, "source": "manual",
             "label": "forced fc"}
        )
    with Session(engine) as s:
        row = _persist_spec(s, spec)
        s.commit()
        s.refresh(row)
        return row.id


# ---------------------------------------------------------------------------
# 1. three-batch alignment on first crack — bands with member detail
# ---------------------------------------------------------------------------

def test_control_batch_is_deterministic_and_marked(client):
    _, _, c = _three_batches(client)
    assert c["data_origin"] == ORIGIN_LOCAL_SYNTHETIC_CONTROL
    assert c["is_control_batch"] is True
    assert c["is_local_synthetic"] is True
    assert c["generator_seed"] == synth.CONTROL_SEED
    # regenerating the spec yields identical samples (deterministic entry)
    spec1 = synth.local_control_batch()
    spec2 = synth.local_control_batch()
    assert spec1["samples"] == spec2["samples"]
    assert spec1["events"] == spec2["events"]
    assert spec1["data_origin"] == ORIGIN_LOCAL_SYNTHETIC_CONTROL


def test_group_aligns_three_on_first_crack_with_bands_and_member_curves(client):
    a, b, c = _three_batches(client)
    r = client.post(
        "/api/groups",
        json={
            "name": "一爆对齐组",
            "anchor_event_type": "first_crack_start",
            "batch_ids": [a["id"], b["id"], c["id"]],
        },
    )
    assert r.status_code == 201, r.text
    gid = r.json()["id"]

    g = client.get(f"/api/groups/{gid}").json()
    res = g["current_analysis"]
    assert res["included_batch_ids"] == [a["id"], b["id"], c["id"]]
    assert res["excluded_batch_ids"] == []

    agg = res["aggregate"]
    assert agg["n_included_members"] == 3
    points = agg["points"]
    assert len(points) > 20
    # every emitted point has all three members' RAW support and a band
    for p in points:
        assert p["n"] == 3
        assert p["q1_c"] <= p["median_c"] <= p["q3_c"]
    # alignment: a grid point at tau=0 exists (the anchor itself, all measured
    # within tolerance), proving the batches line up on first crack
    assert any(p["tau_s"] == 0 for p in points)
    # member detail is present per grid point
    assert len(agg["member_values"]) == len(points) * 3

    # every member retains its full original raw curve + interpolation/gap data
    for m in res["members"]:
        assert m["included"] is True
        assert m["anchor"]["event_type"] == "first_crack_start"
        pts = m["series"]["raw_points"]
        assert any(p["bean_temp_c"] is None for p in pts)  # dropout preserved
        assert any(g["status"] == "wide_unfilled" for g in m["series"]["missing_segments"])
        assert m["anchor"]["selection"] == "current_event"

    # provenance + boundary text
    assert res["provenance"]["contains_local_synthetic"] is True
    assert res["provenance"]["contains_local_synthetic_control"] is True
    assert "合成演示" in res["provenance"]["disclaimer"]
    assert "真实稳定性结论" in res["provenance"]["disclaimer"]
    assert "不构成因果" in res["non_causal_note"]


def test_aggregate_never_interpolates_support(client):
    a, b, c = _three_batches(client)
    gid = client.post("/api/groups", json={
        "name": "g", "batch_ids": [a["id"], b["id"], c["id"]]}).json()["id"]
    res = client.get(f"/api/groups/{gid}").json()["current_analysis"]
    points = res["aggregate"]["points"]
    by_tau = {p["tau_s"]: p for p in points}
    # members A/B have the ~420-480s long dropout.  FC anchors are ~480, so the
    # tau region -60..0 corresponds to the dropout for A/B: no band there.
    assert -50.0 not in by_tau or by_tau[-50.0]["n"] < 3
    # well after crack all members have raw samples again
    assert 30.0 in by_tau and by_tau[30.0]["n"] == 3
    # every member value sits at a measured sample within grid_step/2
    for mv in res["aggregate"]["member_values"]:
        assert mv["sample_distance_s"] <= 5.0 + 1e-9


def test_group_size_bounds(client):
    a, b, c = _three_batches(client)
    too_few = client.post("/api/groups", json={"name": "x", "batch_ids": [a["id"], b["id"]]})
    assert too_few.status_code == 422
    too_many = client.post(
        "/api/groups", json={"name": "y", "batch_ids": [a["id"]] * 9}
    )
    assert too_many.status_code in (409, 422)  # duplicates or size rejected


def test_unknown_anchor_rejected(client):
    a, b, c = _three_batches(client)
    r = client.post("/api/groups", json={
        "name": "x", "anchor_event_type": "charge",
        "batch_ids": [a["id"], b["id"], c["id"]]})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 2. exclusions: missing anchor / long dropout over anchor
# ---------------------------------------------------------------------------

def test_member_missing_first_crack_is_excluded_with_reason(client):
    a, b, _ = _three_batches(client)
    nofc = _make_fixture_batch(name="NO-FC", seed=99, drop_fc=True)
    gid = client.post("/api/groups", json={
        "name": "g2", "batch_ids": [a["id"], b["id"], nofc]}).json()["id"]
    res = client.get(f"/api/groups/{gid}").json()["current_analysis"]
    assert sorted(res["included_batch_ids"]) == sorted([a["id"], b["id"]])
    (ex,) = [x for x in res["exclusions"] if x["batch_id"] == nofc]
    assert ex["reason_code"] == "missing_anchor"
    assert "缺少" in ex["reason"] and "插值" in ex["reason"]
    # excluded member keeps its raw curve but contributes no aggregate
    assert ex["anchor"] is None
    assert res["aggregate"]["n_included_members"] == 2


def test_member_anchor_in_long_gap_is_excluded_not_interpolated(client):
    a, b, _ = _three_batches(client)
    # first crack forced to t=450, inside the 420-480 wide dropout
    gapfc = _make_fixture_batch(
        name="GAP-FC", seed=5, fc_t=450.0,
        dropouts=((150.0, 158.0), (420.0, 480.0)),
    )
    gid = client.post("/api/groups", json={
        "name": "g3", "batch_ids": [a["id"], b["id"], gapfc]}).json()["id"]
    res = client.get(f"/api/groups/{gid}").json()["current_analysis"]
    assert sorted(res["included_batch_ids"]) == sorted([a["id"], b["id"]])
    (ex,) = [x for x in res["exclusions"] if x["batch_id"] == gapfc]
    assert ex["reason_code"] == "anchor_in_long_gap"
    assert ex["anchor_support"]["nearest_measured_delta_s"] > 5.0
    assert ex["anchor_support"]["bracketing_gap_s"] > 45.0
    assert "长断档" in ex["reason"] and "插值" in ex["reason"]


# ---------------------------------------------------------------------------
# 3. duplicates and concurrent edits
# ---------------------------------------------------------------------------

def test_duplicate_batch_cannot_be_added_twice(client):
    a, b, c = _three_batches(client)
    before = len(client.get("/api/groups").json())
    r = client.post("/api/groups", json={
        "name": "dup-no-create", "batch_ids": [a["id"], a["id"], c["id"]]})
    assert r.status_code == 409
    # nothing created by the rejected request
    assert len(client.get("/api/groups").json()) == before

    gid = client.post("/api/groups", json={
        "name": "dup-edit-g", "batch_ids": [a["id"], b["id"], c["id"]]}) .json()["id"]
    rev = client.get(f"/api/groups/{gid}").json()["revision"]
    r2 = client.patch(f"/api/groups/{gid}",
                      json={"base_revision": rev, "batch_ids": [a["id"], a["id"], c["id"]]})
    assert r2.status_code == 409
    # membership untouched
    with Session(engine) as s:
        rows = s.query(GMRow).filter_by(group_id=gid).all()
        assert sorted(m.batch_id for m in rows) == sorted([a["id"], b["id"], c["id"]])


def test_concurrent_edits_from_old_snapshot_revision_conflict(client):
    a, b, c = _three_batches(client)
    gid = client.post("/api/groups", json={
        "name": "g", "batch_ids": [a["id"], b["id"], c["id"]]}) .json()["id"]
    rev = client.get(f"/api/groups/{gid}").json()["revision"]

    ok = client.patch(f"/api/groups/{gid}",
                      json={"base_revision": rev, "description": "editor one"})
    assert ok.status_code == 200
    stale = client.patch(f"/api/groups/{gid}",
                         json={"base_revision": rev, "description": "editor two"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "revision_conflict"
    # first edit survived, second did not silently overwrite
    assert client.get(f"/api/groups/{gid}").json()["description"] == "editor one"


# ---------------------------------------------------------------------------
# 4. event correction: old snapshot replays, current is stale, new snapshot
# ---------------------------------------------------------------------------

def _publish(client, gid, revision, **kw):
    body = {"base_revision": revision}
    body.update(kw)
    return client.post(f"/api/groups/{gid}/snapshots", json=body)


def test_event_correction_marks_stale_but_old_snapshot_replays(client):
    a, b, c = _three_batches(client)
    g = client.post("/api/groups", json={
        "name": "g", "batch_ids": [a["id"], b["id"], c["id"]]}) .json()
    gid, rev = g["id"], g["revision"]
    v1 = _publish(client, gid, rev, note="before correction")
    assert v1.status_code == 201, v1.text
    frozen_ids = v1.json()["document"]["result"]["included_batch_ids"]
    frozen_points = v1.json()["document"]["result"]["aggregate"]["points"]

    # manual FC correction on member A (anchor moves into the 420-480 gap)
    r = client.post(f"/api/batches/{a['id']}/events", json={
        "event_type": "first_crack_start", "t_s": 455.0,
        "source": "manual", "created_by": "lead", "label": "人工一爆"})
    assert r.status_code == 200

    # old snapshot still replays identically
    rp = client.get(f"/api/groups/{gid}/snapshots/1").json()
    assert rp["replay"]["result_identical"] is True
    assert rp["document"]["result"]["included_batch_ids"] == frozen_ids
    assert rp["document"]["result"]["aggregate"]["points"] == frozen_points
    # and it is flagged stale due to the corrected anchor event
    st = rp["replay"]["staleness_vs_current_group"]
    assert st["stale"] is True
    codes = {x["code"] for x in st["reasons"]}
    assert "anchor_event_corrected" in codes

    # current draft reflects the correction: A now excluded
    live = client.get(f"/api/groups/{gid}").json()
    assert live["latest_snapshot"]["staleness"]["stale"] is True
    assert a["id"] in live["current_analysis"]["excluded_batch_ids"]

    # publish a new snapshot from current state
    cur_rev = live["revision"]
    v2 = _publish(client, gid, cur_rev, note="after correction")
    assert v2.status_code == 201
    versions = client.get(f"/api/groups/{gid}/snapshots").json()
    assert [x["version"] for x in versions] == [1, 2]
    assert versions[0]["staleness"]["stale"] is True
    assert versions[1]["staleness"]["stale"] is False
    assert versions[1]["included_batch_ids"] == [b["id"], c["id"]]

    # publishing from an old revision is rejected
    bad = _publish(client, gid, cur_rev, note="late")
    assert bad.status_code == 409


def test_snapshot_freezes_anchor_event_version_not_just_timestamp(client):
    a, b, c = _three_batches(client)
    g = client.post("/api/groups", json={
        "name": "g", "batch_ids": [a["id"], b["id"], c["id"]]}) .json()
    _publish(client, g["id"], g["revision"])
    doc = client.get(f"/api/groups/{g['id']}/snapshots/1/export").json()
    freeze_a = next(f for f in doc["members_freeze"] if f["batch_id"] == a["id"])
    # the exact event row id is pinned and present in the frozen history
    pinned = freeze_a["anchor_event_id"]
    assert isinstance(pinned, int)
    assert any(e["id"] == pinned for e in freeze_a["events"])


# ---------------------------------------------------------------------------
# 5. reproducibility: refresh / export / independent recompute
# ---------------------------------------------------------------------------

def test_export_recompute_reproduces_members_exclusions_and_aggregate(client):
    # dedicated fixture batches: other tests in the shared session may have
    # corrected the demo batches' events, so do not rely on them here.
    ids = [
        _make_fixture_batch(name=f"REP-{i}", seed=300 + i) for i in range(3)
    ]
    g = client.post("/api/groups", json={
        "name": "repro-g", "batch_ids": ids,
        "grid_step_s": 10}) .json()
    _publish(client, g["id"], g["revision"])
    doc = client.get(f"/api/groups/{g['id']}/snapshots/1/export").json()
    assert doc["export_kind"] == "batch_group_snapshot"
    assert doc["doc"] == "roast-group-snapshot/1"

    rc = client.post("/api/groups/recompute", json=doc).json()
    assert rc["identical_to_frozen"] is True
    result = rc["result"]
    # member set, params, exclusions and aggregate points all reproduce
    assert result["included_batch_ids"] == sorted(ids)
    assert result["excluded_batch_ids"] == []
    assert result["params"] == doc["params"]
    assert result["anchor_event_type"] == "first_crack_start"
    assert result["aggregate"]["points"] == doc["result"]["aggregate"]["points"]
    # these are local synthetic demo batches too -> markers must survive
    assert result["provenance"]["contains_local_synthetic"] is True
    assert "合成" in result["provenance"]["disclaimer"]


def test_refresh_replay_and_recompute_are_stable_across_calls(client):
    a, b, c = _three_batches(client)
    g = client.post("/api/groups", json={
        "name": "g", "batch_ids": [a["id"], b["id"], c["id"]]}) .json()
    _publish(client, g["id"], g["revision"])

    doc1 = client.get(f"/api/groups/{g['id']}/snapshots/1/export").json()
    doc2 = client.get(f"/api/groups/{g['id']}/snapshots/1/export").json()
    assert doc1 == doc2
    rp1 = client.get(f"/api/groups/{g['id']}/snapshots/1").json()["document"]
    assert rp1["result"] == doc1["result"]
    r1 = client.post("/api/groups/recompute", json=doc1).json()["result"]
    r2 = client.post("/api/groups/recompute", json=doc1).json()["result"]
    assert r1 == r2


def test_pure_analyze_group_from_frozen_doc(client):
    a, b, c = _three_batches(client)
    g = client.post("/api/groups", json={
        "name": "g", "batch_ids": [a["id"], b["id"], c["id"]]}) .json()
    _publish(client, g["id"], g["revision"])
    doc = client.get(f"/api/groups/{g['id']}/snapshots/1/export").json()

    from app.groups import blobs_from_snapshot

    result = analyze_group(
        blobs_from_snapshot(doc),
        anchor_event_type="first_crack_start",
        params=GroupParams(**doc["params"]),
    )
    assert result["aggregate"]["points"] == doc["result"]["aggregate"]["points"]
    assert result["members"][0]["batch_name"] == "SYN-2026-0920-A"
    assert result["members"][0]["bean"] == "Ethiopia Yirgacheffe"
