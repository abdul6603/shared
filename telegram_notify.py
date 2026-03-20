"""Brotherhood Telegram Notification System.

Reference: ~/Desktop/tg design.rtf

Rules:
    - Every message = 2-3 lines MAX
    - Every message starts with emoji type tag
    - Every message answers: WHO did WHAT and WHAT SHOULD JORDAN DO
    - No walls of text. No reasoning dumps. No JSON.
    - Urgent = send immediately. Non-urgent = batch into daily digest at 9am ET.
    - Questions always end with clear call-to-action

Usage:
    from telegram_notify import notify, NotifyType, Urgency, send_daily_digest

    notify(NotifyType.MONEY, "Viper: New lead — AI chatbot for dental. $4,550. Check dashboard.")
    notify(NotifyType.QUESTION, "Claude asks: Approve $400 bid? Reply YES or NO.", Urgency.IMMEDIATE)
    notify(NotifyType.CONTENT, "TikTok: +200 followers. Engagement 4.2%.", Urgency.DIGEST)
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path

log = logging.getLogger("shared.telegram_notify")

ET = timezone(timedelta(hours=-5))


# ── Enums ──

class NotifyType(Enum):
    MONEY = "\U0001f4b0"       # 💰 Leads, Revenue, Payments
    ALERT = "\u26a0\ufe0f"     # ⚠️ Problems, Failures, Warnings
    CLAUDE = "\U0001f9e0"      # 🧠 Overseer Decisions
    QUESTION = "\u2753"        # ❓ Needs Jordan's Answer
    TASK = "\U0001f4cb"        # 📋 Thor Queue Updates
    CONTENT = "\U0001f4f1"     # 📱 Posting & Metrics
    CLIENT = "\U0001f464"      # 👤 CRM Alerts
    HEALTH = "\U0001f3e5"      # 🏥 System Status
    DIGEST = "\U0001f4ca"      # 📊 Daily Digest header


class Urgency(Enum):
    IMMEDIATE = "immediate"
    NORMAL = "normal"
    DIGEST = "digest"


# ── Urgency defaults per type ──
_DEFAULT_URGENCY = {
    NotifyType.MONEY: Urgency.IMMEDIATE,
    NotifyType.ALERT: Urgency.IMMEDIATE,
    NotifyType.CLAUDE: Urgency.NORMAL,
    NotifyType.QUESTION: Urgency.IMMEDIATE,
    NotifyType.TASK: Urgency.DIGEST,
    NotifyType.CONTENT: Urgency.DIGEST,
    NotifyType.CLIENT: Urgency.DIGEST,
    NotifyType.HEALTH: Urgency.DIGEST,
}


# ── Config ──

_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not _TOKEN:
    _env_path = Path.home() / "shelby" / ".env"
    if _env_path.exists():
        for line in _env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip("'\"")
            if key == "TELEGRAM_BOT_TOKEN":
                _TOKEN = val
            elif key == "TELEGRAM_CHAT_ID":
                _CHAT_ID = val

# ── Storage ──

_DATA_DIR = Path.home() / "claude_overseer" / "data"
_DIGEST_FILE = _DATA_DIR / "tg_digest_queue.json"
_DEDUP_FILE = _DATA_DIR / "tg_dedup.json"

# Rate limiting
_last_send_time = 0.0
_MIN_INTERVAL = 1.0


# ── Core ──

def _send_raw(text: str) -> bool:
    """Send raw message to Telegram."""
    global _last_send_time

    if not _TOKEN or not _CHAT_ID:
        log.warning("Telegram not configured (no token/chat_id)")
        return False

    elapsed = time.time() - _last_send_time
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)

    try:
        url = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": _CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        _last_send_time = time.time()
        return True
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = e.headers.get("Retry-After", "unknown")
            log.warning("Telegram rate limited (429), retry after %s seconds", retry_after)
        else:
            log.error("Telegram HTTP error %d: %s", e.code, str(e)[:100])
        return False
    except Exception as e:
        log.error("Telegram send failed: %s", str(e)[:100])
        return False


def _is_duplicate(message: str) -> bool:
    """Check if this exact message was sent in last 4 hours."""
    dedup = {}
    if _DEDUP_FILE.exists():
        try:
            dedup = json.loads(_DEDUP_FILE.read_text())
        except Exception:
            dedup = {}

    now = time.time()
    # Clean old entries (>4h)
    dedup = {k: v for k, v in dedup.items() if now - v < 14400}

    key = message[:200]
    if key in dedup:
        return True

    dedup[key] = now
    _DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DEDUP_FILE.write_text(json.dumps(dedup))
    return False


def notify(
    type: NotifyType,
    message: str,
    urgency: Urgency | None = None,
) -> bool:
    """Send a Telegram notification to Jordan.

    Args:
        type: NotifyType enum — determines emoji prefix
        message: Short message (2-3 lines max, no JSON, no walls of text)
        urgency: Urgency enum — immediate sends now, digest batches for 9am.
                 If None, uses the default urgency for the type.

    Rules:
        - Message should be < 280 characters
        - Must answer: WHO did WHAT and WHAT SHOULD JORDAN DO
        - Questions must end with clear options (YES/NO, check dashboard, etc.)
        - Never include raw JSON, full reasoning, or technical errors
    """
    if urgency is None:
        urgency = _DEFAULT_URGENCY.get(type, Urgency.NORMAL)

    formatted = f"{type.value} {message}"

    # Dedup check
    if _is_duplicate(formatted):
        log.debug("Skipping duplicate notification")
        return True

    if urgency in (Urgency.IMMEDIATE, Urgency.NORMAL):
        return _send_raw(formatted)
    else:
        _queue_for_digest(formatted, type.name)
        return True


def _queue_for_digest(formatted: str, type_name: str):
    """Add message to digest queue for 9am ET send."""
    queue = []
    if _DIGEST_FILE.exists():
        try:
            queue = json.loads(_DIGEST_FILE.read_text())
        except Exception:
            queue = []

    queue.append({
        "message": formatted,
        "type": type_name,
        "timestamp": datetime.now(ET).isoformat(),
    })

    queue = queue[-50:]
    _DIGEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DIGEST_FILE.write_text(json.dumps(queue, indent=2))


def send_daily_digest() -> bool:
    """Send daily digest. Called by cron at 9am ET.

    Format:
        📊 BROTHERHOOD DAILY DIGEST — Mar 6, 2026

        💰 Revenue: $X MTD | Costs: $X | Net: $X
        🤖 Agents: X active, X paused, X down
        🧠 Claude: Ran X cycles. X decisions. X errors.
        📋 Thor: X tasks done, X in progress, X queued
        📱 Content: X posts yesterday. TikTok +X, LinkedIn +X
        💰 Viper: X new leads (best: $X, score X)
        👤 Clients: X active retainers. Next renewal: X.

        Check dashboard for details.
    """
    if not _DIGEST_FILE.exists():
        return False

    try:
        queue = json.loads(_DIGEST_FILE.read_text())
    except Exception:
        return False

    if not queue:
        return False

    today = datetime.now(ET).strftime("%b %-d, %Y")
    header = f"{NotifyType.DIGEST.value} BROTHERHOOD DAILY DIGEST — {today}\n\n"

    # Collect unique messages, ordered by type priority
    type_order = ["MONEY", "ALERT", "CLAUDE", "QUESTION", "TASK", "CONTENT", "CLIENT", "HEALTH"]
    by_type: dict[str, list[str]] = {}
    for item in queue:
        t = item.get("type", "HEALTH")
        by_type.setdefault(t, []).append(item["message"])

    lines = []
    for t in type_order:
        msgs = by_type.get(t, [])
        for msg in msgs[-3:]:  # Max 3 per type
            lines.append(msg)

    if not lines:
        return False

    body = "\n".join(lines[:15])  # Cap at 15 items
    full_msg = f"{header}{body}\n\nCheck dashboard for details."

    sent = _send_raw(full_msg)
    if sent:
        _DIGEST_FILE.write_text("[]")
        log.info("Daily digest sent: %d items", len(queue))

    return sent


# ── Convenience functions ──

def notify_lead(title: str, amount: str = "", score: float = 0, service: str = "") -> bool:
    """Viper lead notification — always immediate if score >= 7.5."""
    parts = [f"Viper: New lead — {title}."]
    if amount:
        parts.append(f"${amount}.")
    if score:
        parts.append(f"{score:.0f}/10 fit.")
    if service:
        parts.append(f"{service}.")
    parts.append("Check dashboard.")

    urgency = Urgency.IMMEDIATE if score >= 7.5 else Urgency.DIGEST
    return notify(NotifyType.MONEY, " ".join(parts), urgency)


def notify_question(question: str) -> bool:
    """Claude question — always immediate, must have clear options."""
    return notify(NotifyType.QUESTION, f"Claude asks: {question}", Urgency.IMMEDIATE)


def notify_claude_cycle(summary: str) -> bool:
    """Claude overseer cycle summary — normal urgency."""
    return notify(NotifyType.CLAUDE, f"Claude: {summary}", Urgency.NORMAL)


def notify_alert(agent: str, issue: str, action: str = "") -> bool:
    """Alert — always immediate."""
    msg = f"{agent}: {issue}"
    if action:
        msg += f" → {action}"
    return notify(NotifyType.ALERT, msg, Urgency.IMMEDIATE)


def notify_task(agent: str, task: str, status: str, due: str = "") -> bool:
    """Task update — digest unless deadline < 24h."""
    msg = f"{agent}: {task} — {status}."
    if due:
        msg += f" Due {due}."
    return notify(NotifyType.TASK, msg, Urgency.DIGEST)


def notify_content(platform: str, metric: str) -> bool:
    """Content metric — always digest."""
    return notify(NotifyType.CONTENT, f"{platform}: {metric}", Urgency.DIGEST)


def notify_client(client: str, status: str) -> bool:
    """Client CRM alert — immediate if retainer expiring < 7 days."""
    return notify(NotifyType.CLIENT, f"{client}: {status}")


def notify_health(message: str, critical: bool = False) -> bool:
    """System health — immediate if critical."""
    urgency = Urgency.IMMEDIATE if critical else Urgency.DIGEST
    return notify(NotifyType.HEALTH, f"System: {message}", urgency)
