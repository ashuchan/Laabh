"""Inspect and seed the rows preflight.seed_data needs:
- source_health rows for nse / dhan / angel_one (idempotent)
- system_config.holiday_calendar = {"dates": []} (stub — populate manually later)
"""
from __future__ import annotations

import asyncio

from dotenv import load_dotenv
from sqlalchemy import text

from src.db import get_engine

load_dotenv()


async def main() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        before_keys = sorted(
            row[0]
            for row in await conn.execute(text("SELECT key FROM system_config"))
        )
        before_sources = sorted(
            row[0]
            for row in await conn.execute(text("SELECT source FROM source_health"))
        )
        print("BEFORE system_config keys:", before_keys)
        print("BEFORE source_health rows:", before_sources)

        # source_health: idempotent insert of the three sources preflight expects
        await conn.execute(
            text(
                """
                INSERT INTO source_health (source, status)
                VALUES (:source, 'healthy')
                ON CONFLICT (source) DO NOTHING
                """
            ),
            [
                {"source": "nse"},
                {"source": "dhan"},
                {"source": "angel_one"},
            ],
        )

        # holiday_calendar: stub with empty list. TradingDayCheck consumes
        # row.value -> {"dates": [...]}; an empty list is fine for now.
        await conn.execute(
            text(
                """
                INSERT INTO system_config (key, value, description)
                VALUES (
                    'holiday_calendar',
                    :value,
                    'NSE/BSE trading holiday calendar — populate annually'
                )
                ON CONFLICT (key) DO NOTHING
                """
            ),
            {"value": '{"dates": []}'},
        )

        after_keys = sorted(
            row[0]
            for row in await conn.execute(text("SELECT key FROM system_config"))
        )
        after_sources = sorted(
            row[0]
            for row in await conn.execute(text("SELECT source FROM source_health"))
        )
        print("AFTER  system_config keys:", after_keys)
        print("AFTER  source_health rows:", after_sources)


if __name__ == "__main__":
    asyncio.run(main())
