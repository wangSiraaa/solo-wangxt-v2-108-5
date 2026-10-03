"""FastAPI application: batch curves, sourced events, comparison, export."""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import groups as grp
from . import synth
from .analysis import RoRConfig, build_series, current_events, phase_metrics
from .config import CORS_ORIGINS, MAX_GAP_FILL_S
from .models import (
    Batch,
    BatchGroup,
    Event,
    GroupMember,
    GroupSnapshot,
    Sample,
    engine,
    init_db,
)
from .schemas import (
    ANCHOR_CHOICES,
    BatchMeta,
    EventIn,
    EventOut,
    GroupCreate,
    GroupMemberUpdate,
    GroupParamsUpdate,
    SnapshotCreate,
)

app = FastAPI(title="Coffee Roast Batch Explorer", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_batch(session: Session, batch_id: int) -> Batch:
    b = session.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"batch {batch_id} not found")
    return b


def _samples_as_dicts(batch: Batch) -> list[dict]:
    return [
        {
            "t_s": s.t_s,
            "bean_temp_c": s.bean_temp_c,
            "env_temp_c": s.env_temp_c,
        }
        for s in batch.samples
    ]


def _events_as_dicts(batch: Batch, *, include_history: bool) -> list[dict]:
    rows = []
    for e in batch.events:
        if not include_history and e.superseded:
            continue
        rows.append(
            {
                "id": e.id,
                "batch_id": e.batch_id,
                "event_type": e.event_type,
                "t_s": e.t_s,
                "label": e.label,
                "source": e.source,
                "created_by": e.created_by,
                "value_num": e.value_num,
                "note": e.note,
                "superseded": e.superseded,
                "superseded_by_id": e.superseded_by_id,
                "created_at": e.created_at.isoformat(),
            }
        )
    return rows


def _series_payload(
    batch: Batch,
    *,
    window_s: float,
    display_smooth_s: float,
    max_gap_fill_s: float,
    include_history: bool,
) -> dict[str, Any]:
    series = build_series(
        _samples_as_dicts(batch),
        ror_cfg=RoRConfig(window_s=window_s, display_smooth_s=display_smooth_s),
        max_gap_fill_s=max_gap_fill_s,
    )
    events = _events_as_dicts(batch, include_history=include_history)
    return {
        "batch": BatchMeta.model_validate(batch).model_dump(mode="json"),
        "series": series,
        "events": events,
        "metrics": phase_metrics(events),
        "params": {
            "ror_window_s": window_s,
            "ror_display_smooth_s": display_smooth_s,
            "max_gap_fill_s": max_gap_fill_s,
            "raw_is_immutable": True,
        },
    }


# ---------------------------------------------------------------------------
# batches / seeding
# ---------------------------------------------------------------------------

@app.get("/api/batches", response_model=list[BatchMeta])
def list_batches() -> list[Batch]:
    with Session(engine) as s:
        return list(s.scalars(select(Batch).order_by(Batch.id)))


@app.post("/api/seed", response_model=list[BatchMeta])
def seed_demo() -> list[Batch]:
    """Load the two synthetic demo batches (noise + dropouts, no machine)."""
    with Session(engine) as s:
        created: list[Batch] = []
        for spec in synth.two_demo_batches():
            batch = _persist_spec(s, spec)
            created.append(batch)
        s.commit()
        for b in created:
            s.refresh(b)
        return created


def _persist_spec(s: Session, spec: dict) -> Batch:
    existing = s.scalar(select(Batch).where(Batch.name == spec["name"]))
    if existing is not None:
        return existing
    b = Batch(
        name=spec["name"],
        roaster=spec["roaster"],
        bean=spec["bean"],
        charge_at=spec["charge_at"],
        charge_temp_c=spec["charge_temp_c"],
        ambient_temp_c=spec["ambient_temp_c"],
        target_drop_temp_c=spec["target_drop_temp_c"],
        note=spec["note"],
        is_synthetic=bool(spec.get("is_synthetic", True)),
        synthetic_kind=spec.get("synthetic_kind"),
        generator=spec.get("generator"),
    )
    b.samples = [
        Sample(
            t_s=sp["t_s"],
            bean_temp_c=sp["bean_temp_c"],
            env_temp_c=sp["env_temp_c"],
        )
        for sp in spec["samples"]
    ]
    b.events = [Event(**ev) for ev in spec["events"]]
    s.add(b)
    s.flush()
    return b


@app.post("/api/seed-control", response_model=BatchMeta)
def seed_control() -> Batch:
    """Generate the deterministic *local* control batch (complete samples).

    Idempotent: the recipe name + fixed seed make the batch identical on every
    call and on every machine; a second call returns the existing row rather
    than duplicating it.  The batch is marked on every surface as a local
    synthetic demo — never machine data, never uploaded.
    """
    spec = synth.control_batch()
    with Session(engine) as s:
        b = _persist_spec(s, spec)
        s.commit()
        s.refresh(b)
        return b


@app.get("/api/batches/{batch_id}/series")
def get_series(
    batch_id: int,
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
    include_history: bool = Query(False),
) -> dict[str, Any]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        return _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=max_gap_fill_s,
            include_history=include_history,
        )


# ---------------------------------------------------------------------------
# events: append-only corrections with provenance
# ---------------------------------------------------------------------------

@app.post("/api/batches/{batch_id}/events", response_model=EventOut)
def add_event(batch_id: int, ev: EventIn) -> Event:
    with Session(engine) as s:
        _get_batch(s, batch_id)
        row = Event(batch_id=batch_id, **ev.model_dump())
        s.add(row)
        s.flush()
        # Only one *current* event per type: supersede the previous current one.
        if row.event_type != "damper_change":
            prev = s.scalars(
                select(Event).where(
                    Event.batch_id == batch_id,
                    Event.event_type == row.event_type,
                    Event.superseded.is_(False),
                    Event.id != row.id,
                )
            ).all()
            for p in prev:
                p.superseded = True
                p.superseded_by_id = row.id
        s.commit()
        s.refresh(row)
        return row


@app.get("/api/batches/{batch_id}/events", response_model=list[EventOut])
def list_events(batch_id: int, include_history: bool = Query(False)) -> list[Event]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        q = select(Event).where(Event.batch_id == batch_id)
        if not include_history:
            q = q.where(Event.superseded.is_(False))
        return list(s.scalars(q.order_by(Event.t_s)))


# ---------------------------------------------------------------------------
# comparison (no causal claims) + export / recompute
# ---------------------------------------------------------------------------

@app.get("/api/compare")
def compare(
    a: int = Query(..., description="first batch id"),
    b: int = Query(..., description="second batch id"),
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
) -> dict[str, Any]:
    """Overlay two batches on charge-relative time. Damper changes are shown
    as marks so the operator can eyeball before/after shape; the API attaches
    an explicit non-causal note."""
    with Session(engine) as s:
        ba, bb = _get_batch(s, a), _get_batch(s, b)
        payload = {
            "batches": [
                _series_payload(
                    ba,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                ),
                _series_payload(
                    bb,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                ),
            ],
            "interpretation": (
                "曲线按开火/下豆时刻对齐叠加。风门变化以标记线显示，"
                "前后形态仅供观察对比，不构成因果结论（无对照、无重复、无统计检验）。"
            ),
        }
        return payload


@app.get("/api/batches/{batch_id}/export")
def export_batch(batch_id: int, window_s: float = 30.0, display_smooth_s: float = 12.0) -> dict[str, Any]:
    """Self-contained export: raw samples, sourced events, parameters, and the
    derived phase metrics.  The metrics can be reproduced from raw + events +
    the stated window (see /api/recompute)."""
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        payload = _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=MAX_GAP_FILL_S,
            include_history=True,
        )
        payload["export_version"] = 1
        payload["reproducibility"] = {
            "raw_samples_are_source_of_truth": True,
            "metrics_depend_on": ["raw_samples", "current(non-superseded) events", "ror_window_s"],
            "pipeline": "numpy centred least-squares RoR; linear gap fill flagged",
        }
        return payload


@app.post("/api/recompute")
def recompute(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-derive series + metrics from an export-style payload.

    Used to verify an export reproduces every stage metric without touching
    the database.  Body: {"samples": [...], "events": [...], "params": {...}}.
    """
    try:
        samples = payload["samples"]
        events = payload.get("events", [])
        params = payload.get("params", {})
    except KeyError as exc:
        raise HTTPException(422, f"missing field: {exc}")
    cfg = RoRConfig(
        window_s=float(params.get("ror_window_s", 30.0)),
        display_smooth_s=float(params.get("ror_display_smooth_s", 12.0)),
    )
    series = build_series(
        samples,
        ror_cfg=cfg,
        max_gap_fill_s=float(params.get("max_gap_fill_s", MAX_GAP_FILL_S)),
    )
    return {
        "series": series,
        "metrics": phase_metrics(events),
        "current_events": current_events(events),
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_connection": "none (synthetic/offline only)"}


# ---------------------------------------------------------------------------
# batch groups: membership, frozen snapshots, replay, provenance
# ---------------------------------------------------------------------------

def _validate_anchor(anchor: str) -> None:
    if anchor not in ANCHOR_CHOICES:
        raise HTTPException(422, f"anchor_event must be one of {ANCHOR_CHOICES}")


def _group_params(g: BatchGroup) -> grp.GroupParams:
    return grp.GroupParams(
        anchor_event=g.anchor_event,
        ror_window_s=g.window_s,
        ror_display_smooth_s=g.display_smooth_s,
        max_gap_fill_s=g.max_gap_fill_s,
        grid_step_s=g.grid_step_s,
        support_tolerance_s=g.support_tolerance_s,
    )


def _member_payload(s: Session, batch: Batch) -> dict:
    return {
        "batch_id": batch.id,
        "batch_name": batch.name,
        "provenance": grp.batch_provenance(batch),
        "samples": _samples_as_dicts(batch),
        "events": _events_as_dicts(batch, include_history=True),
    }


def _build_live_spec(s: Session, g: BatchGroup, *, params: grp.GroupParams) -> dict:
    members = [_member_payload(s, gm.batch) for gm in g.members]
    return grp.build_spec(
        group_params=params,
        members=members,
        group_name=g.name,
        created_by="operator",
    )


def _snapshot_brief(snap: GroupSnapshot) -> dict[str, Any]:
    result = json.loads(snap.result_json)
    return {
        "id": snap.id,
        "group_id": snap.group_id,
        "version": snap.version,
        "group_revision": snap.group_revision,
        "n_included": snap.n_included,
        "n_excluded": snap.n_excluded,
        "spec_sha256": snap.spec_sha256,
        "result_sha256": snap.result_sha256,
        "created_by": snap.created_by,
        "created_at": snap.created_at.isoformat(),
        "provenance": result.get("provenance", {}),
        "demo_warning": result.get("demo_warning"),
    }


def _live_state_for_staleness(s: Session, g: BatchGroup) -> dict:
    """Current membership/anchor/raw-hash view, compared against old specs."""
    members: dict[int, dict] = {}
    ids: list[int] = []
    for gm in g.members:
        ids.append(gm.batch_id)
        anchor = None
        for e in gm.batch.events:
            if e.event_type == g.anchor_event and not e.superseded:
                anchor = e
        members[gm.batch_id] = {
            "anchor_event_id": anchor.id if anchor else None,
            "anchor_t_s": anchor.t_s if anchor else None,
            "raw_sha256": grp.raw_samples_hash(_samples_as_dicts(gm.batch)),
        }
    return {
        "member_batch_ids": ids,
        "members": members,
        "params": _group_params(g).as_dict(),
    }


def _group_payload(s: Session, g: BatchGroup, *, include_preview: bool = True) -> dict[str, Any]:
    params = _group_params(g)
    member_rows = []
    spec = _build_live_spec(s, g, params=params) if include_preview else None
    result = grp.replay_result(spec) if spec else None
    for gm in g.members:
        row = {
            "batch_id": gm.batch_id,
            "position": gm.position,
            "batch": BatchMeta.model_validate(gm.batch).model_dump(mode="json"),
        }
        if result is not None:
            mr = next(
                (m for m in result["members"] if m["batch_id"] == gm.batch_id), None
            )
            if mr:
                row["anchor"] = mr["anchor"]
                row["excluded"] = mr["excluded"]
                row["exclusion_reason"] = mr["exclusion_reason"]
                row["exclusion_detail"] = mr["exclusion_detail"]
        member_rows.append(row)
    payload: dict[str, Any] = {
        "id": g.id,
        "name": g.name,
        "revision": g.revision,
        "anchor_event": g.anchor_event,
        "params": params.as_dict(),
        "note": g.note,
        "members": member_rows,
        "snapshots": [_snapshot_brief(sn) for sn in g.snapshots],
    }
    if result is not None:
        payload["live_preview"] = {
            "n_included": result["n_members_included"],
            "n_excluded": result["n_members_excluded"],
            "exclusions": result["exclusions"],
            "aggregate_summary": None
            if result["aggregate"] is None
            else {
                "n_members": result["aggregate"]["n_members"],
                "fully_supported_points": sum(
                    1
                    for p in result["aggregate"]["bean_temp"]["points"]
                    if p["fully_supported"]
                ),
            },
            "provenance": result["provenance"],
            "demo_warning": result["demo_warning"],
            "interpretation": result["interpretation"],
        }
    return payload


@app.post("/api/groups", status_code=201)
def create_group(body: GroupCreate) -> dict[str, Any]:
    """Create a 3--8 member group with an explicit anchor and parameters."""
    _validate_anchor(body.anchor_event)
    with Session(engine) as s:
        if s.scalar(select(BatchGroup).where(BatchGroup.name == body.name)):
            raise HTTPException(409, f"组名已存在: {body.name}")
        batches = []
        for bid in body.batch_ids:
            b = s.get(Batch, bid)
            if b is None:
                raise HTTPException(404, f"batch {bid} not found")
            batches.append(b)
        g = BatchGroup(
            name=body.name,
            anchor_event=body.anchor_event,
            window_s=body.window_s,
            display_smooth_s=body.display_smooth_s,
            max_gap_fill_s=body.max_gap_fill_s,
            grid_step_s=body.grid_step_s,
            support_tolerance_s=body.support_tolerance_s,
            note=body.note,
            revision=1,
        )
        s.add(g)
        s.flush()
        for pos, b in enumerate(batches):
            s.add(GroupMember(group_id=g.id, batch_id=b.id, position=pos))
        s.commit()
        s.refresh(g)
        return _group_payload(s, g)


@app.get("/api/groups")
def list_groups() -> list[dict[str, Any]]:
    with Session(engine) as s:
        return [
            {
                "id": g.id,
                "name": g.name,
                "revision": g.revision,
                "anchor_event": g.anchor_event,
                "n_members": len(g.members),
                "n_snapshots": len(g.snapshots),
                "latest_snapshot_version": max((sn.version for sn in g.snapshots), default=None),
            }
            for g in s.scalars(select(BatchGroup).order_by(BatchGroup.id))
        ]


@app.get("/api/groups/{group_id}")
def get_group(group_id: int) -> dict[str, Any]:
    with Session(engine) as s:
        g = _get_group(s, group_id)
        payload = _group_payload(s, g)
        # Staleness of every existing snapshot against the live group.
        payload["snapshot_staleness"] = _staleness_map(s, g)
        return payload


def _get_group(s: Session, group_id: int) -> BatchGroup:
    g = s.get(BatchGroup, group_id)
    if g is None:
        raise HTTPException(404, f"group {group_id} not found")
    return g


def _staleness_map(s: Session, g: BatchGroup) -> dict[str, Any]:
    live = _live_state_for_staleness(s, g)
    out: dict[str, Any] = {}
    for sn in g.snapshots:
        spec = json.loads(sn.spec_json)
        reasons = grp.staleness_reasons(live, spec)
        out[str(sn.version)] = {
            "is_stale": bool(reasons),
            "reasons": reasons,
            "snapshot_spec_sha256": sn.spec_sha256,
        }
    return out


@app.put("/api/groups/{group_id}/members")
def update_members(group_id: int, body: GroupMemberUpdate) -> dict[str, Any]:
    """Replace the member set atomically, guarded by optimistic revision."""
    if len(body.batch_ids) != len(set(body.batch_ids)):
        raise HTTPException(409, "batch_ids 中存在重复成员；同一批次不能重复加入组")
    with Session(engine) as s:
        g = _get_group(s, group_id)
        if g.revision != body.expected_revision:
            raise HTTPException(
                409,
                f"组成员已被其他编辑端修改（当前 revision={g.revision}，"
                f"你的版本基于 revision={body.expected_revision}）；请刷新后重试，"
                "本次修改未写入，旧报告未被覆盖。",
            )
        batches = []
        for bid in body.batch_ids:
            b = s.get(Batch, bid)
            if b is None:
                raise HTTPException(404, f"batch {bid} not found")
            batches.append(b)
        # Replace membership in one transaction; the unique constraint is the
        # final backstop against racing duplicate inserts.
        s.execute(delete(GroupMember).where(GroupMember.group_id == group_id))
        s.flush()
        for pos, b in enumerate(batches):
            s.add(GroupMember(group_id=group_id, batch_id=b.id, position=pos))
        if body.note is not None:
            g.note = body.note
        g.revision += 1
        try:
            s.commit()
        except IntegrityError:  # pragma: no cover - constraint backstop
            s.rollback()
            raise HTTPException(409, "重复成员或并发冲突，修改已拒绝（未覆盖既有报告）")
        s.refresh(g)
        return _group_payload(s, g)


@app.put("/api/groups/{group_id}/params")
def update_params(group_id: int, body: GroupParamsUpdate) -> dict[str, Any]:
    with Session(engine) as s:
        g = _get_group(s, group_id)
        if g.revision != body.expected_revision:
            raise HTTPException(
                409,
                f"组已被其他编辑端修改（当前 revision={g.revision}，"
                f"你基于 revision={body.expected_revision}）；请刷新后重试。",
            )
        if body.anchor_event is not None:
            _validate_anchor(body.anchor_event)
            g.anchor_event = body.anchor_event
        for field in (
            "window_s",
            "display_smooth_s",
            "max_gap_fill_s",
            "grid_step_s",
            "support_tolerance_s",
        ):
            val = getattr(body, field)
            if val is not None:
                setattr(g, field, val)
        g.revision += 1
        s.commit()
        s.refresh(g)
        return _group_payload(s, g)


def _cut_snapshot(s: Session, g: BatchGroup, created_by: str) -> GroupSnapshot:
    params = _group_params(g)
    spec = _build_live_spec(s, g, params=params)
    result = grp.replay_result(spec)
    version = max((sn.version for sn in g.snapshots), default=0) + 1
    spec_json = json.dumps(spec, ensure_ascii=False, sort_keys=True)
    result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
    snap = GroupSnapshot(
        group_id=g.id,
        version=version,
        group_revision=g.revision,
        spec_json=spec_json,
        result_json=result_json,
        spec_sha256=grp.sha256_of(json.loads(spec_json)),
        result_sha256=grp.sha256_of(json.loads(result_json)),
        n_included=result["n_members_included"],
        n_excluded=result["n_members_excluded"],
        created_by=created_by,
    )
    s.add(snap)
    s.flush()
    return snap


@app.post("/api/groups/{group_id}/snapshots", status_code=201)
def create_snapshot(group_id: int, body: SnapshotCreate) -> dict[str, Any]:
    """Freeze member set + anchor versions + parameters + exclusions + result."""
    with Session(engine) as s:
        g = _get_group(s, group_id)
        snap = _cut_snapshot(s, g, body.created_by)
        s.commit()
        s.refresh(snap)
        return _snapshot_full(s, g, snap)


def _snapshot_full(
    s: Session, g: BatchGroup, snap: GroupSnapshot, *, replay: dict | None = None
) -> dict[str, Any]:
    spec = json.loads(snap.spec_json)
    stored = json.loads(snap.result_json)
    replayed = replay if replay is not None else grp.replay_result(spec)
    matches = grp.sha256_of(replayed) == snap.result_sha256
    return {
        "snapshot": _snapshot_brief(snap),
        "group_revision_at_cut": snap.group_revision,
        "spec": spec,
        "result": stored,
        "replay_check": {
            "recomputed_result_sha256": grp.sha256_of(replayed),
            "stored_result_sha256": snap.result_sha256,
            "matches": matches,
        },
        "staleness": _staleness_for(s, g, spec),
        "interpretation": stored["interpretation"],
        "demo_warning": stored.get("demo_warning"),
    }


def _staleness_for(s: Session, g: BatchGroup, spec: dict) -> dict[str, Any]:
    reasons = grp.staleness_reasons(_live_state_for_staleness(s, g), spec)
    return {"is_stale": bool(reasons), "reasons": reasons}


@app.get("/api/groups/{group_id}/snapshots/{version}")
def get_snapshot(group_id: int, version: int) -> dict[str, Any]:
    with Session(engine) as s:
        g = _get_group(s, group_id)
        snap = _get_snapshot(g, version)
        return _snapshot_full(s, g, snap)


def _get_snapshot(g: BatchGroup, version: int) -> GroupSnapshot:
    for sn in g.snapshots:
        if sn.version == version:
            return sn
    raise HTTPException(404, f"snapshot v{version} not found in group {g.id}")


@app.get("/api/groups/{group_id}/snapshots/{version}/export")
def export_snapshot(group_id: int, version: int) -> dict[str, Any]:
    """Self-contained snapshot export: everything an offline replay needs."""
    with Session(engine) as s:
        g = _get_group(s, group_id)
        snap = _get_snapshot(g, version)
        spec = json.loads(snap.spec_json)
        result = json.loads(snap.result_json)
        replayed = grp.replay_result(spec)
        return {
            "export_version": 1,
            "kind": "batch_group_snapshot",
            "generated_locally": True,
            "machine_connection": "none (synthetic/offline only)",
            "snapshot": _snapshot_brief(snap),
            "spec": spec,
            "result": result,
            "reproducibility": {
                "replay_endpoint": "POST /api/group-replay",
                "recomputed_result_sha256": grp.sha256_of(replayed),
                "stored_result_sha256": snap.result_sha256,
                "matches": grp.sha256_of(replayed) == snap.result_sha256,
                "depends_on": [
                    "spec.members[].samples (raw, non-interpolated truth)",
                    "spec.members[].events (frozen anchor event versions)",
                    "spec.params (anchor + analysis parameters)",
                ],
                "note": (
                    "导出中的合成来源标记随载荷携带；含 deterministic_control 的结果"
                    "只能表述为本地合成演示，不是真实稳定性结论，不来自真实烘焙机或上传数据。"
                ),
            },
            "interpretation": result["interpretation"],
            "demo_warning": result.get("demo_warning"),
        }


@app.post("/api/group-replay")
def group_replay(payload: dict[str, Any]) -> dict[str, Any]:
    """Independently recompute a group result from an exported snapshot spec.

    No database access: callers POST exactly the ``spec`` object from
    /api/groups/{id}/snapshots/{v}/export.  When the export also supplies the
    stored result, the response verifies hash equality.
    """
    spec = payload.get("spec", payload)
    if not isinstance(spec, dict) or "members" not in spec or "params" not in spec:
        raise HTTPException(422, "payload must be a group spec (members + params)")
    try:
        result = grp.replay_result(spec)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, f"cannot replay spec: {exc}")
    out: dict[str, Any] = {
        "result": result,
        "result_sha256": grp.sha256_of(result),
        "spec_sha256": grp.sha256_of(spec),
        "demo_warning": result.get("demo_warning"),
        "interpretation": result["interpretation"],
        "offline": True,
    }
    stored_sha = payload.get("result_sha256") or (
        payload.get("result") and grp.sha256_of(payload["result"])
    )
    if stored_sha:
        out["matches_exported_result"] = stored_sha == out["result_sha256"]
    return out
