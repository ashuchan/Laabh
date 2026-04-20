"""Watchdog service — pings all services every 60 seconds, alerts via Telegram on failure."""
from __future__ import annotations

import asyncio
import os
import socket
import time
from datetime import datetime

import httpx


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SERVICES_ENV = os.environ.get("SERVICES", "backend:8000,postgres:5432")
CHECK_INTERVAL = 60  # seconds


def parse_services(services_str: str) -> list[tuple[str, int]]:
    result = []
    for svc in services_str.split(","):
        parts = svc.strip().split(":")
        if len(parts) == 2:
            result.append((parts[0], int(parts[1])))
    return result


def check_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


async def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[watchdog] Telegram not configured. Message: {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"[watchdog] Failed to send Telegram alert: {e}")


async def main() -> None:
    services = parse_services(SERVICES_ENV)
    failures: dict[str, int] = {}
    alert_sent: set[str] = set()

    print(f"[watchdog] monitoring {services}")

    while True:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        for host, port in services:
            key = f"{host}:{port}"
            is_up = check_tcp(host, port)

            if not is_up:
                failures[key] = failures.get(key, 0) + 1
                print(f"[watchdog] {key} DOWN (failure #{failures[key]})")
                if failures[key] >= 2 and key not in alert_sent:
                    await send_telegram(
                        f"⚠️ Laabh Watchdog Alert\n"
                        f"Service DOWN: {key}\n"
                        f"Time: {now}\n"
                        f"Failure count: {failures[key]}"
                    )
                    alert_sent.add(key)
            else:
                if failures.get(key, 0) > 0:
                    print(f"[watchdog] {key} RECOVERED")
                    if key in alert_sent:
                        await send_telegram(f"✅ Laabh: {key} recovered at {now}")
                        alert_sent.discard(key)
                failures[key] = 0

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
