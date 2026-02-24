"""Shared Unified Health Status API.

Single source of truth for system-wide health.
Collects status from all agent files and writes a unified system_health.json.

Usage:
    from shared.health import collect_all_health, get_system_health
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SHARED_DIR = Path(__file__).resolve().parent
HEALTH_FILE = SHARED_DIR / "data" / "system_health.json"

# Agent status file locations
_AGENT_STATUS_FILES = {
    "garves": Path.home() / "polymarket-bot" / "data" / "garves_status.json",
    "hawk": Path.home() / "polymarket-bot" / "data" / "hawk_status.json",
    "soren": Path.home() / "soren-content" / "data" / "status.json",
    "shelby": Path.home() / "shelby" / "data" / "status.json",
    "atlas": Path.home() / "atlas" / "data" / "background_status.json",
    "lisa": Path.home() / "mercury" / "data" / "status.json",
    "robotox": Path.home() / "sentinel" / "data" / "status.json",
    "thor": Path.home() / "thor" / "data" / "status.json",
    "viper": Path.home() / "polymarket-bot" / "data" / "viper_status.json",
    "quant": Path.home() / "polymarket-bot" / "data" / "quant_status.json",
    "oracle": Path.home() / "polymarket-bot" / "oracle" / "data" / "status.json",
    "odin": Path.home() / "odin" / "data" / "status.json",
}


@dataclass
class HealthReport:
    agent: str
    status: str  # "online", "offline", "degraded", "unknown"
    uptime_s: float
    last_activity: str
    error_count: int
    metrics: dict


def _read_agent_status(agent: str, path: Path) -> HealthReport:
    """Read a single agent's status file and return a HealthReport."""
    if not path.exists():
        return HealthReport(
            agent=agent, status="offline", uptime_s=0,
            last_activity="", error_count=0, metrics={},
        )

    try:
        data = json.loads(path.read_text())
    except Exception:
        return HealthReport(
            agent=agent, status="unknown", uptime_s=0,
            last_activity="", error_count=0, metrics={},
        )

    # Determine uptime from file mtime
    try:
        mtime = path.stat().st_mtime
        uptime_s = time.time() - mtime
        # If file hasn't been updated in > 30 min, agent may be offline
        if uptime_s > 1800:
            status = "degraded"
        else:
            status = "online"
    except Exception:
        uptime_s = 0
        status = "unknown"

    # Override with explicit state if present
    state = data.get("state", data.get("status", ""))
    if state in ("running", "online", "active"):
        status = "online"
    elif state in ("stopped", "idle", "offline"):
        status = "offline"
    elif state in ("error", "crashed"):
        status = "degraded"

    last_activity = data.get("last_cycle", data.get("last_activity",
                   data.get("last_scan", data.get("updated_at", ""))))

    return HealthReport(
        agent=agent,
        status=status,
        uptime_s=round(uptime_s, 0),
        last_activity=last_activity,
        error_count=data.get("error_count", data.get("errors", 0)),
        metrics=_extract_metrics(agent, data),
    )


def _extract_metrics(agent: str, data: dict) -> dict:
    """Extract key metrics from agent status data."""
    metrics = {}
    if agent == "garves":
        metrics["win_rate"] = data.get("win_rate", 0)
        metrics["total_trades"] = data.get("total_trades", 0)
        metrics["active_bets"] = data.get("active_bets", 0)
    elif agent == "hawk":
        metrics["total_trades"] = data.get("total_trades", 0)
        metrics["active_bets"] = data.get("active_bets", 0)
    elif agent == "atlas":
        metrics["cycles"] = data.get("cycles", 0)
        metrics["last_findings"] = data.get("last_findings", 0)
    elif agent == "soren":
        metrics["queue_total"] = data.get("queue_total", 0)
    elif agent == "shelby":
        metrics["total_tasks"] = data.get("total_tasks", 0)
    elif agent == "thor":
        metrics["completed"] = data.get("completed", 0)
        metrics["pending"] = data.get("pending", 0)
    return metrics


def collect_all_health() -> dict:
    """Collect health from all agents and return unified report."""
    reports = {}
    for agent, path in _AGENT_STATUS_FILES.items():
        report = _read_agent_status(agent, path)
        reports[agent] = asdict(report)

    online = sum(1 for r in reports.values() if r["status"] == "online")
    degraded = sum(1 for r in reports.values() if r["status"] == "degraded")
    offline = sum(1 for r in reports.values() if r["status"] == "offline")

    return {
        "timestamp": datetime.now(ET).isoformat(),
        "summary": {
            "total": len(reports),
            "online": online,
            "degraded": degraded,
            "offline": offline,
        },
        "agents": reports,
    }


def write_health_file() -> dict:
    """Collect health and write to system_health.json."""
    health = collect_all_health()
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HEALTH_FILE, "w") as f:
        json.dump(health, f, indent=2)
    return health


def get_system_health() -> dict:
    """Read the latest system health from file."""
    if HEALTH_FILE.exists():
        try:
            return json.loads(HEALTH_FILE.read_text())
        except Exception:
            pass
    return collect_all_health()
