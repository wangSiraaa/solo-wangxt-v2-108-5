"""API tests for batch-group comparison (3-8 members, versioned snapshots).

Acceptance mapping:
  ①  control + two demo batches aligned at first crack -> member-level median
      trend + dispersion bands, raw per-member curves still available;
  ②  member missing the anchor, or with the anchor inside a long unfilled gap,
      is excluded with an explicit reason and never interpolated over;
  ③  duplicate batch add and two editors working from a stale snapshot produce
      neither duplicate members nor silent overwrite;
  ④  after a manual correction of a member's key event the old snapshot still
      replays and the group flags it stale; a new snapshot can be cut;
  ⑤  refresh / export / independent replay reproduce the same member set,
      parameters, exclusion records and aggregate result.
"""
import copy
import itertools

import pytest

from app import synth
from app.groups import (
    GroupParams,
    build_spec,
    raw_samples_hash,
    replay_result,
)
from app.models import Batch, Event, Sample
from app.synth import (
    CONTROL_BATCH_NAME,
    CONTROL_BATCH_SEED,
    control_batch,
    control_fingerprint,
)

_name_seq = itertools.count(1)


# ---------------------------------------------------------------------------
# deterministic local control batch
# ---------------------------------------------------------------------------

def _seed_three(client):
    pairs = client.post("/api/seed").json()
    ctrl = client.post("/api/seed-control").json()
    assert ctrl["synthetic_kind"] == "deterministic_control"
    return pairs[0]["id"], pairs[1]["id"], ctrl["id"]


def test_control_batch_is_deterministic_complete_and_labelled():
    a, b = control_batch(), control_batch()
    assert a == b  # fixed recipe + seed
    assert a["name"] == CONTROL_BATCH_NAME
    assert a["synthetic_kind"] == "deterministic_control"
    assert f"seed={CONTROL_BATCH_SEED}" in a["generator"]
    # complete samples: no NULL probe values anywhere
    assert len(a["samples"]) > 150
    assert all(p["bean_temp_c"] is not None and p["env_temp_c"] is not None
               for p in a["samples"])
    # every key event present
    kinds = {e["event_type"] for e in a["events"]}
    assert {"charge", "turning_point", "first_crack_start",
            "first_crack_end", "drop"} <= kinds
    # unmistakably marked as non-machine / non-upload / demo-only
    assert "本地合成" in a["note"] and "非真实烘焙机" in a["roaster"]
    # fingerprint stable
    assert control_fingerprint(a) == control_fingerprint(control_batch())


def test_seed_control_endpoint_is_idempotent(client):
    c1 = client.post("/api/seed-control")
    assert c1.status_code == 200
    c2 = client.post("/api/seed-control")
    assert c2.status_code == 200
    assert c1.json()["id"] == c2.json()["id"]
    assert c1.json()["synthetic_kind"] == "deterministic_control"


# ---------------------------------------------------------------------------
# ① three-batch FC alignment: median + dispersion, raw curves retained
# ---------------------------------------------------------------------------

def _make_group(client, ids, **kw):
    body = {"name": kw.pop("name", f"G-{next(_name_seq)}"),
            "anchor_event": "first_crack_start", "batch_ids": list(ids)}
    body.update(kw)
    r = client.post("/api/groups", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_three_member_group_fc_aligned_aggregate_and_member_curves(client):
    a, b, ctrl = _seed_three(client)
    g = _make_group(client, [a, b, ctrl])
    preview = g["live_preview"]
    assert preview["n_included"] == 3 and preview["n_excluded"] == 0
    assert preview["provenance"]["contains_deterministic_control"] is True
    assert "本地合成演示" in preview["demo_warning"]

    snap = client.post(f"/api/groups/{g['id']}/snapshots", json={}).json()
    res = snap["result"]
    agg = res["aggregate"]
    assert agg is not None
    assert agg["anchor_event"] == "first_crack_start"
    assert agg["n_members"] == 3

    # median+band exist only at fully-supported points; gaps stay broken
    pts = agg["bean_temp"]["points"]
    full = [p for p in pts if p["fully_supported"]]
    broken = [p for p in pts if not p["fully_supported"]]
    assert len(full) > 20
    assert all(p["median"] is not None and p["q25"] <= p["median"] <= p["q75"]
               and p["min"] <= p["median"] <= p["max"] for p in full)
    assert all(p["n_supporting"] < 3 and p["median"] is None for p in broken)
    # every supporting sample is a genuine measured sample near the grid time
    for p in full:
        assert all(abs(dt - p["t_rel_s"]) <= agg["support_tolerance_s"]
                   for dt in p["member_sample_t_rel"])
    # dispersion declared observational, not a confidence interval
    assert agg["dispersion_is_not_confidence_interval"] is True
    # raw per-member curves are retained, with interpolation + long-gap identity
    members = {m["batch_id"]: m for m in res["members"]}
    curve = members[a]["curve"]
    assert len(curve["t_rel_s"]) == len(curve["raw_bean_c"])
    # demo batch A carries interpolation flags and NULL breaks in the raw guide
    assert any(f["is_interpolated"] for f in curve["point_flags"])
    assert any(v is None for v in curve["guide_bean_c"])
    # anchor is at relative zero
    assert members[a]["anchor"]["t_s"] != members[ctrl]["anchor"]["t_s"]
    # non-causal boundary text is attached
    assert "不构成" in res["interpretation"]


def test_aggregate_never_uses_interpolated_values(client):
    # Pure-engine check: a member whose ONLY near-grid value is interpolated
    # must not support that grid point.
    samples = [
        {"t_s": 0, "bean_temp_c": 100.0, "env_temp_c": 190.0},
        {"t_s": 10, "bean_temp_c": 105.0, "env_temp_c": 191.0},
        {"t_s": 20, "bean_temp_c": None, "env_temp_c": 192.0},  # bridged interp
        {"t_s": 30, "bean_temp_c": 110.0, "env_temp_c": 193.0},
    ]
    # Give every member the same anchor=0 charge; one member has a lone
    # interpolated point where others have measurements.
    base = {
        "samples": samples * 1,
        "events": [{"id": 1, "event_type": "charge", "t_s": 0.0,
                    "source": "manual", "superseded": False}],
    }
    members = []
    for i in range(3):
        members.append({
            "batch_id": i + 1, "batch_name": f"M{i}",
            "provenance": {"is_synthetic": True},
            "samples": [dict(s) for s in samples],
            "events": [dict(e) for e in base["events"]],
        })
    p = GroupParams(anchor_event="charge", grid_step_s=10.0,
                    support_tolerance_s=2.0, max_gap_fill_s=45.0,
                    ror_window_s=30.0, ror_display_smooth_s=0.0)
    res = replay_result(build_spec(group_params=p, members=members))
    # t_rel=20 is interpolated for every member -> not fully supported
    at20 = [q for q in res["aggregate"]["bean_temp"]["points"]
            if q["t_rel_s"] == 20.0][0]
    assert at20["fully_supported"] is False and at20["median"] is None


# ---------------------------------------------------------------------------
# ② exclusions: missing anchor / anchor in long gap; no interpolation filling
# ---------------------------------------------------------------------------

def test_member_missing_anchor_is_excluded_with_reason(client, session_factory):
    a, b, ctrl = _seed_three(client)
    # Build a 4th batch whose events lack first_crack_start entirely.
    spec = synth.two_demo_batches()[0]
    extra = session_factory(add_spec_batch(spec, name="SYN-NO-FC", drop_fc=True))
    g = _make_group(client, [a, b, ctrl, extra])
    snap = client.post(f"/api/groups/{g['id']}/snapshots", json={}).json()
    res = snap["result"]
    assert res["n_members_excluded"] == 1
    ex = [e for e in res["exclusions"] if e["batch_id"] == extra][0]
    assert ex["reason"] == "missing_anchor"
    assert res["n_members_included"] == 3


def test_member_anchor_in_long_gap_is_excluded_not_bridged(client):
    a, b, ctrl = _seed_three(client)
    # Move batch A's FC to t=450 (heart of the 423-480 wide NULL run).
    r = client.post(f"/api/batches/{a}/events", json={
        "event_type": "first_crack_start", "t_s": 450.0,
        "source": "manual", "created_by": "tester", "label": "fc moved"})
    assert r.status_code == 200
    g = _make_group(client, [a, b, ctrl])
    snap = client.post(f"/api/groups/{g['id']}/snapshots", json={}).json()
    res = snap["result"]
    assert res["n_members_included"] == 2
    ex = [e for e in res["exclusions"] if e["batch_id"] == a][0]
    assert ex["reason"] == "anchor_in_long_gap"
    assert ex["detail"]["neighbour_span_s"] > 45  # wider than bridge limit
    # aggregate computed over exactly the two supported members
    assert res["aggregate"]["n_members"] == 2


def test_anchor_inside_interpolated_gap_also_refused(client):
    # Pure-level check of the third reason code.
    spec = synth.two_demo_batches()[0]
    events = []
    for k, e in enumerate(spec["events"]):
        row = dict(e, id=k + 1, superseded=False)
        if row["event_type"] == "first_crack_start":
            row["t_s"] = 154.0  # inside the ~9 s short dropout (interpolated)
        events.append(row)
    members = [{
        "batch_id": 1, "batch_name": spec["name"],
        "provenance": {"is_synthetic": True},
        "samples": spec["samples"], "events": events,
    }]
    p = GroupParams()
    res = replay_result(build_spec(group_params=p, members=members))
    assert res["exclusions"][0]["reason"] == "anchor_in_interpolated_gap"


def test_group_requires_three_to_eight_members(client):
    a, b, _ = _seed_three(client)
    r = client.post("/api/groups", json={"name": "TOO-FEW", "batch_ids": [a, b]})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# ③ duplicates and concurrent edits
# ---------------------------------------------------------------------------

def test_duplicate_batch_add_is_rejected(client):
    a, b, ctrl = _seed_three(client)
    r = client.post("/api/groups", json={
        "name": "DUP", "batch_ids": [a, a, b, ctrl]})
    assert r.status_code == 422
    g = _make_group(client, [a, b, ctrl])
    r2 = client.put(f"/api/groups/{g['id']}/members", json={
        "expected_revision": g["revision"], "batch_ids": [a, a, b, ctrl]})
    assert r2.status_code == 422


def test_concurrent_editors_from_same_snapshot_only_first_wins(client):
    a, b, ctrl = _seed_three(client)
    g = _make_group(client, [a, b, ctrl])
    base_rev = g["revision"]
    ok = client.put(f"/api/groups/{g['id']}/members", json={
        "expected_revision": base_rev, "batch_ids": [a, b, ctrl], "note": "one"})
    conflict = client.put(f"/api/groups/{g['id']}/members", json={
        "expected_revision": base_rev, "batch_ids": [a, b, ctrl], "note": "two"})
    assert ok.status_code == 200
    assert conflict.status_code == 409
    final = client.get(f"/api/groups/{g['id']}").json()
    assert final["revision"] == base_rev + 1
    assert final["note"] == "one"  # second writer did not silently overwrite
    assert len(final["members"]) == 3


# ---------------------------------------------------------------------------
# ④ manual correction: old snapshot replays; group stale; new snapshot cut
# ---------------------------------------------------------------------------

def test_manual_correction_marks_snapshot_stale_but_keeps_old_replay(client):
    a, b, ctrl = _seed_three(client)
    g = _make_group(client, [a, b, ctrl])
    v1 = client.post(f"/api/groups/{g['id']}/snapshots", json={}).json()
    assert v1["staleness"]["is_stale"] is False
    v1_anchor_id = [m for m in v1["result"]["members"] if m["batch_id"] == a][0]
    v1_anchor_id = v1_anchor_id["anchor"]["event_id"]

    r = client.post(f"/api/batches/{a}/events", json={
        "event_type": "first_crack_start", "t_s": 470.0,
        "source": "manual", "created_by": "lead", "label": "corrected FC"})
    assert r.status_code == 200

    detail = client.get(f"/api/groups/{g['id']}/snapshots/1").json()
    assert detail["replay_check"]["matches"] is True          # old snapshot replays
    assert detail["staleness"]["is_stale"] is True
    assert any("anchor_changed" for rsn in detail["staleness"]["reasons"])
    # the frozen spec holds the event history as it was at cut time: exactly
    # one current FC row, which is the original auto anchor (version pinned).
    frozen = [m for m in detail["spec"]["members"] if m["batch_id"] == a][0]
    current_rows = [e for e in frozen["events"]
                    if e["event_type"] == "first_crack_start"]
    current = [e for e in current_rows if not e["superseded"]]
    assert len(current) == 1
    assert current[0]["id"] == v1_anchor_id
    assert current[0]["t_s"] == 480.0  # frozen value, not the later 470
    # the frozen result still reports the old anchor version
    frozen_result_anchor = [
        m for m in detail["result"]["members"] if m["batch_id"] == a
    ][0]["anchor"]
    assert frozen_result_anchor["event_id"] == v1_anchor_id
    assert frozen_result_anchor["t_s"] == 480.0

    # cut a fresh snapshot: version 2, not stale
    v2 = client.post(f"/api/groups/{g['id']}/snapshots", json={}).json()
    assert v2["snapshot"]["version"] == 2
    assert v2["staleness"]["is_stale"] is False
    # v1 still present and replayable
    again = client.get(f"/api/groups/{g['id']}/snapshots/1").json()
    assert again["snapshot"]["version"] == 1
    assert again["replay_check"]["matches"] is True


# ---------------------------------------------------------------------------
# ⑤ export / refresh / offline replay reproduce everything
# ---------------------------------------------------------------------------

def test_export_and_offline_replay_reproduce_snapshot(client):
    a, b, ctrl = _seed_three(client)
    g = _make_group(client, [a, b, ctrl])
    client.post(f"/api/groups/{g['id']}/snapshots", json={}).json()
    ex = client.get(f"/api/groups/{g['id']}/snapshots/1/export").json()

    # export is self-contained: full raw samples + full event history
    member = ex["spec"]["members"][0]
    assert len(member["samples"]) > 50
    assert any("superseded" in e for e in member["events"])
    assert ex["reproducibility"]["matches"] is True
    assert ex["generated_locally"] is True

    rp = client.post("/api/group-replay", json={
        "spec": ex["spec"], "result_sha256": ex["snapshot"]["result_sha256"]})
    assert rp.status_code == 200, rp.text
    body = rp.json()
    assert body["matches_exported_result"] is True
    assert body["offline"] is True

    # member set, params, exclusions and aggregate grid all reproduce
    res = body["result"]
    assert [m["batch_id"] for m in res["members"]] == \
           [m["batch_id"] for m in ex["result"]["members"]]
    assert res["params"] == ex["spec"]["params"]
    assert res["exclusions"] == ex["result"]["exclusions"]
    assert ([p["median"] for p in res["aggregate"]["bean_temp"]["points"]]
            == [p["median"] for p in ex["result"]["aggregate"]["bean_temp"]["points"]])
    # provenance travels into the recomputed result
    assert res["provenance"]["contains_deterministic_control"] is True
    assert "本地合成演示" in res["demo_warning"]


def test_pure_replay_is_independent_of_later_db_changes(client):
    a, b, ctrl = _seed_three(client)
    g = _make_group(client, [a, b, ctrl])
    client.post(f"/api/groups/{g['id']}/snapshots", json={})
    ex = client.get(f"/api/groups/{g['id']}/snapshots/1/export").json()
    # sanity: at cut time all three members were included
    assert ex["result"]["n_members_included"] == 3
    frozen_fc = [m for m in ex["spec"]["members"] if m["batch_id"] == a][0]
    frozen_fc = [e for e in frozen_fc["events"]
                 if e["event_type"] == "first_crack_start"
                 and not e["superseded"]][0]["t_s"]

    # Correct FC *later*; wherever the new value lands, it differs from frozen.
    later_t = 450.0 if frozen_fc != 450.0 else 455.0
    client.post(f"/api/batches/{a}/events", json={
        "event_type": "first_crack_start", "t_s": later_t,
        "source": "manual", "created_by": "x", "label": "later fix"})
    rp = client.post("/api/group-replay", json={"spec": ex["spec"]}).json()
    # offline replay still yields the frozen 3-included result, hash identical
    assert rp["result"]["n_members_included"] == 3
    assert rp["result_sha256"] == ex["snapshot"]["result_sha256"]
    frozen_a = [m for m in rp["result"]["members"] if m["batch_id"] == a][0]
    assert frozen_a["anchor"]["t_s"] == frozen_fc
    assert frozen_a["anchor"]["t_s"] != later_t


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------

def add_spec_batch(spec, *, name, drop_fc=False):
    """Return an insertion callback for a raw spec batch (test-only setup)."""
    events = [dict(e) for e in spec["events"]]
    if drop_fc:
        events = [e for e in events if e["event_type"] != "first_crack_start"]

    def _insert(s):
        b = Batch(
            name=name, roaster=spec["roaster"], bean=spec["bean"],
            charge_at=spec["charge_at"], charge_temp_c=180.0,
            ambient_temp_c=22.0, target_drop_temp_c=spec["target_drop_temp_c"],
            note=spec["note"], is_synthetic=True,
            synthetic_kind=spec.get("synthetic_kind"),
            generator=spec.get("generator"),
        )
        b.samples = [Sample(t_s=sp["t_s"], bean_temp_c=sp["bean_temp_c"],
                            env_temp_c=sp["env_temp_c"]) for sp in spec["samples"]]
        b.events = [Event(**e) for e in events]
        s.add(b)
        s.commit()
        s.refresh(b)
        return b.id
    return _insert


@pytest.fixture()
def session_factory():
    from app.models import engine
    from sqlalchemy.orm import Session

    def _run(fn):
        with Session(engine) as s:
            return fn(s)
    return _run


# ---------------------------------------------------------------------------
# pure-engine units (no API): staleness, parameter gating, replay isolation
# ---------------------------------------------------------------------------

def _three_spec_members():
    raw = synth.two_demo_batches() + [control_batch()]
    out = []
    for i, spec in enumerate(raw, start=1):
        events = [dict(e, id=100 + i * 10 + k, superseded=False)
                  for k, e in enumerate(spec["events"])]
        out.append({
            "batch_id": i,
            "batch_name": spec["name"],
            "provenance": {"is_synthetic": True,
                           "synthetic_kind": spec["synthetic_kind"],
                           "generator": spec["generator"]},
            "samples": spec["samples"],
            "events": events,
        })
    return out


def test_staleness_detects_member_param_and_anchor_changes():
    members = _three_spec_members()
    p = GroupParams()
    spec = build_spec(group_params=p, members=members)

    # identical live state -> not stale
    live = {
        "member_batch_ids": [1, 2, 3],
        "members": {
            m["batch_id"]: {
                "anchor_event_id": next(
                    e["id"] for e in m["events"]
                    if e["event_type"] == "first_crack_start"
                    and not e["superseded"]),
                "anchor_t_s": next(
                    e["t_s"] for e in m["events"]
                    if e["event_type"] == "first_crack_start"
                    and not e["superseded"]),
                "raw_sha256": raw_samples_hash(m["samples"]),
            }
            for m in members
        },
        "params": p.as_dict(),
    }
    from app.groups import staleness_reasons
    assert staleness_reasons(live, spec) == []

    # member removed
    live2 = dict(live, member_batch_ids=[1, 2])
    assert any("members_removed" in r for r in staleness_reasons(live2, spec))

    # params changed
    live3 = dict(live, params=GroupParams(grid_step_s=10.0).as_dict())
    assert any("params_changed" in r for r in staleness_reasons(live3, spec))

    # anchor event version changed
    live4 = copy.deepcopy(live)
    live4["members"][1]["anchor_event_id"] = 9999
    assert any("anchor_changed" in r for r in staleness_reasons(live4, spec))


def test_replay_hash_is_input_determined_and_isolated():
    from app.groups import sha256_of
    members = _three_spec_members()
    spec = build_spec(group_params=GroupParams(), members=members)
    r1 = replay_result(spec)
    # mutate a member input afterwards; a fresh replay of the frozen spec
    # must still equal the first result
    members[0]["samples"][5]["bean_temp_c"] = 999.0
    r2 = replay_result(spec)
    assert sha256_of(r1) == sha256_of(r2)


def test_unsupported_anchor_type_rejected():
    members = _three_spec_members()
    spec = build_spec(group_params=GroupParams(anchor_event="damper_change"),
                      members=members)
    with pytest.raises(ValueError):
        replay_result(spec)


def test_calculation_basis_frozen_in_spec():
    members = _three_spec_members()
    spec = build_spec(group_params=GroupParams(grid_step_s=7.0,
                                               support_tolerance_s=4.5),
                      members=members)
    assert spec["schema"] == "group_spec_v1"
    assert spec["params"]["grid_step_s"] == 7.0
    assert spec["params"]["support_tolerance_s"] == 4.5
    assert "non-interpolated" in spec["calculation_basis"]["median"]
    assert all("raw_sha256" in m for m in spec["members"])
    res = replay_result(spec)
    assert res["aggregate"]["grid_step_s"] == 7.0
