"""Jordan Tasks — manual action tracker for the Brotherhood.

Tracks tasks that require Jordan's manual intervention to activate features.
Claude Overseer reads this every loop. Shelby shows it on /tasks.

Priority system:
  urgent       — Alert immediately + standalone reminder every 12h (max 1/day)
  blocking     — Alert immediately + daily reminder in 9am digest
  nice_to_have — Alert once + mentioned in weekly digest only
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("shared.jordan_tasks")

ET = timezone(timedelta(hours=-5))

TASKS_FILE = Path.home() / "polymarket-bot" / "data" / "jordan_tasks.json"
OVERSEER_COPY = Path.home() / "claude_overseer" / "data" / "jordan_tasks.json"

URGENT = "urgent"
BLOCKING = "blocking"
NICE_TO_HAVE = "nice_to_have"

_URGENT_KEYWORDS = [
    "revenue", "earning", "money", "live trading", "orders",
    "can't receive", "no revenue", "real money",
]

# Killed tasks — never show these regardless of JSON status
_KILLED_IDS = {"jt_006", "jt_007", "jt_008", "jt_009", "jt_012", "jt_013"}

# Dead agent specs — auto-filter
_DEAD_SPECS = {"Odin Agent Build", "Hawk Agent Build", "Killshot Agent Build",
               "Oracle Agent Build", "Mercury Agent Build", "Pro Infrastructure"}

_PRIORITY_ICON = {URGENT: "\U0001f534", BLOCKING: "\U0001f7e1", NICE_TO_HAVE: "\U0001f7e2"}
_SEP = "\u2501" * 20


def _load() -> dict:
    if not TASKS_FILE.exists():
        return {"tasks": [], "updated_at": None}
    try:
        return json.loads(TASKS_FILE.read_text())
    except Exception:
        return {"tasks": [], "updated_at": None}


def _save(data: dict) -> None:
    data["updated_at"] = datetime.now(ET).isoformat()
    output = json.dumps(data, indent=2)
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(output)
    if OVERSEER_COPY.parent.exists():
        OVERSEER_COPY.write_text(output)


def _next_id(tasks: list[dict]) -> str:
    existing = [t.get("id", "") for t in tasks]
    nums = []
    for tid in existing:
        try:
            nums.append(int(tid.replace("jt_", "")))
        except (ValueError, AttributeError):
            pass
    next_num = max(nums, default=0) + 1
    return f"jt_{next_num:03d}"


def _auto_priority(blocking: str) -> str:
    if not blocking:
        return NICE_TO_HAVE
    lower = blocking.lower()
    for kw in _URGENT_KEYWORDS:
        if kw in lower:
            return URGENT
    return BLOCKING


def _get_priority(task: dict) -> str:
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path.home() / "shared"))
        from jordan_tasks_detector import get_priority
        p = get_priority(task.get("id", ""))
        if p:
            return p
    except Exception:
        pass
    return task.get("priority", _auto_priority(task.get("blocking", "")))


def _hours_since(iso_str) -> float:
    if not iso_str:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return (datetime.now(ET) - dt).total_seconds() / 3600
    except Exception:
        return float("inf")


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _parse_minutes(est: str) -> int:
    try:
        num = int("".join(c for c in est if c.isdigit()))
        if "hour" in est.lower() or "hr" in est.lower():
            return num * 60
        return num
    except (ValueError, TypeError):
        return 10


def _total_time_str(tasks: list[dict]) -> str:
    total = sum(_parse_minutes(t.get("time_estimate", "")) for t in tasks)
    if total >= 60:
        return f"{total // 60}h {total % 60}m"
    return f"{total}m"


def _is_dead(task: dict) -> bool:
    tid = task.get("id", "")
    if tid in _KILLED_IDS:
        return True
    if task.get("spec", "") in _DEAD_SPECS:
        return True
    if task.get("status") == "removed":
        return True
    return False


# Short descriptions for clean mobile display
_SHORT_DESC = {
    "jt_001": "Create Reddit API app \u2192 reddit.com/prefs/apps",
    "jt_002": "Sign up at vollna.com \u2192 set Upwork filters",
    "jt_003": "Create 6 Google Alerts \u2192 alerts.google.com",
    "jt_004": "Check Fiverr tax approval \u2192 publish gigs",
    "jt_005": "Complete Upwork profile (DarkCode AI)",
    "jt_010": "Sign up at hunter.io (25 free lookups/mo)",
    "jt_011": "Create directory listings (Contra, Clutch, etc)",
    "jt_014": "Sign up for Apify ($5/mo) → add token to .env",
    "jt_015": "Create affiliate accounts (Audible, BetterHelp, Amazon, Onnit)",
    "jt_016": "Set up Stan Store / Beacons link-in-bio page",
    "jt_017": "Add IG @the.soren.era to Buffer",
    "jt_018": "Source 25+ dark instrumentals for audio library",
    "jt_019": "Add link-in-bio URL to TikTok + IG bios",
}

_SHORT_BLOCKING = {
    "jt_001": "Reddit lead scanning",
    "jt_002": "Upwork monitoring",
    "jt_003": "Passive lead alerts",
    "jt_004": "Fiverr revenue",
    "jt_005": "Upwork lead flow",
    "jt_010": "Cold outreach pipeline",
    "jt_011": "Inbound lead generation",
    "jt_014": "Trending sound discovery",
    "jt_015": "Soren monetization",
    "jt_016": "Affiliate revenue capture",
    "jt_017": "IG auto-posting",
    "jt_018": "Audio brain",
    "jt_019": "Lead capture from bios",
}


def _desc(task: dict) -> str:
    return _SHORT_DESC.get(task.get("id", ""), task.get("task", "?")[:55])


def _block(task: dict) -> str:
    return _SHORT_BLOCKING.get(task.get("id", ""), task.get("blocking", "")[:35])


# ── Public API ──

def add_task(
    task: str,
    spec: str,
    phase: str,
    blocking: str,
    time_estimate: str = "10 min",
    priority: str = None,
    notify: bool = True,
) -> str:
    data = _load()
    tasks = data.get("tasks", [])

    for t in tasks:
        if t.get("task") == task and t.get("status") == "pending":
            return t["id"]

    if priority is None:
        priority = _auto_priority(blocking)

    task_id = _next_id(tasks)
    entry = {
        "id": task_id,
        "task": task,
        "spec": spec,
        "phase": phase,
        "blocking": blocking,
        "priority": priority,
        "status": "pending",
        "reminded_at": None,
        "completed_at": None,
        "time_estimate": time_estimate,
    }
    tasks.append(entry)
    data["tasks"] = tasks
    _save(data)

    log.info("[JORDAN_TASKS] Added %s [%s]: %s", task_id, priority, task[:60])

    if notify:
        _send_standalone(entry)

    return task_id


def complete_task(task_id: str) -> bool:
    data = _load()
    for t in data.get("tasks", []):
        if t.get("id") == task_id and t.get("status") == "pending":
            t["status"] = "completed"
            t["completed_at"] = datetime.now(ET).isoformat()
            _save(data)
            log.info("[JORDAN_TASKS] Completed: %s", task_id)
            return True
    return False


def get_pending() -> list[dict]:
    data = _load()
    return [t for t in data.get("tasks", [])
            if t.get("status") == "pending" and not _is_dead(t)]


def get_blocking() -> list[dict]:
    return [t for t in get_pending() if t.get("blocking")]


def get_by_spec(spec: str) -> list[dict]:
    data = _load()
    return [t for t in data.get("tasks", []) if t.get("spec") == spec]


# ── Priority-Aware Reminder System ──

def process_reminders() -> dict:
    data = _load()
    now = datetime.now(ET)
    result = {"standalone": 0, "daily_digest": False, "weekly_digest": False}
    changed = False

    pending = [t for t in data.get("tasks", [])
               if t.get("status") == "pending" and not _is_dead(t)]
    if not pending:
        return result

    # 1. Urgent standalone (every 12h, max 1/day/task)
    for t in pending:
        if _get_priority(t) != URGENT:
            continue
        hours = _hours_since(t.get("reminded_at"))
        last_date = ""
        if t.get("reminded_at"):
            try:
                last_date = datetime.fromisoformat(t["reminded_at"]).strftime("%Y-%m-%d")
            except Exception:
                pass
        if hours >= 12 and last_date != _today_et():
            _send_standalone(t)
            t["reminded_at"] = now.isoformat()
            result["standalone"] += 1
            changed = True

    # 2. Daily digest (9-10am ET)
    if 9 <= now.hour <= 10:
        last_daily = data.get("last_daily_digest")
        daily_date = ""
        if last_daily:
            try:
                daily_date = datetime.fromisoformat(last_daily).strftime("%Y-%m-%d")
            except Exception:
                pass

        if daily_date != _today_et():
            digest = [t for t in pending if _get_priority(t) in (URGENT, BLOCKING)]
            if digest:
                _send_daily_digest(digest)
                data["last_daily_digest"] = now.isoformat()
                result["daily_digest"] = True
                changed = True

            # 3. Weekly (Monday)
            if now.weekday() == 0:
                last_weekly = data.get("last_weekly_digest")
                weekly_date = ""
                if last_weekly:
                    try:
                        weekly_date = datetime.fromisoformat(last_weekly).strftime("%Y-%m-%d")
                    except Exception:
                        pass
                if weekly_date != _today_et():
                    nice = [t for t in pending if _get_priority(t) == NICE_TO_HAVE]
                    if nice:
                        _send_weekly_digest(nice)
                        data["last_weekly_digest"] = now.isoformat()
                        result["weekly_digest"] = True
                        changed = True

    if changed:
        _save(data)

    log.info("[JORDAN_TASKS] Reminders: %d standalone, daily=%s, weekly=%s",
             result["standalone"], result["daily_digest"], result["weekly_digest"])
    return result


def remind_pending(max_reminders: int = 3) -> int:
    result = process_reminders()
    return result["standalone"] + (1 if result["daily_digest"] else 0)


# ── Display (Shelby /tasks) ──

def format_pending_text() -> str:
    pending = get_pending()
    if not pending:
        return ""

    now = datetime.now(ET)
    date_str = now.strftime("%b %-d, %Y")

    urgent = [t for t in pending if _get_priority(t) == URGENT]
    blocking = [t for t in pending if _get_priority(t) == BLOCKING]
    nice = [t for t in pending if _get_priority(t) == NICE_TO_HAVE]

    lines = [f"\U0001f4cb <b>YOUR TASKS</b> \u2014 {date_str}\n"]
    num = 0

    if urgent:
        lines.append(f"\U0001f534 <b>URGENT ({len(urgent)}):</b>\n")
        for t in urgent[:5]:
            num += 1
            lines.append(f"{num}. {_desc(t)}")
            lines.append(f"   \u23f1 {t.get('time_estimate', '?')} | Blocking: {_block(t)}\n")

    if blocking:
        lines.append(f"\U0001f7e1 <b>BLOCKING ({len(blocking)}):</b>\n")
        for t in blocking[:5]:
            num += 1
            lines.append(f"{num}. {_desc(t)}")
            lines.append(f"   \u23f1 {t.get('time_estimate', '?')} | Blocking: {_block(t)}\n")

    if nice:
        lines.append(f"\U0001f7e2 <b>NICE TO HAVE ({len(nice)}):</b>\n")
        for t in nice[:3]:
            num += 1
            lines.append(f"{num}. {_desc(t)}")
            lines.append(f"   \u23f1 {t.get('time_estimate', '?')}\n")

    shown = num
    overflow = len(pending) - shown
    lines.append(_SEP)
    lines.append(f"{len(pending)} tasks | ~{_total_time_str(pending)} total")
    if overflow > 0:
        lines.append(f"+{overflow} more \u2014 check /tasks for full list")
    lines.append('Say "done [task]" when complete.')

    return "\n".join(lines)


# ── Notifications ──

def _send_standalone(task: dict) -> None:
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path.home() / "shared"))
        from telegram_notify import notify, NotifyType, Urgency

        priority = _get_priority(task)
        icon = _PRIORITY_ICON.get(priority, "\U0001f4cb")
        tid = task.get("id", "")

        msg = (
            f"{icon} {_desc(task)}\n"
            f"\u23f1 {task.get('time_estimate', '?')} | Blocking: {_block(task)}\n"
            f'Say "done {tid}" when complete.'
        )
        notify(NotifyType.TASK, msg, Urgency.IMMEDIATE)
    except Exception as e:
        log.warning("[JORDAN_TASKS] TG notification failed: %s", str(e)[:100])


def _send_daily_digest(tasks: list[dict]) -> None:
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path.home() / "shared"))
        from telegram_notify import notify, NotifyType, Urgency

        now = datetime.now(ET)
        date_str = now.strftime("%b %-d, %Y")

        urgent = [t for t in tasks if _get_priority(t) == URGENT]
        blocking = [t for t in tasks if _get_priority(t) == BLOCKING]

        lines = [f"\U0001f4cb DAILY TASKS \u2014 {date_str}\n"]
        num = 0
        shown = 0

        if urgent:
            lines.append(f"\U0001f534 URGENT ({len(urgent)}):\n")
            for t in urgent[:5]:
                num += 1
                shown += 1
                lines.append(f"{num}. {_desc(t)}")
                lines.append(f"   \u23f1 {t.get('time_estimate', '?')} | Blocking: {_block(t)}\n")

        if blocking and shown < 10:
            lines.append(f"\U0001f7e1 BLOCKING ({len(blocking)}):\n")
            for t in blocking[:min(5, 10 - shown)]:
                num += 1
                shown += 1
                lines.append(f"{num}. {_desc(t)}")
                lines.append(f"   \u23f1 {t.get('time_estimate', '?')} | Blocking: {_block(t)}\n")

        overflow = len(tasks) - shown
        lines.append(_SEP)
        lines.append(f"{len(tasks)} tasks | ~{_total_time_str(tasks)} total")
        if overflow > 0:
            lines.append(f"+{overflow} more \u2014 check /tasks for full list")
        lines.append('Say "done [task]" when complete.')

        msg = "\n".join(lines)
        notify(NotifyType.TASK, msg, Urgency.IMMEDIATE)
        log.info("[JORDAN_TASKS] Daily digest: %d urgent, %d blocking", len(urgent), len(blocking))
    except Exception as e:
        log.warning("[JORDAN_TASKS] Daily digest failed: %s", str(e)[:100])


def _send_weekly_digest(tasks: list[dict]) -> None:
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path.home() / "shared"))
        from telegram_notify import notify, NotifyType, Urgency

        lines = ["\U0001f7e2 WEEKLY NICE-TO-HAVE TASKS:\n"]
        for i, t in enumerate(tasks[:5], 1):
            lines.append(f"{i}. {_desc(t)}")
            lines.append(f"   \u23f1 {t.get('time_estimate', '?')}\n")

        lines.append(_SEP)
        lines.append(f"{len(tasks)} optional tasks | ~{_total_time_str(tasks)} total")
        lines.append("Do these when you have downtime.")

        msg = "\n".join(lines)
        notify(NotifyType.TASK, msg, Urgency.IMMEDIATE)
        log.info("[JORDAN_TASKS] Weekly digest: %d nice-to-have", len(tasks))
    except Exception as e:
        log.warning("[JORDAN_TASKS] Weekly digest failed: %s", str(e)[:100])


# Legacy alias
_send_notification = _send_standalone
