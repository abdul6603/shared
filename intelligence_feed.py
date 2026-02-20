"""Shared intelligence feed — Robotox writes, Thor reads.

A structured JSONL feed that flows intelligence from monitoring to coding:
- Performance hotspots → optimization tasks
- Recurring errors → root-cause fix tasks
- Dependency CVEs → package update tasks
- Resource trending → architecture tasks
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

log = logging.getLogger("shared.intelligence_feed")

ET = ZoneInfo("America/New_York")

FEED_FILE = Path.home() / "shared" / "intelligence_feed.jsonl"
CURSOR_FILE = Path.home() / "shared" / "intel_cursors.json"

# Categories for intelligence items
CATEGORIES = {
    "performance_hotspot": "Agent process using excessive CPU/memory",
    "recurring_error": "Same error pattern detected multiple times",
    "dependency_cve": "Known vulnerability in a dependency",
    "resource_trend": "Resource usage trending toward a limit",
    "dependency_down": "External API dependency is unreachable",
    "stale_data": "Agent data files are stale beyond threshold",
    "deployment_regression": "Performance degraded after code deployment",
}


def publish_intel(
    source: str,
    category: str,
    agent: str,
    title: str,
    details: str,
    priority: str = "normal",
    suggested_action: str = "",
    data: dict | None = None,
) -> None:
    """Publish an intelligence item to the feed.

    Args:
        source: Who is publishing (e.g., "robotox", "atlas")
        category: One of CATEGORIES keys
        agent: Which agent this is about
        title: Short title
        details: Full description
        priority: "low", "normal", "high", "critical"
        suggested_action: What Thor should do about it
        data: Optional structured data
    """
    entry = {
        "timestamp": datetime.now(ET).isoformat(),
        "source": source,
        "category": category,
        "agent": agent,
        "title": title,
        "details": details[:500],
        "priority": priority,
        "suggested_action": suggested_action[:300],
        "data": data or {},
        "consumed": False,
    }

    try:
        FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FEED_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        log.info("Intel published: [%s] %s — %s", category, agent, title)
    except Exception as e:
        log.warning("Failed to publish intel: %s", e)


def get_unread(reader: str, limit: int = 20) -> list[dict]:
    """Get unread intelligence items for a reader (e.g., 'thor').

    Uses cursor-based reading so each reader tracks their own position.
    """
    if not FEED_FILE.exists():
        return []

    # Load cursor
    cursor = 0
    try:
        if CURSOR_FILE.exists():
            cursors = json.loads(CURSOR_FILE.read_text())
            cursor = cursors.get(reader, 0)
    except Exception:
        pass

    # Read from cursor position
    items = []
    try:
        lines = FEED_FILE.read_text().splitlines()
        for i, line in enumerate(lines):
            if i < cursor or not line.strip():
                continue
            try:
                entry = json.loads(line)
                entry["_line"] = i
                items.append(entry)
            except Exception:
                continue
    except Exception:
        return []

    return items[-limit:]


def mark_read(reader: str, up_to_line: int) -> None:
    """Mark all items up to a line number as read for this reader."""
    try:
        cursors = {}
        if CURSOR_FILE.exists():
            cursors = json.loads(CURSOR_FILE.read_text())
        cursors[reader] = up_to_line + 1
        CURSOR_FILE.write_text(json.dumps(cursors, indent=2))
    except Exception as e:
        log.warning("Failed to update intel cursor: %s", e)


def get_all(limit: int = 50) -> list[dict]:
    """Get all recent intelligence items (for dashboard)."""
    if not FEED_FILE.exists():
        return []
    try:
        items = []
        for line in FEED_FILE.read_text().splitlines():
            if line.strip():
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
        return items[-limit:]
    except Exception:
        return []


def get_stats() -> dict:
    """Get feed statistics."""
    items = get_all(limit=1000)
    if not items:
        return {"total": 0, "by_category": {}, "by_agent": {}, "by_priority": {}}

    by_cat = {}
    by_agent = {}
    by_priority = {}
    for item in items:
        cat = item.get("category", "unknown")
        by_cat[cat] = by_cat.get(cat, 0) + 1
        agent = item.get("agent", "unknown")
        by_agent[agent] = by_agent.get(agent, 0) + 1
        pri = item.get("priority", "normal")
        by_priority[pri] = by_priority.get(pri, 0) + 1

    return {
        "total": len(items),
        "by_category": by_cat,
        "by_agent": by_agent,
        "by_priority": by_priority,
    }


def prune(keep: int = 500) -> int:
    """Prune old entries, keeping the last N."""
    if not FEED_FILE.exists():
        return 0
    try:
        lines = FEED_FILE.read_text().splitlines()
        if len(lines) <= keep:
            return 0
        removed = len(lines) - keep
        FEED_FILE.write_text("\n".join(lines[-keep:]) + "\n")
        # Reset cursors since line numbers changed
        if CURSOR_FILE.exists():
            CURSOR_FILE.write_text("{}")
        log.info("Pruned %d old intel entries", removed)
        return removed
    except Exception:
        return 0
