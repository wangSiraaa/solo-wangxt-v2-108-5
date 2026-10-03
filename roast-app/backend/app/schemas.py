"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


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
    is_synthetic: bool = True
    synthetic_kind: str | None = None
    generator: str | None = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# batch groups (3--8 members, explicit anchor + parameters, versioned snapshots)
# ---------------------------------------------------------------------------

ANCHOR_CHOICES = (
    "charge",
    "turning_point",
    "first_crack_start",
    "first_crack_end",
    "drop",
)


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    anchor_event: str = "first_crack_start"
    window_s: float = Field(30.0, gt=0, le=300)
    display_smooth_s: float = Field(12.0, ge=0, le=180)
    max_gap_fill_s: float = Field(45.0, gt=0, le=600)
    grid_step_s: float = Field(5.0, gt=0, le=60)
    support_tolerance_s: float = Field(3.0, gt=0, le=30)
    batch_ids: list[int] = Field(min_length=3, max_length=8)
    note: str = ""

    @model_validator(mode="after")
    def _validate(self) -> "GroupCreate":
        self.name = self.name.strip()
        # Preserve order but reject duplicates outright at the schema boundary
        # (deduplicating silently would hide a user/editor mistake).
        if len(self.batch_ids) != len(set(self.batch_ids)):
            raise ValueError("batch_ids 中存在重复成员；同一批次不能重复加入组")
        return self


class GroupMemberUpdate(BaseModel):
    # The revision the editor last saw; stale writers get 409 instead of
    # silently overwriting another editor's membership change.
    expected_revision: int
    batch_ids: list[int] = Field(min_length=3, max_length=8)
    note: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> "GroupMemberUpdate":
        if len(self.batch_ids) != len(set(self.batch_ids)):
            raise ValueError("batch_ids 中存在重复成员；同一批次不能重复加入组")
        return self


class GroupParamsUpdate(BaseModel):
    expected_revision: int
    anchor_event: str | None = None
    window_s: float | None = Field(None, gt=0, le=300)
    display_smooth_s: float | None = Field(None, ge=0, le=180)
    max_gap_fill_s: float | None = Field(None, gt=0, le=600)
    grid_step_s: float | None = Field(None, gt=0, le=60)
    support_tolerance_s: float | None = Field(None, gt=0, le=30)


class SnapshotCreate(BaseModel):
    created_by: str = "operator"
