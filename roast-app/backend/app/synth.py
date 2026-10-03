"""Synthetic batch generator — stands in for a real roaster (NOT connected).

Produces a physically-shaped bean curve with:

* uneven sampling intervals (jittered 1--5 s),
* white measurement noise on both probes,
* two short probe dropouts (NULL samples, one short-fillable, one wide),
* a damper-change event whose effect is a *modelled* kink so the operator can
  inspect before/after curves — the UI never claims causation,
* auto-detected/suggested turning point and first-crack events marked with
  source='auto'.

Every generated batch carries explicit provenance (``data_origin``, seed and
generator version).  ``local_control_batch`` is the *deterministic local
control* entry used to make a >=3-batch group from the two existing demo
batches without importing anything from elsewhere: same generator, a fixed
seed, and the label ``local_synthetic_control`` everywhere the data travels.
It is and stays marked as a local synthetic demo — never as real roaster data.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from .analysis import detect_turning_point
from .models import GENERATOR_VERSION, ORIGIN_LOCAL_SYNTHETIC, ORIGIN_LOCAL_SYNTHETIC_CONTROL

RNG = np.random.default_rng

# Fixed seed for the deterministic local control: regenerating it anywhere
# (another machine, recompute endpoint) yields byte-identical samples.
CONTROL_SEED = 20260920
CONTROL_NAME = "SYN-LOCAL-CONTROL-C"


def _bean_curve(t: np.ndarray, *, damper_t: float | None, damper_strength: float) -> np.ndarray:
    """Idealised °C bean curve vs seconds since charge.

    Charge at ~180 °C -> turning point -> ROR-decaying climb -> ~210 °C drop.
    """
    # Turning-point valley.
    valley = 92.0 + 8.0 * (1.0 - math.exp(-3.0))
    y = np.empty_like(t)
    for i, x in enumerate(t):
        if x < 60:
            # Initial fall from charge temp into the valley.
            y[i] = 180.0 - (180.0 - valley) * (1.0 - math.exp(-x / 18.0))
        else:
            u = x - 60.0
            # Decaying-rate climb toward an asymptotic high temperature.
            y[i] = valley + 125.0 * (1.0 - math.exp(-u / 300.0))
    if damper_t is not None:
        # MODELED response to a damper change: a small extra heat-loss slope
        # after the change. A visual narrative for before/after comparison,
        # not a causal claim.
        after = np.clip((t - damper_t) / 60.0, 0.0, 1.0)
        y -= damper_strength * after
    return y


def _env_curve(t: np.ndarray) -> np.ndarray:
    """Environment/inlet temperature: hotter and less responsive."""
    return 190.0 + 35.0 * (1.0 - np.exp(-t / 240.0)) + 4.0 * (t / 600.0)


def generate_batch(
    *,
    name: str,
    seed: int,
    bean: str = "Ethiopia Yirgacheffe",
    roaster: str = "synthetic-1kg (NOT a connected machine)",
    damper_t: float | None = 300.0,
    damper_from: float = 70.0,
    damper_to: float = 40.0,
    duration_s: float = 600.0,
    ambient_temp_c: float = 22.5,
    drop_temp_c: float = 208.0,
    bean_noise_sd: float = 0.8,
    env_noise_sd: float = 1.4,
    dropout_ranges_s: tuple[tuple[float, float], ...] = ((150.0, 158.0), (420.0, 480.0)),
    data_origin: str = ORIGIN_LOCAL_SYNTHETIC,
    is_control_batch: bool = False,
) -> dict:
    rng = RNG(seed)

    # Uneven timestamps: jitter a nominal 2 s grid with 1--5 s gaps.
    t: list[float] = []
    x = 0.0
    while x <= duration_s:
        t.append(round(x, 2))
        x += float(rng.uniform(1.0, 5.0))
    t_arr = np.array(t)

    strength = 3.5 if damper_to < damper_from else -2.0
    bean_true = _bean_curve(t_arr, damper_t=damper_t, damper_strength=strength)
    env_true = _env_curve(t_arr)

    bean_obs = bean_true + rng.normal(0.0, bean_noise_sd, size=t_arr.shape)
    env_obs = env_true + rng.normal(0.0, env_noise_sd, size=t_arr.shape)

    bean_missing = np.zeros_like(t_arr, dtype=bool)
    env_missing = np.zeros_like(t_arr, dtype=bool)
    for lo, hi in dropout_ranges_s:
        m = (t_arr >= lo) & (t_arr <= hi)
        bean_missing |= m
        # env probe shares the short dropout but recovers from the long one
        if hi - lo <= 30:
            env_missing |= m

    samples = [
        {
            "t_s": float(t_arr[i]),
            "bean_temp_c": None if bean_missing[i] else round(float(bean_obs[i]), 2),
            "env_temp_c": None if env_missing[i] else round(float(env_obs[i]), 2),
        }
        for i in range(len(t_arr))
    ]

    # Drop when modelled curve first crosses the drop target.
    idx_drop = int(np.argmax(bean_true >= drop_temp_c))
    drop_t = float(t_arr[idx_drop]) if bean_true[idx_drop] >= drop_temp_c else duration_s

    # Auto-suggested turning point from noisy measured data.
    tp_t = detect_turning_point(samples)

    # First crack ~ 192 °C modelled; search just before drop.
    fc_hits = np.flatnonzero(bean_true >= 192.0)
    fc_hits = fc_hits[fc_hits < idx_drop]
    if len(fc_hits):
        fc_t = float(t_arr[int(fc_hits[0])])
    else:
        fc_t = max(drop_t - 120.0, 0.0)
    fce_t = fc_t + 55.0

    charge_at = datetime(2026, 9, 20, 9, 0, 0) + timedelta(minutes=seed % 17)
    events = [
        {"event_type": "charge", "t_s": 0.0, "source": "auto", "label": "下豆/开火"},
    ]
    if tp_t is not None:
        events.append(
            {"event_type": "turning_point", "t_s": tp_t, "source": "auto",
             "label": "回温点(自动建议, 可修正)"}
        )
    events.append(
        {"event_type": "first_crack_start", "t_s": round(fc_t, 2), "source": "auto",
         "label": "一爆开始(自动建议, 可修正)"}
    )
    events.append(
        {"event_type": "first_crack_end", "t_s": round(min(fce_t, drop_t), 2),
         "source": "auto", "label": "一爆结束(自动建议, 可修正)"}
    )
    if damper_t is not None:
        events.append(
            {
                "event_type": "damper_change",
                "t_s": float(damper_t),
                "source": "manual",
                "created_by": "seed-recipe",
                "value_num": float(damper_to),
                "label": f"风门 {damper_from:.0f}% -> {damper_to:.0f}%",
                "note": "modelled event; before/after comparison is not causal evidence",
            }
        )
    events.append(
        {"event_type": "drop", "t_s": round(drop_t, 2), "source": "auto",
         "label": "出锅(自动建议, 可修正)"}
    )

    if is_control_batch:
        note = (
            "本地合成对照批次（确定性生成器，固定种子；未连接真实烘焙机、未上传测量数据）。"
            "仅用于本地演示批次组聚合，不构成真实稳定性结论。"
        )
    else:
        note = "合成数据：含测量噪声、不均采样与探针缺测；未连接真实烘焙机。"

    return {
        "name": name,
        "roaster": roaster,
        "bean": bean,
        "charge_at": charge_at,
        "charge_temp_c": 180.0,
        "ambient_temp_c": ambient_temp_c,
        "target_drop_temp_c": drop_temp_c,
        "note": note,
        "samples": samples,
        "events": events,
        # Provenance — follows the batch into snapshots, legend, export.
        "data_origin": data_origin,
        "is_local_synthetic": True,
        "is_control_batch": is_control_batch,
        "generator_seed": int(seed),
        "generator_version": GENERATOR_VERSION,
    }


def two_demo_batches() -> list[dict]:
    """Batch A: close damper mid-roast. Batch B: keep it open. Same seed base."""
    return [
        generate_batch(
            name="SYN-2026-0920-A",
            seed=42,
            damper_t=300.0,
            damper_from=70.0,
            damper_to=40.0,
        ),
        generate_batch(
            name="SYN-2026-0920-B",
            seed=7,
            bean="Colombia Huila",
            damper_t=None,  # no damper change: control for comparison
        ),
    ]


def local_control_batch() -> dict:
    """Deterministic local-only control batch ("batch C").

    Fixed seed → regenerating this entry anywhere reproduces the complete
    samples and key events exactly.  Designed so that its first-crack anchor
    (modelled ~480 s) has a *measured* sample within the standard support
    tolerance, i.e. it aligns cleanly with the two existing demo batches for a
    >=3-member group despite their ~420--480 s long dropout.
    """
    return generate_batch(
        name=CONTROL_NAME,
        seed=CONTROL_SEED,
        bean="Brazil Cerrado (本地合成对照豆种, 非真实批次)",
        # The long modelled dropout ends at 470 s here, leaving measured points
        # immediately around the 480 s first-crack anchor; the short dropout
        # and wide-gap mechanics otherwise match batches A/B.
        dropout_ranges_s=((150.0, 158.0), (410.0, 470.0)),
        damper_t=None,
        data_origin=ORIGIN_LOCAL_SYNTHETIC_CONTROL,
        is_control_batch=True,
    )
