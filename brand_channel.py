"""Brand Channel — Viper -> Soren -> Lisa message pipeline.

Manages the flow of brand opportunities from Viper's scanner
through Soren's brand identity filter to Lisa's content queue.

State persisted to ~/polymarket-bot/data/brand_channel.json.
Publishes events to the shared event bus for dashboard notifications.
"""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
DATA_DIR = Path.home() / "polymarket-bot" / "data"
CHANNEL_FILE = DATA_DIR / "brand_channel.json"
MAX_MESSAGES = 200

_lock = threading.Lock()


def _generate_id() -> str:
    ts = int(time.time())
    rand_hex = os.urandom(2).hex()
    return f"bc_{ts}_{rand_hex}"


def _load_messages() -> list[dict]:
    if not CHANNEL_FILE.exists():
        return []
    try:
        data = json.loads(CHANNEL_FILE.read_text())
        return data.get("messages", [])
    except Exception:
        return []


def _save_messages(messages: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Keep only last MAX_MESSAGES
    messages = messages[-MAX_MESSAGES:]
    try:
        CHANNEL_FILE.write_text(json.dumps({
            "messages": messages,
            "count": len(messages),
            "updated": datetime.now(ET).isoformat(),
        }, indent=2, default=str))
    except Exception:
        log.exception("Failed to save brand channel")


def _publish_event(event_type: str, data: dict, summary: str = "") -> None:
    """Publish to shared event bus (best-effort)."""
    try:
        from shared.events import publish
        publish(
            agent="brand_channel",
            event_type=f"brand_channel.{event_type}",
            data=data,
            severity="info",
            summary=summary,
        )
    except Exception:
        log.debug("Event bus publish failed (non-critical)")


def submit_opportunity(opportunity: dict) -> dict:
    """Submit an opportunity to the brand channel.

    Calls brand_filter.assess_brand_fit() for GPT-4o assessment,
    creates a channel message, and publishes an event.

    Args:
        opportunity: dict from Viper's soren_scout (has id, title, description, type, etc.)

    Returns:
        The created channel message dict.
    """
    from shared.brand_filter import assess_brand_fit

    msg_id = _generate_id()
    now = datetime.now(ET).isoformat()

    # Check for duplicate (same opportunity ID already in channel)
    with _lock:
        existing = _load_messages()
        opp_id = opportunity.get("id", "")
        if opp_id:
            for m in existing:
                if m.get("opportunity_id") == opp_id:
                    return m  # Already submitted

    # Run brand assessment
    assessment = assess_brand_fit(opportunity)
    verdict = assessment.get("auto_verdict", "needs_review")

    # Determine initial status based on verdict
    if verdict == "auto_approved":
        status = "approved"
    elif verdict == "auto_rejected":
        status = "rejected"
    else:
        status = "assessed"  # needs_review -> stays as assessed until Jordan acts

    message = {
        "id": msg_id,
        "opportunity_id": opp_id,
        "sender": "viper",
        "recipients": ["soren", "lisa"],
        "type": "opportunity_submitted",
        "timestamp": now,
        "opportunity": {
            "title": opportunity.get("title", ""),
            "description": opportunity.get("description", ""),
            "type": opportunity.get("type", ""),
            "category": opportunity.get("category", ""),
            "url": opportunity.get("url", ""),
            "source": opportunity.get("source", ""),
            "estimated_value": opportunity.get("estimated_value", ""),
            "fit_score": opportunity.get("fit_score", 0),
        },
        "brand_assessment": assessment,
        "status": status,
        "status_history": [
            {"status": "new", "at": now, "by": "viper"},
            {"status": status, "at": now, "by": "brand_filter"},
        ],
        "lisa_action": None,
    }

    with _lock:
        messages = _load_messages()
        messages.append(message)
        _save_messages(messages)

    # Publish event
    score = assessment.get("brand_fit_score", 0)
    _publish_event(
        "submitted",
        {"message_id": msg_id, "score": score, "verdict": verdict,
         "title": opportunity.get("title", "")[:80]},
        f"Brand opp: {opportunity.get('title', '')[:50]} — {verdict} ({score}/100)",
    )

    return message


def get_messages(
    status: str | None = None,
    recipient: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read channel messages with optional filters. Returns newest first."""
    messages = _load_messages()

    if status:
        messages = [m for m in messages if m.get("status") == status]
    if recipient:
        messages = [m for m in messages if recipient in m.get("recipients", [])]

    return list(reversed(messages[-limit:]))


def update_status(
    message_id: str,
    new_status: str,
    by: str,
    notes: str = "",
) -> dict | None:
    """Update a channel message's status (approve, reject, content_planned).

    Args:
        message_id: The channel message ID (bc_...)
        new_status: approved | rejected | content_planned
        by: Who is updating (jordan, lisa)
        notes: Optional notes (e.g. rejection reason)

    Returns:
        Updated message dict, or None if not found.
    """
    with _lock:
        messages = _load_messages()
        for msg in messages:
            if msg["id"] == message_id:
                msg["status"] = new_status
                entry = {
                    "status": new_status,
                    "at": datetime.now(ET).isoformat(),
                    "by": by,
                }
                if notes:
                    entry["notes"] = notes
                msg.setdefault("status_history", []).append(entry)

                if new_status == "content_planned":
                    msg["lisa_action"] = {
                        "planned_at": datetime.now(ET).isoformat(),
                        "notes": notes,
                    }

                _save_messages(messages)

                _publish_event(
                    new_status,
                    {"message_id": message_id, "by": by, "notes": notes},
                    f"Brand opp {message_id} -> {new_status} by {by}",
                )
                return msg

    return None


def get_channel_stats() -> dict:
    """Get pipeline counts by status."""
    messages = _load_messages()
    counts: dict[str, int] = {}
    for m in messages:
        s = m.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    return {
        "total": len(messages),
        "by_status": counts,
        "approved": counts.get("approved", 0),
        "assessed": counts.get("assessed", 0),  # needs_review
        "rejected": counts.get("rejected", 0),
        "content_planned": counts.get("content_planned", 0),
    }
