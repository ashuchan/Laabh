"""IV history — daily ATM implied volatility percentile and VRP per instrument."""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class IVHistory(Base):
    """Daily ATM IV, 52-week percentile, and Volatility Risk Premium per instrument.

    VRP columns (rv_20d, vrp, vrp_regime) are written by vrp_engine.py which runs
    immediately after iv_history_builder in the EOD pipeline. They will be NULL
    on rows written before 2026-05-13 or when price_daily has insufficient history.
    """

    __tablename__ = "iv_history"

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    atm_iv: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    iv_rank_52w: Mapped[float | None] = mapped_column(Numeric(6, 2))
    iv_percentile_52w: Mapped[float | None] = mapped_column(Numeric(6, 2))

    # VRP Engine outputs — added 2026-05-13
    rv_20d: Mapped[float | None] = mapped_column(Numeric(8, 4))        # realized vol, annualized decimal
    vrp: Mapped[float | None] = mapped_column(Numeric(8, 4))           # atm_iv_decimal - rv_20d
    vrp_regime: Mapped[str | None] = mapped_column(String(10))         # 'rich' | 'fair' | 'cheap'

    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
