"""Optional real outbound requests through an operator-provided SOCKS client.

Probe configuration is a protected deployment file, never an arbitrary command
or an unauthenticated API parameter. Failures report degraded service without
rebooting a running instance based solely on an external network failure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
import ipaddress
import requests
from . import db

_stop = threading.Event()
_thread = None


def initialize() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS service_health(
        server_id INTEGER PRIMARY KEY, status TEXT NOT NULL, checked_at INTEGER NOT NULL,
        failures INTEGER NOT NULL DEFAULT 0, latency_ms REAL, detail TEXT NOT NULL DEFAULT '')""")


def check(config: dict) -> dict:
    expected = str(ipaddress.IPv4Address(config["expected_ipv4"]))
    port = int(config["socks_port"])
    if not 1 <= port <= 65535:
        raise ValueError("Invalid SOCKS port")
    # Deployment chooses the local sidecar; no credentials in request URLs or logs.
    host = str(config.get("socks_host") or "127.0.0.1")
    if any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"
        for c in host
    ):
        raise ValueError("Invalid SOCKS host")
    proxy = f"socks5h://{host}:{port}"
    started = time.monotonic()
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                "https://checkip.amazonaws.com/",
                proxies={"http": proxy, "https": proxy},
                timeout=(5, 10),
            )
            response.raise_for_status()
            ok = response.text.strip() == expected
        return {
            "status": "healthy" if ok else "degraded",
            "detail": "" if ok else "代理出口与预期不符",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
    except Exception:
        return {"status": "degraded", "detail": "代理实际请求失败", "latency_ms": None}


def _loop(configs: list[dict]) -> None:
    while not _stop.is_set():
        for config in configs:
            if _stop.is_set():
                break
            try:
                result = check(config)
                db.execute(
                    """INSERT INTO service_health(server_id,status,checked_at,failures,latency_ms,detail)
                    VALUES(?,?,?, ?,?,?) ON CONFLICT(server_id) DO UPDATE SET status=excluded.status,
                    checked_at=excluded.checked_at,latency_ms=excluded.latency_ms,detail=excluded.detail,
                    failures=CASE WHEN excluded.status='healthy' THEN 0 ELSE service_health.failures+1 END""",
                    (
                        int(config["server_id"]),
                        result["status"],
                        int(time.time()),
                        0 if result["status"] == "healthy" else 1,
                        result["latency_ms"],
                        result["detail"],
                    ),
                )
            except Exception:
                pass
        _stop.wait(120)


def start() -> None:
    global _thread
    initialize()
    name = os.environ.get("CDT_PROBE_CONFIG", "")
    if not name:
        return
    configs = json.loads(Path(name).read_text())
    if not isinstance(configs, list) or len(configs) > 20:
        raise ValueError("Invalid probe configuration")
    _stop.clear()
    _thread = threading.Thread(
        target=_loop, args=(configs,), name="cdt-service-health", daemon=True
    )
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=20)
