"""SQLAlchemy models: batches, *raw* samples, sourced events, batch groups.

Design rules enforced at the storage layer:

* ``samples`` only ever contains measured samples.  Interpolation for gaps is
  computed at query time and is returned with ``is_interpolated=True`` — it is
  never written back here, so a plotting convenience can never masquerade as a
  measurement.
* Events (turning point, first crack, damper change, drop ...) are append-only.
  A manual correction supersedes the previous row instead of deleting it, so
  every value keeps its ``source`` / ``created_by`` provenance.
* Batch *groups* compare 3--8 members around an explicit event anchor.  A group
  has a mutable draft (name/params/members, ``revision`` for optimistic
  concurrency).  Every published **snapshot** freezes the member set, the exact
  event versions used as anchors, the exclusion reasons and the analysis
  parameters.  Old snapshots never mutate; event corrections afterwards only
  flag the group as stale, they cannot silently change an existing report.

Provenance of synthetic data
----------------------------
There is no machine connection anywhere in this app.  The local generator that
produces demo batches is tracked per row (``data_origin``, generator seed and
generator version) so a synthetic control batch stays visibly synthetic in
snapshots, legend entries, exports and independent recomputation — it can never
be presented as data from a real roaster.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import DATABASE_URL

# Provenance labels.
ORIGIN_LOCAL_SYNTHETIC = "local_synthetic_demo"
ORIGIN_LOCAL_SYNTHETIC_CONTROL = "local_synthetic_control"
GENERATOR_VERSION = "synth-v2-control"
# Result text that must accompany any result containing synthetic members.
SYNTHETIC_DISCLAIMER = (
    "本结果包含本地生成的合成演示批次（确定性生成器，未连接任何真实烘焙机、未上传任何测量数据），"
    "仅为本地合成演示，不构成真实稳定性结论。"
)


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    roaster: Mapped[str] = mapped_column(String(120), default="synthetic")
    bean: Mapped[str] = mapped_column(String(120), default="")
    charge_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    charge_temp_c: Mapped[float] = mapped_column(Float)
    ambient_temp_c: Mapped[float] = mapped_column(Float)
    target_drop_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # --- provenance of how the row came to exist -----------------------------
    # "local_synthetic_demo" / "local_synthetic_control": generated offline by
    # the bundled deterministic generator.  A real upload pipeline (there is
    # none in this app) would use a distinct origin.  Never blank on synth rows.
    data_origin: Mapped[str] = mapped_column(
        String(60), default=ORIGIN_LOCAL_SYNTHETIC, nullable=False
    )
    is_local_synthetic: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_control_batch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    generator_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generator_version: Mapped[str | None] = mapped_column(String(40), nullable=True)

    samples: Mapped[list["Sample"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Sample.t_s"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Event.t_s"
    )


class Sample(Base):
    """One raw probe reading.  Temperatures are NULL when the probe was
    briefly lost — the missing reading is preserved as missing, not invented."""

    __tablename__ = "samples"
    __table_args__ = (UniqueConstraint("batch_id", "t_s", name="uq_sample_batch_t"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # Seconds since charge.  Intervals are intentionally uneven.
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bean_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    env_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)

    batch: Mapped[Batch] = relationship(back_populates="samples")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # turning_point | first_crack_start | first_crack_end | drop |
    # damper_change | charge | custom
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    # auto = detected from the raw series; manual = operator entry.
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    # Numeric payload, e.g. new damper position (%) for damper_change.
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped[Batch] = relationship(back_populates="events")


class BatchGroup(Base):
    """A named small group (3--8 batches) compared around an event anchor.

    The row is the mutable *draft*: editing members/params bumps ``revision``.
    Published analyses live in ``GroupSnapshot`` rows and are immutable.
    """

    __tablename__ = "batch_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # Event type the members are aligned on, e.g. "first_crack_start".
    anchor_event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # Frozen analysis/display parameters (copied into every snapshot).
    ror_window_s: Mapped[float] = mapped_column(Float, default=30.0)
    display_smooth_s: Mapped[float] = mapped_column(Float, default=12.0)
    max_gap_fill_s: Mapped[float] = mapped_column(Float, default=45.0)
    # Anchor-relative grid for the median band, seconds.
    grid_step_s: Mapped[float] = mapped_column(Float, default=10.0)
    # A measured sample within anchor ± this many seconds counts as support;
    # otherwise the member is excluded as "anchor inside a long dropout".
    anchor_support_tolerance_s: Mapped[float] = mapped_column(Float, default=5.0)
    # Optimistic concurrency token.  Every member/param edit bumps it.
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    latest_snapshot_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    members: Mapped[list["GroupMember"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="GroupMember.position",
    )
    snapshots: Mapped[list["GroupSnapshot"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="GroupSnapshot.version",
    )


class GroupMember(Base):
    """One draft membership row.  (group_id, batch_id) is unique, so the same
    batch can never appear twice in a group even under concurrent edits."""

    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "batch_id", name="uq_group_member_batch"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("batch_groups.id"), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    group: Mapped[BatchGroup] = relationship(back_populates="members")


class GroupSnapshot(Base):
    """An immutable, versionable freeze of a group analysis.

    ``payload_json`` holds the complete self-describing analysis: members,
    their exact anchor event versions, exclusion reasons, parameters and the
    aggregate result.  Replay/export/recompute all read this single document,
    so later edits or event corrections cannot alter a published report.
    """

    __tablename__ = "group_snapshots"
    __table_args__ = (
        UniqueConstraint("group_id", "version", name="uq_group_snapshot_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("batch_groups.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Revision of the group draft this snapshot was published from.
    base_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)

    group: Mapped[BatchGroup] = relationship(back_populates="snapshots")


_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)


# Columns added to the existing `batches` table after the two-batch milestone.
# create_all handles fresh databases; these ALTERs upgrade databases created by
# the previous version (SQLite CI store and local dev files).
_BATCHES_ADDED_COLUMNS = {
    "data_origin": f"VARCHAR(60) NOT NULL DEFAULT '{ORIGIN_LOCAL_SYNTHETIC}'",
    "is_local_synthetic": "BOOLEAN NOT NULL DEFAULT 1",
    "is_control_batch": "BOOLEAN NOT NULL DEFAULT 0",
    "generator_seed": "INTEGER",
    "generator_version": "VARCHAR(40)",
}


def init_db() -> None:
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("batches")}
    with engine.begin() as conn:
        for col, ddl in _BATCHES_ADDED_COLUMNS.items():
            if col not in existing:
                conn.exec_driver_sql(f"ALTER TABLE batches ADD COLUMN {col} {ddl}")
