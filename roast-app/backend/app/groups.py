"""Batch-group analysis: anchor-aligned median trend and dispersion bands.

This module is a set of *pure functions* over raw sample dicts and event
dicts.  Nothing here touches the database, and the same entry point
(:func:`analyze_group`) powers live previews, frozen snapshot results and
independent replay of an exported snapshot — so "recompute from the export"
genuinely exercises the same calculation rather than a second implementation.

Calculation basis (frozen into every snapshot)
----------------------------------------------
* Members are aligned on one explicitly chosen **event anchor** per batch
  (default ``first_crack_start``).  Time is re-expressed as
  ``τ = t − anchor_t``; the frozen anchor records the exact event row id and
  ``source`` (auto/manual) used — that is the anchor *version*.
* The aggregate grid runs at a fixed step over the intersection of the
  included members' anchor-relative domains.
* At every grid point each included member contributes **only when a
  non-interpolated, measured bean sample exists within
  ``support_tolerance_s``** of that point.  Interpolated samples are never
  accepted as support and never create a median point.  The median and the
  dispersion band are emitted only at grid points where **every** included
  member has such raw support; elsewhere the band breaks (``None``) instead
  of being bridged — long missing runs keep their identity.
* The dispersion band is the empirical inter-quartile range (q25/q75) plus
  min/max across the members.  With 3--8 members this is an *observational
  spread*, not a confidence interval, and every payload says so explicitly.
* Members that cannot honestly be aligned are **excluded with a reason**,
  never silently dropped and never replaced by interpolation:

    ============================== =========================================
    code                           meaning
    ============================== =========================================
    missing_anchor                 the batch has no current event of the
                                   chosen anchor type
    anchor_in_long_gap             anchor time lies inside a bean-temp gap
                                   wider than ``max_gap_fill_s`` (the guide
                                   curve is broken there)
    anchor_in_interpolated_gap     anchor sits inside a short gap whose only
                                   coverage is flagged interpolation
    anchor_without_raw_support     no *measured* bean sample within
                                   ``support_tolerance_s`` of the anchor
    ============================== =========================================

Synthetic provenance is carried alongside every result; a result containing
the deterministic control batch is labelled a local synthetic demo and must
not be presented as a real stability conclusion.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass

import numpy as np

from .analysis import RoRConfig, build_series, current_events
from .models import PROV_CONTROL

# Anchor types a group may align on.  Each must exist in EVENT_EVENT_KEYS-ish
# vocabulary used by the event table; damper_change is deliberately absent
# (multiple discrete events, not a single phase boundary).
SUPPORTED_ANCHORS = (
    "charge",
    "turning_point",
    "first_crack_start",
    "first_crack_end",
    "drop",
)

NON_CAUSAL_NOTE = (
    "中位趋势与离散带仅按所选事件锚点对齐展示组内批次的观察形态，"
    "样本量 3–8、无随机对照、无统计检验，不构成稳定性或因果结论；"
    "聚合曲线不是任何一次真实测量。"
)


@dataclass(frozen=True)
class GroupParams:
    """All parameters that define a group calculation.  Frozen into specs."""

    anchor_event: str = "first_crack_start"
    ror_window_s: float = 30.0
    ror_display_smooth_s: float = 12.0
    max_gap_fill_s: float = 45.0
    grid_step_s: float = 5.0
    support_tolerance_s: float = 3.0

    def ror_config(self) -> RoRConfig:
        return RoRConfig(
            window_s=self.ror_window_s,
            display_smooth_s=self.ror_display_smooth_s,
        )

    def as_dict(self) -> dict:
        return asdict(self)


def params_from_dict(d: dict) -> GroupParams:
    """Tolerant parse: unknown keys ignored, missing keys take defaults."""
    allowed = {f for f in GroupParams.__dataclass_fields__}  # type: ignore[attr-defined]
    vals = {k: v for k, v in (d or {}).items() if k in allowed}
    return GroupParams(**vals)


# ---------------------------------------------------------------------------
# hashing — stable, locale-independent identity for spec / result payloads
# ---------------------------------------------------------------------------

def canonical_json(obj) -> str:
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def sha256_of(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _round(v, ndigits: int):
    if v is None:
        return None
    f = float(v)
    if np.isnan(f):
        return None
    return round(f, ndigits)


# ---------------------------------------------------------------------------
# per-member preparation
# ---------------------------------------------------------------------------

def _bean_gap_lookup(missing_segments: list[dict]) -> list[dict]:
    return [g for g in missing_segments if g.get("channel") == "bean"]


def anchor_exclusion_reason(
    anchor_t: float | None,
    series: dict,
    params: GroupParams,
) -> tuple[str | None, dict | None]:
    """Return (reason_code, detail) — ``(None, None)`` when the member is fine.

    The decision uses only measured samples: gap records describe runs of NULL
    bean temps, and the support check rejects interpolated samples outright.
    """
    if anchor_t is None:
        return "missing_anchor", {}

    pts = series["raw_points"]
    measured_t = np.array(
        [p["t_s"] for p in pts if p["bean_temp_c"] is not None and not p["is_interpolated"]],
        dtype=float,
    )

    # 1) Anchor inside a run of missing bean samples?  Gap records carry the
    #    measured-neighbour span, which is exactly what interpolation (or its
    #    refusal) would bridge.
    for g in _bean_gap_lookup(series["missing_segments"]):
        lo, hi = g["t_start_s"], g["t_end_s"]
        nspan = g.get("neighbour_span_s")
        left_bound = lo
        right_bound = hi
        # Half-open-ish containment; equality with a recorded missing sample
        # time means the anchor itself has no measurement.
        if left_bound <= anchor_t <= right_bound:
            detail = {
                "gap_start_s": round(lo, 3),
                "gap_end_s": round(hi, 3),
                "neighbour_span_s": round(nspan, 3) if nspan is not None else None,
                "n_missing": g.get("n_missing"),
                "max_gap_fill_s": params.max_gap_fill_s,
            }
            if g["status"] == "interpolated":
                return "anchor_in_interpolated_gap", detail
            return "anchor_in_long_gap", detail

    # 2) Raw support near the anchor: at least one genuinely measured bean
    #    sample inside the stated tolerance.  Interpolated guides cannot
    #    vouch for the anchor.
    if len(measured_t):
        nearest = float(np.min(np.abs(measured_t - anchor_t)))
    else:
        nearest = float("inf")
    if nearest > params.support_tolerance_s:
        return "anchor_without_raw_support", {
            "nearest_measured_distance_s": round(nearest, 3)
            if np.isfinite(nearest)
            else None,
            "support_tolerance_s": params.support_tolerance_s,
        }
    return None, None


def _resolve_anchor(events: list[dict], anchor_type: str) -> dict | None:
    """The single current (non-superseded) anchor event, frozen by row id."""
    norm = [
                {**e, "superseded": bool(e.get("superseded", False))} for e in events
            ]
    cur = current_events(norm)
    ev = cur.get(anchor_type)
    if ev is None:
        return None
    return {
        "event_id": ev.get("id"),
        "event_type": anchor_type,
        "t_s": float(ev["t_s"]),
        "source": ev.get("source"),
        "created_by": ev.get("created_by"),
        "label": ev.get("label", ""),
        "superseded": False,
    }


def _aligned_member_curve(points: list[dict], anchor_t: float) -> dict:
    """Project one member's full series onto anchor-relative time.

    Measured values, interpolation markers and the long-gap breaks are all
    preserved so the chart can render each original (never aggregated away)
    curve faithfully: raw_bean excludes interpolated points, guide_bean is
    the measured+flagged-interpolated line with None at unfilled gaps, env and
    ror traces ride alongside.
    """
    tau, raw_bean, guide_bean, env_vals, ror_raw, ror_disp, flags = [], [], [], [], [], [], []
    for p in points:
        t = round(float(p["t_s"]) - anchor_t, 3)
        bean = p["bean_temp_c"]
        tau.append(t)
        raw_bean.append(bean if bean is not None and not p["is_interpolated"] else None)
        # guide value: measured bean, or flagged interpolation, or None break
        guide_bean.append(bean)
        env_vals.append(p["env_temp_c"])
        ror_raw.append(p["ror_c_per_min"])
        ror_disp.append(p["ror_display"])
        flags.append({
            "t_s_rel": t,
            "is_interpolated": bool(p["is_interpolated"]),
            "ror_edge": bool(p.get("ror_edge", False)),
        })
    return {
        "t_rel_s": tau,
        "raw_bean_c": raw_bean,
        "guide_bean_c": guide_bean,
        "env_c": env_vals,
        "ror_raw": ror_raw,
        "ror_display": ror_disp,
        "point_flags": flags,
    }


def _grid_aggregate(
    member_data: list[dict],
    grid: np.ndarray,
    tol: float,
    ndigits: int,
) -> dict:
    """Median + dispersion on a common grid, raw-support gated per member.

    ``member_data`` items are ``{t_rel (ndarray), values (ndarray)}`` where
    NaN means "no valid value at that time".  A member contributes at grid
    point τ only when it has a finite value within ``tol`` seconds; the grid
    point is emitted only when ALL members contribute.
    """
    n = len(member_data)
    contrib = np.full((n, len(grid)), np.nan)
    contrib_t = np.full((n, len(grid)), np.nan)
    for mi, md in enumerate(member_data):
        t = md["t_rel"]
        v = md["values"]
        finite = ~np.isnan(v)
        for gi, tau in enumerate(grid):
            if not finite.any():
                continue
            d = np.abs(t - tau)
            j = int(np.argmin(d))
            if d[j] <= tol and finite[j]:
                contrib[mi, gi] = v[j]
                contrib_t[mi, gi] = t[j]

    all_supported = ~np.isnan(contrib).any(axis=0)
    rows = []
    for gi, tau in enumerate(grid):
        per_member = [
            {
                "t_rel_s": _round(contrib_t[mi, gi], 3),
                "value_c": _round(contrib[mi, gi], 2),
                "supported": bool(np.isfinite(contrib[mi, gi])),
            }
            for mi in range(n)
        ]
        if not all_supported[gi]:
            rows.append({
                "t_rel_s": round(float(tau), 3),
                "median": None,
                "q25": None,
                "q75": None,
                "min": None,
                "max": None,
                "n_supporting": int((~np.isnan(contrib[:, gi])).sum()),
                "fully_supported": False,
                "member_sample_t_rel": [
                    _round(contrib_t[mi, gi], 3) for mi in range(n)
                ],
                "per_member": per_member,
            })
            continue
        vals = contrib[:, gi]
        rows.append({
            "t_rel_s": round(float(tau), 3),
            "median": _round(np.median(vals), ndigits),
            "q25": _round(np.percentile(vals, 25, method="linear"), ndigits),
            "q75": _round(np.percentile(vals, 75, method="linear"), ndigits),
            "min": _round(np.min(vals), ndigits),
            "max": _round(np.max(vals), ndigits),
            "n_supporting": n,
            "fully_supported": True,
            "member_sample_t_rel": [
                _round(contrib_t[mi, gi], 3) for mi in range(n)
            ],
            "per_member": per_member,
        })
    return {"points": rows, "grid_step_s": float(_round(grid[1] - grid[0], 3) if len(grid) > 1 else 0.0)}


# ---------------------------------------------------------------------------
# main pure entry point
# ---------------------------------------------------------------------------

def analyze_member(
    *,
    batch_ref: dict,
    samples: list[dict],
    events: list[dict],
    params: GroupParams,
    current_events_for_chart: list[dict] | None = None,
) -> dict:
    """Build everything one member contributes (series, anchor, exclusion).

    ``events`` drives anchor resolution (full history accepted; only the
    current row is used); ``current_events_for_chart`` is the set of
    non-superseded events projected onto anchor-relative time for chart marks.
    """
    series = build_series(
        samples,
        ror_cfg=params.ror_config(),
        max_gap_fill_s=params.max_gap_fill_s,
    )
    anchor = _resolve_anchor(events, params.anchor_event)
    anchor_t = None if anchor is None else anchor["t_s"]
    reason, detail = anchor_exclusion_reason(anchor_t, series, params)

    out = {
        "batch_id": batch_ref.get("id"),
        "batch_name": batch_ref.get("name"),
        "provenance": batch_ref.get("provenance", {}),
        "anchor": anchor,
        "excluded": reason is not None,
        "exclusion_reason": reason,
        "exclusion_detail": detail,
        "series": series,
        "chart_events": current_events_for_chart or [],
    }
    if reason is None:
        out["aligned"] = _aligned_member_curve(series["raw_points"], anchor_t)
        out["missing_segments"] = series["missing_segments"]
    return out


def analyze_group(spec: dict) -> dict:
    """Deterministic group result from a self-contained spec dict.

    Used identically for live preview, snapshot result caching and offline
    replay of exports (which is why it must only read its argument).
    """
    params = params_from_dict(spec.get("params"))
    if params.anchor_event not in SUPPORTED_ANCHORS:
        raise ValueError(f"unsupported anchor: {params.anchor_event}")

    member_records = []
    for m in spec["members"]:
        chart_events = m.get(
            "events_for_chart", [e for e in m["events"] if not e.get("superseded")]
        )
        member_records.append(
            analyze_member(
                batch_ref={
                    "id": m["batch_id"],
                    "name": m.get("batch_name"),
                    "provenance": m.get("provenance", {}),
                },
                samples=m["samples"],
                events=m["events"],
                params=params,
                current_events_for_chart=chart_events,
            )
        )

    included = [m for m in member_records if not m["excluded"]]
    excluded = [m for m in member_records if m["excluded"]]

    # Frozen exclusion record (member identity + the exact reason/details).
    exclusions = [
        {
            "batch_id": m["batch_id"],
            "batch_name": m["batch_name"],
            "reason": m["exclusion_reason"],
            "detail": m["exclusion_detail"],
        }
        for m in excluded
    ]

    aggregate = None
    if len(included) >= 2:
        bean_inputs, ror_inputs = [], []
        lo = -float("inf")
        hi = float("inf")
        for m in included:
            al = m["aligned"]
            t = np.asarray(al["t_rel_s"], dtype=float)
            bean = np.array(
                [np.nan if v is None else v for v in al["raw_bean_c"]], dtype=float
            )
            ror = np.array(
                [np.nan if v is None else v for v in al["ror_display"]], dtype=float
            )
            bean_inputs.append({"t_rel": t, "values": bean})
            ror_inputs.append({"t_rel": t, "values": ror})
            lo = max(lo, float(np.min(t)))
            hi = min(hi, float(np.max(t)))
        grid = np.arange(lo, hi + 1e-9, params.grid_step_s, dtype=float)
        bean_grid = _grid_aggregate(
            bean_inputs, grid, params.support_tolerance_s, ndigits=2
        )
        ror_grid = _grid_aggregate(
            ror_inputs, grid, params.support_tolerance_s, ndigits=2
        )
        aggregate = {
            "anchor_event": params.anchor_event,
            "time_basis": "t_rel_s = t_s - member_anchor_t_s",
            "grid_step_s": params.grid_step_s,
            "support_tolerance_s": params.support_tolerance_s,
            # Index order for every per_member array inside grid points.
            "member_order": [
                {"batch_id": m["batch_id"], "batch_name": m["batch_name"]}
                for m in included
            ],
            "support_rule": (
                "每个网格点仅当全部入组成员在 ±support_tolerance_s 内各有一个"
                "非插值实测豆温样本时才给中位与离散带；否则该点断档。"
            ),
            "median_method": "numpy median over per-member nearest measured samples",
            "dispersion_method": "empirical q25/q75 (IQR) and min/max across members",
            "dispersion_is_not_confidence_interval": True,
            "n_members": len(included),
            "bean_temp": bean_grid,
            "ror": ror_grid,
        }

    members_out = []
    for m in member_records:
        mo = {
            "batch_id": m["batch_id"],
            "batch_name": m["batch_name"],
            "provenance": m["provenance"],
            "anchor": m["anchor"],
            "excluded": m["excluded"],
            "exclusion_reason": m["exclusion_reason"],
            "exclusion_detail": m["exclusion_detail"],
        }
        if not m["excluded"]:
            anchor_t = m["anchor"]["t_s"]
            mo["curve"] = m["aligned"]
            mo["missing_segments"] = m["missing_segments"]
            mo["events_aligned"] = _events_relative(m["chart_events"], anchor_t)
        members_out.append(mo)

    prov = _spec_provenance(spec, member_records)
    return {
        "schema": "group_result_v1",
        "params": params.as_dict(),
        "n_members_total": len(member_records),
        "n_members_included": len(included),
        "n_members_excluded": len(excluded),
        "members": members_out,
        "exclusions": exclusions,
        "aggregate": aggregate,
        "provenance": prov,
        "interpretation": NON_CAUSAL_NOTE,
        "synthetic_demo_only": prov["contains_synthetic"],
        "demo_warning": prov["demo_warning"],
    }


def _events_relative(chart_events: list[dict], anchor_t: float) -> list[dict]:
    """Project current member events onto anchor-relative time for marks."""
    out = []
    for e in chart_events:
        out.append({
            "event_id": e.get("id"),
            "event_type": e["event_type"],
            "t_s": float(e["t_s"]),
            "t_rel_s": round(float(e["t_s"]) - anchor_t, 3),
            "source": e.get("source"),
            "value_num": e.get("value_num"),
            "label": e.get("label", ""),
        })
    return out


def _spec_provenance(spec: dict, member_records: list[dict]) -> dict:
    members = []
    any_synth = False
    control_present = False
    for m in member_records:
        p = m["provenance"] or {}
        is_syn = bool(p.get("is_synthetic"))
        if is_syn:
            any_synth = True
        if p.get("synthetic_kind") == PROV_CONTROL:
            control_present = True
        members.append({
            "batch_id": m["batch_id"],
            "batch_name": m["batch_name"],
            "is_synthetic": is_syn,
            "synthetic_kind": p.get("synthetic_kind"),
            "generator": p.get("generator"),
        })
    demo_warning = None
    if any_synth:
        if control_present:
            demo_warning = (
                "本组含本地确定性对照批次（synthetic_kind=deterministic_control）："
                "结果为本地合成演示，不是真实稳定性结论，不来自真实烘焙机，也不是上传数据。"
            )
        else:
            demo_warning = "本组全部/部分成员为本地合成批次；结果仅供过程观察演示，不构成真实稳定性结论。"
    return {
        "members": members,
        "contains_synthetic": any_synth,
        "contains_deterministic_control": control_present,
        "synthetic_demo_label": "本地合成演示 · 非真实烘焙机数据 · 不构成稳定性/因果结论"
        if any_synth
        else None,
        "demo_warning": demo_warning,
    }


# ---------------------------------------------------------------------------
# spec construction (API side), replay and staleness
# ---------------------------------------------------------------------------

def batch_provenance(batch) -> dict:
    return {
        "is_synthetic": bool(getattr(batch, "is_synthetic", False)),
        "synthetic_kind": getattr(batch, "synthetic_kind", None),
        "generator": getattr(batch, "generator", None),
    }


def build_spec(
    *,
    group_params: GroupParams,
    members: list[dict],
    created_by: str = "operator",
    group_name: str = "",
) -> dict:
    """Assemble the hashable, exportable, replayable spec.

    ``members`` items need: batch_id, batch_name, provenance dict, samples
    (raw dicts), events (full event dicts incl. history rows).
    """
    payload_members = []
    for m in members:
        samples = copy.deepcopy(m["samples"])
        payload_members.append({
            "batch_id": m["batch_id"],
            "batch_name": m.get("batch_name"),
            "provenance": m.get("provenance", {}),
            "raw_sha256": raw_samples_hash(samples),
            "samples": samples,
            # full event history: the anchor version is the current row, but
            # superseded rows travel along so a replay stays auditable.
            "events": copy.deepcopy(m["events"]),
        })
    return {
        "schema": "group_spec_v1",
        "group_name": group_name,
        "created_by": created_by,
        "params": group_params.as_dict(),
        "members": payload_members,
        "calculation_basis": {
            "median": "per-grid-point median of nearest non-interpolated raw "
                      "bean samples within support_tolerance_s, only when all "
                      "included members support the point",
            "dispersion": "empirical q25/q75 and min/max across members; not a "
                          "confidence interval",
            "alignment": "t_rel = t - current anchor event t_s, frozen by event id",
            "exclusion": "missing_anchor | anchor_in_long_gap | "
                         "anchor_in_interpolated_gap | anchor_without_raw_support",
            "interpolation": "interpolated samples are flagged guides only; "
                             "never used for anchor support or aggregation",
        },
    }


def replay_result(spec: dict) -> dict:
    """Recompute a result purely from a spec (offline; no database access).

    The spec carries each member's full event history; chart marks use the
    current (non-superseded) rows.  Deep copy keeps the caller's spec intact.
    """
    spec = copy.deepcopy(spec)
    for m in spec["members"]:
        m.setdefault(
            "events_for_chart", [e for e in m["events"] if not e.get("superseded")]
        )
    return analyze_group(spec)


def staleness_reasons(live: dict, frozen_spec: dict) -> list[str]:
    """Compare current group state against a frozen snapshot spec.

    ``live`` shape: {"member_batch_ids": [..], "members": {batch_id: {
        anchor_event_id, anchor_t_s, raw_sha256}}, "params": {...}}
    """
    reasons: list[str] = []
    frozen_ids = [m["batch_id"] for m in frozen_spec["members"]]
    live_ids = list(live.get("member_batch_ids", []))
    if sorted(frozen_ids) != sorted(live_ids):
        added = sorted(set(live_ids) - set(frozen_ids))
        removed = sorted(set(frozen_ids) - set(live_ids))
        if added:
            reasons.append(f"members_added:{added}")
        if removed:
            reasons.append(f"members_removed:{removed}")

    fp = params_from_dict(frozen_spec.get("params")).as_dict()
    lp = params_from_dict(live.get("params")).as_dict()
    if fp != lp:
        changed = {k: {"frozen": fp[k], "current": lp[k]} for k in fp if fp[k] != lp[k]}
        reasons.append(f"params_changed:{json.dumps(changed, ensure_ascii=False)}")

    for m in frozen_spec["members"]:
        bid = m["batch_id"]
        live_m = live.get("members", {}).get(bid)
        if live_m is None:
            continue  # membership change already recorded
        anchor = _resolve_anchor(m["events"], frozen_spec["params"]["anchor_event"])
        frozen_eid = anchor["event_id"] if anchor else None
        if live_m.get("anchor_event_id") != frozen_eid:
            reasons.append(
                f"anchor_changed:batch={bid}:"
                f"frozen_event_id={frozen_eid},current_event_id={live_m.get('anchor_event_id')}"
            )
        elif live_m.get("anchor_t_s") != (anchor["t_s"] if anchor else None):
            reasons.append(f"anchor_time_changed:batch={bid}")
        if live_m.get("raw_sha256") != m.get("raw_sha256"):
            reasons.append(f"raw_samples_changed:batch={bid}")
    return reasons


def raw_samples_hash(samples: list[dict]) -> str:
    """Hash of measured raw rows — samples are append/immutable in practice."""
    rows = [
        [s["t_s"], s["bean_temp_c"], s["env_temp_c"]]
        for s in sorted(samples, key=lambda s: s["t_s"])
    ]
    return sha256_of(rows)
