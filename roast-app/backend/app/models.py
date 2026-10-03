"""SQLAlchemy models: batches, *raw* samples, and sourced events.

Design rules enforced at the storage layer:

* ``samples`` only ever contains measured samples.  Interpolation for gaps is
  computed at query time and is returned with ``is_interpolated=True`` — it is
  never written back here, so a plotting convenience can never masquerade as a
  measurement.
* Events (turning point, first crack, damper change, drop ...) are append-only.
  A manual correction supersedes the previous row instead of deleting it, so
  every value keeps its ``source`` / ``created_by`` provenance.
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

# Provenance markers.  Every batch the app can create is locally synthesised;
# these strings are the machine-readable half of that declaration and travel
# with the data into group snapshots, legends, exports and replay results.
PROV_DEMO_PAIR = "demo_pair"
PROV_CONTROL = "deterministic_control"
PROV_GENERATOR = "local-synth-generator/v1"


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
    # Synthetic provenance.  is_synthetic must be True for everything this
    # offline app itself produces; synthetic_kind names the generation recipe
    # (demo_pair member vs the deterministic control batch); generator carries
    # the exact recipe/seed reference.  None would mean "origin unknown" and
    # the UI treats that as *not* locally synthetic — it never assumes.
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    synthetic_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    generator: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    samples: Mapped[list["Sample"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Sample.t_s"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Event.t_s"
    )
    group_memberships: Mapped[list["GroupMember"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
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


_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)


class BatchGroup(Base):
    """A named, user-built comparison group of 3--8 batches.

    The group row is the *editable* surface (membership changes bump
    ``revision``); every frozen analysis lives in GroupSnapshot rows that are
    never rewritten.  ``anchor_event`` and the analysis parameters here are the
    defaults applied when a new snapshot is cut.
    """

    __tablename__ = "batch_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    anchor_event: Mapped[str] = mapped_column(String(40), default="first_crack_start")
    window_s: Mapped[float] = mapped_column(Float, default=30.0)
    display_smooth_s: Mapped[float] = mapped_column(Float, default=12.0)
    max_gap_fill_s: Mapped[float] = mapped_column(Float, default=45.0)
    grid_step_s: Mapped[float] = mapped_column(Float, default=5.0)
    support_tolerance_s: Mapped[float] = mapped_column(Float, default=3.0)
    # Optimistic-concurrency counter; editors must send the revision they saw.
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    members: Mapped[list["GroupMember"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="GroupMember.position",
    )
    snapshots: Mapped[list["GroupSnapshot"]] = relationship(
        back_populates="group", cascade="all, delete-orphan", order_by="GroupSnapshot.version"
    )


class GroupMember(Base):
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
    batch: Mapped[Batch] = relationship(back_populates="group_memberships")


class GroupSnapshot(Base):
    """An immutable, hash-pinned version of a group analysis.

    ``spec_json`` freezes the exact inputs the result was derived from: the
    member set with their resolved anchor *event versions* (event row ids),
    parameters, exclusion reasons and the raw samples of every member.
    ``result_json`` caches the deterministic output; recomputing from spec alone
    must reproduce it byte-for-byte in meaning (checked via both hashes).
    """

    __tablename__ = "group_snapshots"
    __table_args__ = (
        UniqueConstraint("group_id", "version", name="uq_group_snapshot_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("batch_groups.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # revision of the editable group at the moment this snapshot was cut
    group_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    n_included: Mapped[int] = mapped_column(Integer, nullable=False)
    n_excluded: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    group: Mapped[BatchGroup] = relationship(back_populates="snapshots")


def _migrate_columns() -> None:
    """Idempotently add columns introduced after the initial schema.

    Tests recreate tables, but existing local/CI databases keep their old
    layout; ALTER TABLE ADD COLUMN is supported identically by SQLite and
    PostgreSQL for these simple nullable/defaulted columns.
    """
    insp = inspect(engine)
    batch_cols = {c["name"] for c in insp.get_columns("batches")}
    with engine.begin() as conn:
        for col, ddl in (
            ("is_synthetic", "BOOLEAN NOT NULL DEFAULT 1"),
            ("synthetic_kind", "VARCHAR(40)"),
            ("generator", "VARCHAR(120)"),
        ):
            if col not in batch_cols:
                conn.exec_driver_sql(f"ALTER TABLE batches ADD COLUMN {col} {ddl}")


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_columns()
