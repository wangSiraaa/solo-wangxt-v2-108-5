"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .groups import ALLOWED_ANCHORS, MAX_MEMBERS, MIN_MEMBERS


class SampleIn(BaseModel):
    t_s: float
    bean_temp_c: float | None = None
    env_temp_c: float | None = None
    sampled_at: datetime | None = None


class EventIn(BaseModel):
    event_type: str
    t_s: float = Field(ge=0)
    label: str = ""
    source: str = "manual"
    created_by: str = "operator"
    value_num: float | None = None
    note: str = ""


class EventOut(EventIn):
    id: int
    batch_id: int
    superseded: bool
    superseded_by_id: int | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class BatchMeta(BaseModel):
    id: int
    name: str
    roaster: str
    bean: str
    charge_at: datetime
    charge_temp_c: float
    ambient_temp_c: float
    target_drop_temp_c: float | None = None
    note: str
    # Provenance: a synthetic local batch must never be mistaken for data
    # uploaded from a real roaster.
    data_origin: str = "local_synthetic_demo"
    is_local_synthetic: bool = True
    is_control_batch: bool = False
    generator_seed: int | None = None
    generator_version: str | None = None

    class Config:
        from_attributes = True


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    anchor_event_type: str = "first_crack_start"
    batch_ids: list[int] = Field(min_length=MIN_MEMBERS, max_length=MAX_MEMBERS)
    ror_window_s: float = Field(30.0, gt=0, le=300)
    display_smooth_s: float = Field(12.0, ge=0, le=180)
    max_gap_fill_s: float = Field(45.0, gt=0, le=600)
    grid_step_s: float = Field(10.0, gt=0, le=120)
    anchor_support_tolerance_s: float = Field(5.0, gt=0, le=120)

    def validated_anchor(self) -> str:
        if self.anchor_event_type not in ALLOWED_ANCHORS:
            raise ValueError(
                f"anchor_event_type must be one of {ALLOWED_ANCHORS}, "
                f"got {self.anchor_event_type!r}"
            )
        return self.anchor_event_type


class GroupPatch(BaseModel):
    """Edit a group draft.  ``base_revision`` is required for optimistic
    concurrency: an edit based on an outdated revision is refused (409)."""

    base_revision: int
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    anchor_event_type: str | None = None
    batch_ids: list[int] | None = Field(default=None, min_length=MIN_MEMBERS, max_length=MAX_MEMBERS)
    ror_window_s: float | None = Field(default=None, gt=0, le=300)
    display_smooth_s: float | None = Field(default=None, ge=0, le=180)
    max_gap_fill_s: float | None = Field(default=None, gt=0, le=600)
    grid_step_s: float | None = Field(default=None, gt=0, le=120)
    anchor_support_tolerance_s: float | None = Field(default=None, gt=0, le=120)

    def validated_anchor(self) -> str | None:
        if self.anchor_event_type is None:
            return None
        if self.anchor_event_type not in ALLOWED_ANCHORS:
            raise ValueError(
                f"anchor_event_type must be one of {ALLOWED_ANCHORS}, "
                f"got {self.anchor_event_type!r}"
            )
        return self.anchor_event_type


class SnapshotCreate(BaseModel):
    # Optimistic token: publishing from a stale draft is refused (409).
    base_revision: int
    note: str = ""
    created_by: str = "operator"
