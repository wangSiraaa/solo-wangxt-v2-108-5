"""Batch-group analysis: anchor alignment, honest median bands, snapshots.

The aggregation contract
------------------------
A group aligns 3--8 batches on one explicit event anchor (e.g. first crack).
For each **included** member we take that member's *raw measured* bean
samples (never interpolated points) on anchor-relative time ``tau``.  A common
grid (default 10 s) is evaluated: a grid point is emitted only when **every**
included member has a non-interpolated measured sample within
``grid_step_s / 2`` of it.  At emitted points the value is the cross-sectional
median of the members' measured temperatures, with a q1--q3 dispersion band.
Where even one member lacks support the aggregate is simply absent — nothing is
bridged, imputed or extrapolated.

Members are excluded at snapshot time, never silently:

* ``missing_anchor``       — the member has no current event of the anchor type;
* ``anchor_in_long_gap``   — no measured sample lies within
  ``anchor_support_tolerance_s`` of the anchor, i.e. the anchor sits inside a
  long probe dropout.  A linear interpolation across that gap is *not* allowed
  to rescue the alignment.

Every published snapshot freezes: member set, exact anchor **event versions**
(by event id), exclusion reasons, parameters, raw samples and the full event
history.  Replay/export/recompute read that document, so later membership edits
or manual event corrections can never mutate an existing report — they only
mark the *current* group as stale relative to its latest snapshot.

Synthetic provenance
--------------------
A generated control batch stays visibly synthetic (``data_origin`` /
``is_control_batch`` / generator seed+version) all the way through snapshots,
legend labels, exports and recomputation; any result that contains one carries
an explicit "local synthetic demo — not a real stability conclusion" marker and
is never presented as coming from a real roaster or an upload.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import numpy as np

from .analysis import RoRConfig, build_series, current_events
from .models import SYNTHETIC_DISCLAIMER

# Event types a group may align on (all are sourced, correctable events).
ALLOWED_ANCHORS = (
    "turning_point",
    "first_crack_start",
    "first_crack_end",
    "drop",
)

MIN_MEMBERS = 3
MAX_MEMBERS = 8

SNAPSHOT_SCHEMA = "roast-group-snapshot/1"
SNAPSHOT_DOC_VERSION = 1

NON_CAUSAL_NOTE = (
    "组内离散带是成员间横截面分布，无随机对照、无重复、无统计检验；"
    "仅用于观察组内一致性，不构成因果结论，也不构成真实稳定性结论。"
)


@dataclass(frozen=True)
class GroupParams:
    ror_window_s: float = 30.0
    display_smooth_s: float = 12.0
    max_gap_fill_s: float = 45.0
    grid_step_s: float = 10.0
    anchor_support_tolerance_s: float = 5.0

    def ror_config(self) -> RoRConfig:
        return RoRConfig(
            window_s=self.ror_window_s,
            display_smooth_s=self.display_smooth_s,
        )


# ---------------------------------------------------------------------------
# pure analysis
# ---------------------------------------------------------------------------

def _measured_bean_times(samples: list[dict]) -> np.ndarray:
    return np.array(
        sorted(s["t_s"] for s in samples if s.get("bean_temp_c") is not None),
        dtype=float,
    )


def anchor_support(
    samples: list[dict], anchor_t_s: float, max_gap_fill_s: float, tolerance_s: float
) -> dict[str, Any]:
    """Describe the raw-sample support around an anchor timestamp.

    No interpolation participates: we only look at measured samples.
    """
    t_m = _measured_bean_times(samples)
    if len(t_m) == 0:
        return {
            "nearest_measured_delta_s": None,
            "left_measured_t_s": None,
            "right_measured_t_s": None,
            "bracketing_gap_s": None,
            "tolerance_s": tolerance_s,
            "max_gap_fill_s": max_gap_fill_s,
        }
    deltas = np.abs(t_m - anchor_t_s)
    nearest_idx = int(np.argmin(deltas))
    left = t_m[t_m <= anchor_t_s]
    right = t_m[t_m >= anchor_t_s]
    left_t = float(left.max()) if len(left) else None
    right_t = float(right.min()) if len(right) else None
    bracket = (
        round(right_t - left_t, 3)
        if left_t is not None and right_t is not None
        else None
    )
    return {
        "nearest_measured_delta_s": round(float(deltas[nearest_idx]), 3),
        "left_measured_t_s": left_t,
        "right_measured_t_s": right_t,
        "bracketing_gap_s": bracket,
        "tolerance_s": tolerance_s,
        "max_gap_fill_s": max_gap_fill_s,
    }


def _round_or_none(v: Any, nd: int = 3) -> Any:
    if v is None:
        return None
    f = float(v)
    if np.isnan(f):
        return None
    return round(f, nd)


def analyze_member(blob: dict[str, Any], anchor_type: str, params: GroupParams) -> dict[str, Any]:
    """Build one member record: anchor version, support decision, full series.

    ``blob`` keys:
      batch   – batch metadata dict (provenance included)
      samples – raw sample dicts (bean_temp_c NULL = dropout)
      events  – event dicts (history allowed)
      frozen_anchor_event_id – optional: replay a snapshot by pinning this exact
                               event version even if it has since been superseded
    """
    batch = blob["batch"]
    samples = blob["samples"]
    events = blob.get("events", [])
    frozen_id = blob.get("frozen_anchor_event_id")

    # --- choose the anchor event version ------------------------------------
    anchor_event: dict | None = None
    if frozen_id is not None:
        anchor_event = next((e for e in events if e["id"] == frozen_id), None)
    if anchor_event is None:
        anchor_event = current_events(events).get(anchor_type)

    series = build_series(
        samples,
        ror_cfg=params.ror_config(),
        max_gap_fill_s=params.max_gap_fill_s,
    )

    included = False
    reason_code: str | None = None
    reason: str | None = None
    support: dict[str, Any] | None = None
    anchor_out: dict[str, Any] | None = None

    if anchor_event is None:
        reason_code = "missing_anchor"
        reason = (
            f"缺少当前「{anchor_type}」事件锚点；不猜测锚点，禁止用插值对齐，"
            "该成员排除出聚合。"
        )
    else:
        anchor_t = float(anchor_event["t_s"])
        support = anchor_support(
            samples,
            anchor_t,
            max_gap_fill_s=params.max_gap_fill_s,
            tolerance_s=params.anchor_support_tolerance_s,
        )
        anchor_out = {
            "event_id": anchor_event["id"],
            "event_type": anchor_event["event_type"],
            "t_s": round(anchor_t, 3),
            "source": anchor_event.get("source"),
            "created_by": anchor_event.get("created_by"),
            "label": anchor_event.get("label", ""),
            "superseded": bool(anchor_event.get("superseded", False)),
            "selection": "frozen_event_version" if frozen_id is not None else "current_event",
        }
        nearest = support["nearest_measured_delta_s"]
        if nearest is None or nearest > params.anchor_support_tolerance_s:
            reason_code = "anchor_in_long_gap"
            reason = (
                f"锚点 t={anchor_t:g}s 附近 ±{params.anchor_support_tolerance_s:g}s "
                f"内没有非插值原始实测豆温样本（最近实测点距离 "
                f"{nearest if nearest is None else format(nearest, '.2f') + 's'}，"
                f"跨接实测点缺口 {support['bracketing_gap_s']}s "
                f"> max_gap_fill_s={params.max_gap_fill_s:g}s）；"
                "判定为长断档覆盖锚点，不以线性插值硬补，该成员排除出聚合。"
            )
        else:
            included = True

    # Anchor-relative measured support window (for chart/audit).
    support_window: dict[str, Any] = {"lo_tau_s": None, "hi_tau_s": None}
    if anchor_out is not None:
        t_m = _measured_bean_times(samples)
        if len(t_m):
            support_window = {
                "lo_tau_s": round(float(t_m.min() - anchor_out["t_s"]), 3),
                "hi_tau_s": round(float(t_m.max() - anchor_out["t_s"]), 3),
            }

    return {
        "batch_id": batch["id"],
        "batch_name": batch["name"],
        "bean": batch.get("bean", ""),
        "roaster": batch.get("roaster", ""),
        "data_origin": batch.get("data_origin"),
        "is_local_synthetic": bool(batch.get("is_local_synthetic", False)),
        "is_control_batch": bool(batch.get("is_control_batch", False)),
        "generator": {
            "seed": batch.get("generator_seed"),
            "version": batch.get("generator_version"),
        },
        "included": included,
        "exclusion_reason_code": reason_code,
        "exclusion_reason": reason,
        "anchor": anchor_out,
        "anchor_support": support,
        "support_window_tau": support_window,
        # Every original curve, interpolation flags and long-gap identity are
        # retained per member — raw curve switching never needs a second call.
        "series": series,
    }


def aggregate_members(
    members: list[dict[str, Any]], params: GroupParams
) -> dict[str, Any]:
    """Median trend + q1/q3 dispersion on the strict common-support grid."""
    included = [m for m in members if m["included"]]
    included = sorted(included, key=lambda m: m["batch_id"])

    # tau -> per-member nearest measured value, kept only when every member
    # has a measured sample within grid_step_s / 2.
    per_member_pts: list[tuple[int, np.ndarray, np.ndarray]] = []
    los, his = [], []
    for m in included:
        anchor_t = m["anchor"]["t_s"]
        pts = [
            (p["t_s"] - anchor_t, p["bean_temp_c"])
            for p in m["series"]["raw_points"]
            if p["bean_temp_c"] is not None  # raw measured only; interp stays NULL
        ]
        tau = np.array([p[0] for p in pts], dtype=float)
        val = np.array([p[1] for p in pts], dtype=float)
        order = np.argsort(tau)
        tau, val = tau[order], val[order]
        per_member_pts.append((m["batch_id"], tau, val))
        los.append(float(tau.min()))
        his.append(float(tau.max()))

    points: list[dict[str, Any]] = []
    member_columns: list[dict[str, Any]] = []
    common_lo = common_hi = None
    if included:
        common_lo = max(los)
        common_hi = min(his)
        half = params.grid_step_s / 2.0
        g_lo = np.ceil(common_lo / params.grid_step_s) * params.grid_step_s
        g_hi = np.floor(common_hi / params.grid_step_s) * params.grid_step_s
        n_grid = int(round((g_hi - g_lo) / params.grid_step_s)) + 1 if g_hi >= g_lo else 0
        for k in range(n_grid):
            tau = round(float(g_lo + k * params.grid_step_s), 3)
            chosen: list[tuple[int, float, float]] = []
            supported = True
            for batch_id, t_arr, v_arr in per_member_pts:
                j = int(np.argmin(np.abs(t_arr - tau)))
                dist = abs(float(t_arr[j]) - tau)
                if dist > half + 1e-9:
                    supported = False
                    break
                chosen.append((batch_id, float(v_arr[j]), round(dist, 3)))
            if not supported:
                continue
            values = np.array([c[1] for c in chosen], dtype=float)
            q1 = float(np.percentile(values, 25, method="linear"))
            med = float(np.percentile(values, 50, method="linear"))
            q3 = float(np.percentile(values, 75, method="linear"))
            points.append(
                {
                    "tau_s": tau,
                    "median_c": round(med, 3),
                    "q1_c": round(q1, 3),
                    "q3_c": round(q3, 3),
                    "iqr_c": round(q3 - q1, 3),
                    "n": len(chosen),
                }
            )
            for batch_id, value, dist in chosen:
                member_columns.append(
                    {
                        "batch_id": batch_id,
                        "tau_s": tau,
                        "bean_temp_c": round(value, 3),
                        "sample_distance_s": float(dist),
                    }
                )

    return {
        "anchor_relative_time_basis": "tau_s = t_s - anchor_event.t_s (seconds)",
        "grid_step_s": params.grid_step_s,
        "support_tolerance_s": params.grid_step_s / 2.0,
        "common_support_window_tau": {
            "lo_tau_s": _round_or_none(common_lo),
            "hi_tau_s": _round_or_none(common_hi),
        },
        "n_included_members": len(included),
        "points": points,
        "member_values": member_columns,
        "method": {
            "median": "cross-sectional median of members' RAW MEASURED bean temps at tau",
            "band_q1_q3": (
                "25th/75th percentiles across members (numpy linear); this is a "
                "cross-section summary, never a time interpolation"
            ),
            "support_rule": (
                "point emitted only when EVERY included member has a "
                "non-interpolated measured sample within grid_step_s/2; "
                "otherwise the aggregate is absent (no bridging, no imputation)"
            ),
        },
    }


def provenance(members: list[dict[str, Any]]) -> dict[str, Any]:
    synth = [m for m in members if m["is_local_synthetic"]]
    origins = sorted({m["data_origin"] for m in synth if m.get("data_origin")})
    return {
        "machine_connected": False,
        "upload_data_present": False,
        "contains_local_synthetic": bool(synth),
        "contains_local_synthetic_control": any(m["is_control_batch"] for m in synth),
        "synthetic_member_batch_ids": [m["batch_id"] for m in synth],
        "synthetic_origins": origins,
        "disclaimer": SYNTHETIC_DISCLAIMER if synth else None,
    }


def analyze_group(
    member_blobs: list[dict[str, Any]],
    *,
    anchor_event_type: str,
    params: GroupParams,
) -> dict[str, Any]:
    """Pure group analysis.  Inputs only — no DB access (used by live view,
    snapshot creation, replay and independent recompute)."""
    members = [analyze_member(blob, anchor_event_type, params) for blob in member_blobs]
    members = sorted(members, key=lambda m: m["batch_id"])
    agg = aggregate_members(members, params)
    included = [m for m in members if m["included"]]
    excluded = [m for m in members if not m["included"]]

    interpretation = (
        f"各批次按明确事件锚点「{anchor_event_type}」时刻对齐（tau=0）。"
        "中位趋势与 q1–q3 离散带只在所有纳入成员在该锚点相对时刻"
        "均有非插值原始实测样本支持的网格区间给出；任一成员缺测的区间不输出聚合值，"
        "绝不插值或外推。" + NON_CAUSAL_NOTE
    )

    return {
        "anchor_event_type": anchor_event_type,
        "params": asdict(params),
        "members": members,
        "included_batch_ids": [m["batch_id"] for m in included],
        "excluded_batch_ids": [m["batch_id"] for m in excluded],
        "exclusions": [
            {
                "batch_id": m["batch_id"],
                "batch_name": m["batch_name"],
                "reason_code": m["exclusion_reason_code"],
                "reason": m["exclusion_reason"],
                "anchor": m["anchor"],
                "anchor_support": m["anchor_support"],
            }
            for m in excluded
        ],
        "aggregate": agg,
        "provenance": provenance(members),
        "interpretation": interpretation,
        "non_causal_note": NON_CAUSAL_NOTE,
    }


# ---------------------------------------------------------------------------
# snapshot documents
# ---------------------------------------------------------------------------

def freeze_member(member: dict[str, Any], blob: dict[str, Any], position: int) -> dict[str, Any]:
    """The immutable per-member section of a snapshot document."""
    return {
        "position": position,
        "batch_id": member["batch_id"],
        # Full batch metadata as it was at freeze time.
        "batch_meta": blob["batch"],
        "batch_name": member["batch_name"],
        "data_origin": member["data_origin"],
        "is_local_synthetic": member["is_local_synthetic"],
        "is_control_batch": member["is_control_batch"],
        "generator": member["generator"],
        "included": member["included"],
        "exclusion_reason_code": member["exclusion_reason_code"],
        "exclusion_reason": member["exclusion_reason"],
        # Exact event version pinned for replay (None only when it was missing).
        "anchor_event_id": member["anchor"]["event_id"] if member["anchor"] else None,
        # Raw source data so the document rebuilds offline.
        "samples": blob["samples"],
        "events": blob.get("events", []),
    }


def build_snapshot_document(
    *,
    group_id: int,
    group_name: str,
    description: str,
    anchor_event_type: str,
    params: GroupParams,
    version: int,
    base_revision: int,
    member_blobs: list[dict[str, Any]],
    created_by: str,
    created_at: datetime,
    note: str,
) -> dict[str, Any]:
    # Pass 1: resolve current events to decide inclusions/exclusions.
    probe = analyze_group(
        member_blobs, anchor_event_type=anchor_event_type, params=params
    )
    anchor_by_batch = {
        m["batch_id"]: (m["anchor"]["event_id"] if m["anchor"] else None)
        for m in probe["members"]
    }
    # Pass 2 (canonical): pin the exact event versions.  This is the same
    # function recompute/replay runs, so stored and rebuilt results are
    # byte-identical after a JSON round-trip.
    frozen_blobs = []
    for blob in member_blobs:
        b2 = dict(blob)
        b2["frozen_anchor_event_id"] = anchor_by_batch[blob["batch"]["id"]]
        frozen_blobs.append(b2)
    result = analyze_group(
        frozen_blobs, anchor_event_type=anchor_event_type, params=params
    )
    by_id = {blob["batch"]["id"]: blob for blob in member_blobs}
    freezes = []
    for pos, m in enumerate(result["members"]):
        freezes.append(freeze_member(m, by_id[m["batch_id"]], pos))

    return {
        "doc": SNAPSHOT_SCHEMA,
        "snapshot_doc_version": SNAPSHOT_DOC_VERSION,
        "group_id": group_id,
        "group_name": group_name,
        "group_description": description,
        "version": version,
        "base_revision": base_revision,
        "created_by": created_by,
        "created_at": created_at.isoformat(),
        "note": note,
        "anchor_event_type": anchor_event_type,
        "params": asdict(params),
        "members_freeze": freezes,
        "result": result,
        "provenance": result["provenance"],
        "interpretation": result["interpretation"],
        "non_causal_note": NON_CAUSAL_NOTE,
        "reproducibility": {
            "rebuild": (
                "POST /api/groups/recompute with this document: analysis is "
                "re-derived from members_freeze[].samples/events and the pinned "
            "anchor_event_id; the aggregate is identical byte-for-byte."
            ),
            "raw_samples_are_source_of_truth": True,
            "interpolated_points_never_count_as_support": True,
            "deterministic": True,
        },
    }


def blobs_from_snapshot(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Reconstruct analyze_group inputs from a frozen snapshot document."""
    blobs = []
    for f in doc["members_freeze"]:
        meta = f.get("batch_meta") or {}
        blobs.append(
            {
                "batch": {
                    "id": f["batch_id"],
                    "name": f["batch_name"],
                    "bean": meta.get("bean", ""),
                    "roaster": meta.get("roaster", ""),
                    "data_origin": f["data_origin"],
                    "is_local_synthetic": f["is_local_synthetic"],
                    "is_control_batch": f["is_control_batch"],
                    "generator_seed": (f.get("generator") or {}).get("seed"),
                    "generator_version": (f.get("generator") or {}).get("version"),
                },
                "samples": f["samples"],
                "events": f["events"],
                "frozen_anchor_event_id": f["anchor_event_id"],
            }
        )
    return blobs


def recompute_from_snapshot(doc: dict[str, Any]) -> dict[str, Any]:
    """Independent rebuild of the result section from the frozen document."""
    params = GroupParams(**doc["params"])
    return analyze_group(
        blobs_from_snapshot(doc),
        anchor_event_type=doc["anchor_event_type"],
        params=params,
    )


def serialize(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def deserialize(text: str) -> dict[str, Any]:
    return json.loads(text)


# ---------------------------------------------------------------------------
# staleness
# ---------------------------------------------------------------------------

def snapshot_staleness(doc: dict[str, Any], *, current_group, current_batches, current_events_by_cache):
    """Compare a frozen snapshot with the current DB state.

    Returns {"stale": bool, "reasons": [...]}.  Never mutates the snapshot.
    """
    reasons: list[dict[str, Any]] = []

    frozen_ids = [f["batch_id"] for f in doc["members_freeze"]]
    current_ids = [b.id for b in current_batches]
    if sorted(frozen_ids) != sorted(current_ids):
        reasons.append(
            {
                "code": "members_changed",
                "detail": {
                    "snapshot_batch_ids": sorted(frozen_ids),
                    "current_batch_ids": sorted(current_ids),
                    "added": sorted(set(current_ids) - set(frozen_ids)),
                    "removed": sorted(set(frozen_ids) - set(current_ids)),
                },
            }
        )

    if current_group.anchor_event_type != doc["anchor_event_type"]:
        reasons.append(
            {
                "code": "anchor_type_changed",
                "detail": {
                    "snapshot": doc["anchor_event_type"],
                    "current": current_group.anchor_event_type,
                },
            }
        )

    current_params = {
        "ror_window_s": current_group.ror_window_s,
        "display_smooth_s": current_group.display_smooth_s,
        "max_gap_fill_s": current_group.max_gap_fill_s,
        "grid_step_s": current_group.grid_step_s,
        "anchor_support_tolerance_s": current_group.anchor_support_tolerance_s,
    }
    if current_params != doc["params"]:
        reasons.append(
            {"code": "params_changed", "detail": {"snapshot": doc["params"], "current": current_params}}
        )

    for f in doc["members_freeze"]:
        anchor_id = f["anchor_event_id"]
        if anchor_id is None:
            continue
        ev = current_events_by_cache.get(f["batch_id"], {}).get(anchor_id)
        if ev is None:
            reasons.append(
                {
                    "code": "anchor_event_missing",
                    "detail": {"batch_id": f["batch_id"], "event_id": anchor_id},
                }
            )
        elif ev.superseded:
            reasons.append(
                {
                    "code": "anchor_event_corrected",
                    "detail": {
                        "batch_id": f["batch_id"],
                        "batch_name": f["batch_name"],
                        "event_id": anchor_id,
                        "old_t_s": ev.t_s,
                        "superseded_by_id": ev.superseded_by_id,
                    },
                }
            )

    return {"stale": bool(reasons), "reasons": reasons}
