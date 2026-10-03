"""FastAPI application: batch curves, sourced events, groups, export.

Endpoints
---------
Batches: list, seed (two synthetic demos), local deterministic control,
series, append-only event corrections, pairwise compare, self-contained
export, offline recompute.

Groups (3--8 batches, explicit anchor, versionable snapshots):
  POST   /api/groups                      create draft (duplicates rejected)
  GET    /api/groups                      list drafts
  GET    /api/groups/{id}                 draft + current (unfrozen) analysis
  PATCH  /api/groups/{id}                 edit draft (optimistic revision)
  POST   /api/groups/{id}/snapshots       freeze an immutable version
  GET    /api/groups/{id}/snapshots       version list (staleness per version)
  GET    /api/groups/{id}/snapshots/{ver} replay a frozen report
  GET    /api/groups/snapshots/{sid}/export
                                         self-contained snapshot export
  POST   /api/groups/recompute            rebuild a result from an export doc
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import synth
from .analysis import RoRConfig, build_series, current_events, phase_metrics
from .config import CORS_ORIGINS, MAX_GAP_FILL_S
from .groups import (
    MAX_MEMBERS,
    MIN_MEMBERS,
    GroupParams,
    analyze_group,
    build_snapshot_document,
    deserialize,
    recompute_from_snapshot,
    serialize,
    snapshot_staleness,
)
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
    BatchMeta,
    EventIn,
    EventOut,
    GroupCreate,
    GroupPatch,
    SnapshotCreate,
)

app = FastAPI(title="Coffee Roast Batch Explorer", version="1.1.0")
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


def _get_group(session: Session, group_id: int) -> BatchGroup:
    g = session.get(BatchGroup, group_id)
    if g is None:
        raise HTTPException(404, f"group {group_id} not found")
    return g


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


def _batch_meta(batch: Batch) -> dict[str, Any]:
    return BatchMeta.model_validate(batch).model_dump(mode="json")


def _persist_spec(session: Session, spec: dict) -> Batch:
    """Idempotently persist a generated batch spec incl. provenance."""
    existing = session.scalar(select(Batch).where(Batch.name == spec["name"]))
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
        data_origin=spec["data_origin"],
        is_local_synthetic=spec["is_local_synthetic"],
        is_control_batch=spec["is_control_batch"],
        generator_seed=spec["generator_seed"],
        generator_version=spec["generator_version"],
    )
    b.samples = [
        Sample(t_s=sp["t_s"], bean_temp_c=sp["bean_temp_c"], env_temp_c=sp["env_temp_c"])
        for sp in spec["samples"]
    ]
    b.events = [Event(**ev) for ev in spec["events"]]
    session.add(b)
    session.flush()
    return b


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
        "batch": _batch_meta(batch),
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


def _group_params(g: BatchGroup) -> GroupParams:
    return GroupParams(
        ror_window_s=g.ror_window_s,
        display_smooth_s=g.display_smooth_s,
        max_gap_fill_s=g.max_gap_fill_s,
        grid_step_s=g.grid_step_s,
        anchor_support_tolerance_s=g.anchor_support_tolerance_s,
    )


def _member_blob(session: Session, batch_id: int) -> dict[str, Any]:
    b = _get_batch(session, batch_id)
    return {
        "batch": _batch_meta(b),
        "samples": _samples_as_dicts(b),
        # full history so pinned event versions can always be resolved
        "events": _events_as_dicts(b, include_history=True),
    }


def _group_summary(session: Session, g: BatchGroup) -> dict[str, Any]:
    latest = None
    if g.latest_snapshot_version is not None:
        row = session.scalar(
            select(GroupSnapshot).where(
                GroupSnapshot.group_id == g.id,
                GroupSnapshot.version == g.latest_snapshot_version,
            )
        )
        if row is not None:
            doc = deserialize(row.payload_json)
            latest = {
                "snapshot_id": row.id,
                "version": row.version,
                "created_at": row.created_at.isoformat(),
                "staleness": snapshot_staleness(
                    doc,
                    current_group=g,
                    current_batches=[session.get(Batch, mid.batch_id) for mid in g.members],
                    current_events_by_cache=_current_event_cache(session, g),
                ),
            }
    return {
        "id": g.id,
        "name": g.name,
        "description": g.description,
        "anchor_event_type": g.anchor_event_type,
        "revision": g.revision,
        "params": {
            "ror_window_s": g.ror_window_s,
            "display_smooth_s": g.display_smooth_s,
            "max_gap_fill_s": g.max_gap_fill_s,
            "grid_step_s": g.grid_step_s,
            "anchor_support_tolerance_s": g.anchor_support_tolerance_s,
        },
        "batch_ids": [m.batch_id for m in g.members],
        "latest_snapshot_version": g.latest_snapshot_version,
        "latest_snapshot": latest,
        "updated_at": g.updated_at.isoformat(),
    }


def _current_event_cache(session: Session, g: BatchGroup) -> dict[int, dict[int, Event]]:
    """{batch_id: {event_id: Event}} for the groups' members (staleness use)."""
    cache: dict[int, dict[int, Event]] = {}
    for m in g.members:
        rows = session.scalars(
            select(Event).where(Event.batch_id == m.batch_id)
        ).all()
        cache[m.batch_id] = {e.id: e for e in rows}
    return cache


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
        created = [_persist_spec(s, spec) for spec in synth.two_demo_batches()]
        s.commit()
        for b in created:
            s.refresh(b)
        return created


@app.post("/api/seed/control", response_model=BatchMeta)
def seed_local_control() -> Batch:
    """Generate the deterministic local-only control batch (batch C).

    Local entry point: fixed seed, fully reproducible samples + key events,
    never connected to a roaster and never the result of an upload.  The batch
    is permanently marked ``local_synthetic_control``; that mark propagates
    into group snapshots, legend labels, exports and recomputation.
    """
    with Session(engine) as s:
        b = _persist_spec(s, synth.local_control_batch())
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


# ---------------------------------------------------------------------------
# batch groups: drafts, immutable snapshots, replay, export, recompute
# ---------------------------------------------------------------------------

def _unique_ordered(ids: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for i in ids:
        if i in seen:
            raise HTTPException(
                409,
                f"batch {i} 在成员列表中重复出现；同一批次不能重复加入组，"
                "未创建/修改任何成员。",
            )
        seen.add(i)
        out.append(i)
    return out


def _set_members(session: Session, g: BatchGroup, batch_ids: list[int]) -> None:
    ids = _unique_ordered(batch_ids)
    if not (MIN_MEMBERS <= len(ids) <= MAX_MEMBERS):
        raise HTTPException(422, f"组必须包含 {MIN_MEMBERS}–{MAX_MEMBERS} 个批次")
    # every batch must exist first (nothing is partially written on failure)
    for bid in ids:
        _get_batch(session, bid)
    existing = {m.batch_id: m for m in g.members}
    wanted = set(ids)
    for m in list(g.members):
        if m.batch_id not in wanted:
            session.delete(m)
    for pos, bid in enumerate(ids):
        m = existing.get(bid)
        if m is None:
            session.add(GroupMember(group_id=g.id, batch_id=bid, position=pos))
        else:
            m.position = pos
    session.flush()


@app.post("/api/groups", status_code=201)
def create_group(body: GroupCreate) -> dict[str, Any]:
    try:
        body.validated_anchor()
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    with Session(engine) as s:
        g = BatchGroup(
            name=body.name,
            description=body.description,
            anchor_event_type=body.anchor_event_type,
            ror_window_s=body.ror_window_s,
            display_smooth_s=body.display_smooth_s,
            max_gap_fill_s=body.max_gap_fill_s,
            grid_step_s=body.grid_step_s,
            anchor_support_tolerance_s=body.anchor_support_tolerance_s,
            revision=1,
        )
        s.add(g)
        s.flush()
        _set_members(s, g, body.batch_ids)
        s.commit()
        s.refresh(g)
        created = _group_summary(s, g)
        return created


@app.get("/api/groups")
def list_groups() -> list[dict[str, Any]]:
    with Session(engine) as s:
        return [_group_summary(s, g) for g in s.scalars(select(BatchGroup).order_by(BatchGroup.id))]


def _live_result(session: Session, g: BatchGroup) -> dict[str, Any]:
    params = _group_params(g)
    blobs = [_member_blob(session, m.batch_id) for m in g.members]
    return analyze_group(
        blobs, anchor_event_type=g.anchor_event_type, params=params
    )


@app.get("/api/groups/{group_id}")
def get_group(group_id: int) -> dict[str, Any]:
    with Session(engine) as s:
        g = _get_group(s, group_id)
        summary = _group_summary(s, g)
        # Current, NOT frozen, analysis of the draft (clearly labelled).
        summary["current_analysis"] = _live_result(s, g)
        return summary


@app.patch("/api/groups/{group_id}")
def patch_group(group_id: int, body: GroupPatch) -> dict[str, Any]:
    try:
        body.validated_anchor()
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    with Session(engine) as s:
        g = _get_group(s, group_id)
        if g.revision != body.base_revision:
            raise HTTPException(
                409,
                {
                    "error": "revision_conflict",
                    "detail": (
                        f"该编辑基于旧修订号 {body.base_revision}，组当前修订号为 {g.revision}；"
                        "已拒绝覆盖。请重新读取当前成员与参数后再提交。"
                    ),
                    "current_revision": g.revision,
                },
            )
        if body.name is not None:
            g.name = body.name
        if body.description is not None:
            g.description = body.description
        if body.anchor_event_type is not None:
            g.anchor_event_type = body.anchor_event_type
        for field_name in (
            "ror_window_s",
            "display_smooth_s",
            "max_gap_fill_s",
            "grid_step_s",
            "anchor_support_tolerance_s",
        ):
            v = getattr(body, field_name)
            if v is not None:
                setattr(g, field_name, v)
        if body.batch_ids is not None:
            _set_members(s, g, body.batch_ids)
        g.revision += 1
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise HTTPException(409, "成员约束冲突（可能与并发编辑重复），未保存。")
        s.refresh(g)
        return _group_summary(s, g)


def _publish_snapshot(
    session: Session, g: BatchGroup, *, note: str, created_by: str
) -> GroupSnapshot:
    existing_versions = list(
        session.scalars(
            select(GroupSnapshot.version)
            .where(GroupSnapshot.group_id == g.id)
            .order_by(GroupSnapshot.version)
        )
    )
    version = (max(existing_versions) + 1) if existing_versions else 1
    params = _group_params(g)
    blobs = [_member_blob(session, m.batch_id) for m in g.members]
    doc = build_snapshot_document(
        group_id=g.id,
        group_name=g.name,
        description=g.description,
        anchor_event_type=g.anchor_event_type,
        params=params,
        version=version,
        base_revision=g.revision,
        member_blobs=blobs,
        created_by=created_by,
        created_at=g.updated_at,
        note=note,
    )
    row = GroupSnapshot(
        group_id=g.id,
        version=version,
        base_revision=g.revision,
        note=note,
        created_by=created_by,
        payload_json=serialize(doc),
    )
    session.add(row)
    session.flush()
    g.latest_snapshot_version = version
    g.revision += 1  # membership state now has a published freeze point
    return row


@app.post("/api/groups/{group_id}/snapshots", status_code=201)
def create_snapshot(group_id: int, body: SnapshotCreate) -> dict[str, Any]:
    with Session(engine) as s:
        g = _get_group(s, group_id)
        if g.revision != body.base_revision:
            raise HTTPException(
                409,
                {
                    "error": "revision_conflict",
                    "detail": (
                        f"快照基于旧修订号 {body.base_revision}，组当前修订号为 {g.revision}；"
                        "请先审阅当前成员/参数后再发布快照。"
                    ),
                    "current_revision": g.revision,
                },
            )
        row = _publish_snapshot(s, g, note=body.note, created_by=body.created_by)
        s.commit()
        s.refresh(row)
        doc = deserialize(row.payload_json)
        return {"snapshot_id": row.id, "document": doc}


@app.get("/api/groups/{group_id}/snapshots")
def list_snapshots(group_id: int) -> list[dict[str, Any]]:
    with Session(engine) as s:
        g = _get_group(s, group_id)
        event_cache = _current_event_cache(s, g)
        current_batches = [s.get(Batch, m.batch_id) for m in g.members]
        out = []
        for row in g.snapshots:
            doc = deserialize(row.payload_json)
            out.append(
                {
                    "snapshot_id": row.id,
                    "version": row.version,
                    "base_revision": row.base_revision,
                    "note": row.note,
                    "created_by": row.created_by,
                    "created_at": row.created_at.isoformat(),
                    "included_batch_ids": doc["result"]["included_batch_ids"],
                    "excluded_batch_ids": doc["result"]["excluded_batch_ids"],
                    "n_aggregate_points": len(doc["result"]["aggregate"]["points"]),
                    "staleness": snapshot_staleness(
                        doc,
                        current_group=g,
                        current_batches=current_batches,
                        current_events_by_cache=event_cache,
                    ),
                }
            )
        return out


def _snapshot_row(session: Session, group_id: int, version: int) -> GroupSnapshot:
    row = session.scalar(
        select(GroupSnapshot).where(
            GroupSnapshot.group_id == group_id,
            GroupSnapshot.version == version,
        )
    )
    if row is None:
        raise HTTPException(404, f"snapshot v{version} of group {group_id} not found")
    return row


def _snapshot_replay(session: Session, g: BatchGroup, row: GroupSnapshot) -> dict[str, Any]:
    """Replay a frozen report and attach current staleness.  The document is
    never modified — replay and export rebuild from the same bytes."""
    doc = deserialize(row.payload_json)
    # Fresh rebuild from the frozen document (proves replay determinism).
    rebuilt = recompute_from_snapshot(doc)
    frozen_identical = serialize(rebuilt) == serialize(doc["result"])
    staleness = snapshot_staleness(
        doc,
        current_group=g,
        current_batches=[session.get(Batch, m.batch_id) for m in g.members],
        current_events_by_cache=_current_event_cache(session, g),
    )
    return {
        "document": doc,
        "replay": {
            "rebuilt_from_frozen_samples_events": True,
            "result_identical": frozen_identical,
            "staleness_vs_current_group": staleness,
        },
    }


@app.get("/api/groups/{group_id}/snapshots/{version}")
def replay_snapshot(group_id: int, version: int) -> dict[str, Any]:
    with Session(engine) as s:
        g = _get_group(s, group_id)
        row = _snapshot_row(s, group_id, version)
        return _snapshot_replay(s, g, row)


@app.get("/api/groups/{group_id}/snapshots/{version}/export")
def export_snapshot(group_id: int, version: int) -> dict[str, Any]:
    """Self-contained snapshot export.  POST it verbatim to
    /api/groups/recompute to reproduce every member, exclusion and band."""
    with Session(engine) as s:
        g = _get_group(s, group_id)
        row = _snapshot_row(s, group_id, version)
        doc = deserialize(row.payload_json)
        doc["export_kind"] = "batch_group_snapshot"
        doc["export_rebuild_endpoint"] = "/api/groups/recompute"
        return doc


@app.post("/api/groups/recompute")
def groups_recompute(doc: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a group result from an exported snapshot document.

    No database access: members, frozen anchor versions, exclusions and params
    all come from the document.  A result containing synthetic members carries
    the same provenance markers and disclaimer as the original report.
    """
    for key in ("members_freeze", "anchor_event_type", "params"):
        if key not in doc:
            raise HTTPException(422, f"snapshot document missing field: {key}")
    result = recompute_from_snapshot(doc)
    frozen = doc.get("result")
    return {
        "doc": doc.get("doc"),
        "snapshot_doc_version": doc.get("snapshot_doc_version"),
        "group_id": doc.get("group_id"),
        "version": doc.get("version"),
        "result": result,
        "identical_to_frozen": (
            serialize(result) == serialize(frozen) if frozen is not None else None
        ),
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_connection": "none (synthetic/offline only)"}
