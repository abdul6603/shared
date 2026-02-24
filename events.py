"""Shared Event Bus — Blackboard Architecture.

Central event log that all agents can publish to and read from.
Append-only JSONL file with cursor-based reading and auto-pruning.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

ET = ZoneInfo("America/New_York")

SHARED_DIR = Path(__file__).resolve().parent
EVENTS_FILE = SHARED_DIR / "events.jsonl"
CURSORS_FILE = SHARED_DIR / "cursors.json"
LOCK_FILE = SHARED_DIR / ".events.lock"
ARCHIVE_DIR = SHARED_DIR / "data"

_lock = threading.Lock()  # intra-process
_file_lock_fd = None       # cross-process

# --- Event Type Constants ---
INSIGHT_FOUND = "insight_found"
AGENT_ERROR = "agent_error"
TRADE_EXECUTED = "trade_executed"
BREAKING_NEWS = "breaking_news"
CYCLE_COMPLETED = "cycle_completed"
LEARNING_APPLIED = "learning_applied"
HEALTH_CHECK = "health_check"
PARAM_SUGGESTION = "param_suggestion"

# Per-agent event type subscriptions
_subscriptions: dict[str, set[str]] = {}


def subscribe(agent_name: str, event_types: list[str]) -> None:
    """Register an agent to receive only specific event types."""
    _subscriptions[agent_name] = set(event_types)




def _acquire_file_lock():
    """Acquire cross-process file lock for event bus writes."""
    global _file_lock_fd
    _file_lock_fd = open(LOCK_FILE, "w")
    fcntl.flock(_file_lock_fd, fcntl.LOCK_EX)


def _release_file_lock():
    """Release cross-process file lock."""
    global _file_lock_fd
    if _file_lock_fd:
        fcntl.flock(_file_lock_fd, fcntl.LOCK_UN)
        _file_lock_fd.close()
        _file_lock_fd = None


def _generate_id() -> str:
    """Generate a unique event ID: evt_{unix_ts}_{hex4}."""
    ts = int(time.time())
    rand_hex = os.urandom(2).hex()
    return f"evt_{ts}_{rand_hex}"


def publish(
    agent: str,
    event_type: str,
    data: dict[str, Any] | None = None,
    severity: str = "info",
    summary: str = "",
) -> str:
    """Publish an event to the shared bus. Returns the event ID."""
    event = {
        "id": _generate_id(),
        "ts": datetime.now(ET).isoformat(),
        "agent": agent,
        "type": event_type,
        "severity": severity,
        "data": data or {},
        "summary": summary,
    }

    with _lock:
        _acquire_file_lock()
        try:
            with open(EVENTS_FILE, "a") as f:
                f.write(json.dumps(event, default=str) + "\n")
        finally:
            _release_file_lock()

    # Auto-prune on every 50th write (probabilistic, low overhead)
    if int(time.time()) % 50 == 0:
        try:
            prune()
        except Exception:
            pass

    return event["id"]


def _read_all() -> list[dict]:
    """Read all events from the JSONL file."""
    if not EVENTS_FILE.exists():
        return []
    events = []
    with open(EVENTS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def get_events(
    since_id: str | None = None,
    agent: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get events with optional filters. Returns newest first."""
    events = _read_all()

    # Filter since_id
    if since_id:
        found = False
        filtered = []
        for e in events:
            if found:
                filtered.append(e)
            elif e.get("id") == since_id:
                found = True
        events = filtered

    # Apply filters
    if agent:
        events = [e for e in events if e.get("agent") == agent]
    if event_type:
        events = [e for e in events if e.get("type") == event_type]
    if severity:
        events = [e for e in events if e.get("severity") == severity]

    # Return newest first, capped at limit
    return list(reversed(events[-limit:]))


def _load_cursors() -> dict:
    if not CURSORS_FILE.exists():
        return {}
    try:
        return json.loads(CURSORS_FILE.read_text())
    except Exception:
        return {}


def _save_cursors(cursors: dict) -> None:
    CURSORS_FILE.write_text(json.dumps(cursors, indent=2))


def get_unread(agent_name: str) -> list[dict]:
    """Get events published since this agent's last read cursor. Updates cursor."""
    cursors = _load_cursors()
    last_id = cursors.get(agent_name)

    events = get_events(since_id=last_id, limit=100)

    if events:
        # Update cursor to newest event
        cursors[agent_name] = events[0]["id"]  # events[0] is newest
        _save_cursors(cursors)

    return events



def get_subscribed_events(agent_name: str) -> list[dict]:
    """Get unread events filtered to agent's subscriptions."""
    events = get_unread(agent_name)
    subs = _subscriptions.get(agent_name)
    if subs:
        events = [e for e in events if e.get("type") in subs]
    return events


def mark_read(agent_name: str, event_id: str) -> None:
    """Manually set an agent's cursor to a specific event ID."""
    cursors = _load_cursors()
    cursors[agent_name] = event_id
    _save_cursors(cursors)


def get_stats() -> dict:
    """Get event counts grouped by agent and type."""
    events = _read_all()
    by_agent: dict[str, int] = {}
    by_type: dict[str, int] = {}
    severities: dict[str, int] = {}

    for e in events:
        a = e.get("agent", "unknown")
        t = e.get("type", "unknown")
        s = e.get("severity", "info")
        by_agent[a] = by_agent.get(a, 0) + 1
        by_type[t] = by_type.get(t, 0) + 1
        severities[s] = severities.get(s, 0) + 1

    return {
        "total": len(events),
        "by_agent": by_agent,
        "by_type": by_type,
        "by_severity": severities,
    }



def rotate(max_age_days: int = 7) -> int:
    """Archive events older than max_age_days. Returns count archived."""
    if not EVENTS_FILE.exists():
        return 0

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(ET) - timedelta(days=max_age_days)
    events = _read_all()
    kept = []
    archived = []

    for e in events:
        ts_str = e.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ET)
            if ts >= cutoff:
                kept.append(e)
            else:
                archived.append(e)
        except Exception:
            kept.append(e)

    if not archived:
        return 0

    date_str = datetime.now(ET).strftime("%Y%m%d")
    archive_file = ARCHIVE_DIR / f"events_archive_{date_str}.jsonl"
    with open(archive_file, "a") as f:
        for e in archived:
            f.write(json.dumps(e, default=str) + "\n")

    with _lock:
        _acquire_file_lock()
        try:
            with open(EVENTS_FILE, "w") as f:
                for e in kept:
                    f.write(json.dumps(e, default=str) + "\n")
        finally:
            _release_file_lock()

    return len(archived)


def prune(max_age_hours: int = 48) -> int:
    """Remove events older than max_age_hours. Returns count removed."""
    if not EVENTS_FILE.exists():
        return 0

    cutoff = datetime.now(ET) - timedelta(hours=max_age_hours)
    events = _read_all()
    kept = []
    removed = 0

    for e in events:
        ts_str = e.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ET)
            if ts >= cutoff:
                kept.append(e)
            else:
                removed += 1
        except Exception:
            kept.append(e)  # Keep events we can't parse

    if removed > 0:
        with _lock:
            _acquire_file_lock()
            try:
                with open(EVENTS_FILE, "w") as f:
                    for e in kept:
                        f.write(json.dumps(e, default=str) + "\n")
            finally:
                _release_file_lock()

    return removed
