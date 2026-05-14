"""ORM model for vol_surface_snapshot — daily IV skew, term structure, and OI walls."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class VolSurfaceSnapshot(Base):
    """One surface reading per instrument per day.

    Written by vol_surface.compute_for_instruments() which runs pre-market
    (after chain collection, before Phase 3) and optionally EOD.

    Skew: moneyness-based (2-15% OTM band, highest-OI strike on each side).
    Term: ATM IV comparison across front and back expiry.
    OI walls: strike-level open interest peaks — used for condor wing placement.
    """

    __tablename__ = "vol_surface_snapshot"
    __table_args__ = (
        UniqueConstraint("instrument_id", "run_date"),
        CheckConstraint("skew_regime IN ('put_skewed','flat','call_skewed','insufficient_data')"),
        CheckConstraint("term_regime IN ('normal','flat','inverted','single_expiry','near_pin')"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4())
    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    chain_snap_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Skew (moneyness-based, highest-OI OTM strike in 2-15% band)
    iv_skew_5pct: Mapped[float | None] = mapped_column(Numeric(8, 4))   # iv_otm_put - iv_otm_call (% points)
    iv_otm_put: Mapped[float | None] = mapped_column(Numeric(8, 4))
    iv_otm_call: Mapped[float | None] = mapped_column(Numeric(8, 4))
    otm_put_strike: Mapped[float | None] = mapped_column(Numeric(12, 2))
    otm_call_strike: Mapped[float | None] = mapped_column(Numeric(12, 2))
    skew_regime: Mapped[str | None] = mapped_column(String(20))

    # Term structure
    expiry_near: Mapped[date | None] = mapped_column(Date)
    expiry_far: Mapped[date | None] = mapped_column(Date)
    iv_front: Mapped[float | None] = mapped_column(Numeric(8, 4))
    iv_back: Mapped[float | None] = mapped_column(Numeric(8, 4))
    term_slope: Mapped[float | None] = mapped_column(Numeric(8, 4))
    term_regime: Mapped[str | None] = mapped_column(String(20))

    # OI walls
    pin_strike: Mapped[float | None] = mapped_column(Numeric(12, 2))     # argmax(OI_CE + OI_PE)
    call_wall: Mapped[float | None] = mapped_column(Numeric(12, 2))      # highest CE OI strike (resistance)
    put_wall: Mapped[float | None] = mapped_column(Numeric(12, 2))       # highest PE OI strike (support)
    pcr_near_expiry: Mapped[float | None] = mapped_column(Numeric(6, 4)) # total PE OI / CE OI for near expiry
    underlying_ltp: Mapped[float | None] = mapped_column(Numeric(12, 2))
    days_to_expiry: Mapped[int | None] = mapped_column(Integer)

    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
