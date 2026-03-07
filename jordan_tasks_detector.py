"""Jordan Tasks Auto-Detection Engine.

Detection rules stored in _TASK_RULES (not in jordan_tasks.json).
Called every Claude Overseer loop. Auto-marks tasks complete.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("shared.jordan_tasks_detector")

ET = timezone(timedelta(hours=-5))

TASKS_FILE = Path.home() / "polymarket-bot" / "data" / "jordan_tasks.json"
OVERSEER_COPY = Path.home() / "claude_overseer" / "data" / "jordan_tasks.json"

# Detection rules for LIVE tasks only.
# Dead agent tasks (Odin, Hawk, Killshot, Oracle, Freelancer) removed.
_TASK_RULES = {
    "jt_001": {
        "detect_type": "env_keys",
        "detect_keys": ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"],
        "detect_env_file": "~/polymarket-bot/.env",
        "on_complete": "Viper Reddit scanning activated",
        "match_keywords": ["reddit"],
        "priority": "blocking",
    },
    "jt_002": {
        "detect_type": "manual",
        "on_complete": "Vollna/Upwork webhook monitoring activated",
        "match_keywords": ["vollna", "upwork"],
        "priority": "blocking",
    },
    "jt_003": {
        "detect_type": "env_keys",
        "detect_keys": ["GOOGLE_ALERT_FEEDS"],
        "detect_env_file": "~/polymarket-bot/.env",
        "on_complete": "Viper Google Alerts RSS scanning activated",
        "match_keywords": ["google", "alerts", "google alerts"],
        "priority": "blocking",
    },
    "jt_004": {
        "detect_type": "manual",
        "on_complete": "Fiverr gigs published and live",
        "match_keywords": ["fiverr"],
        "priority": "urgent",
    },
    "jt_005": {
        "detect_type": "manual",
        "on_complete": "Upwork profile live and ready for bids",
        "match_keywords": ["upwork", "profile"],
        "priority": "urgent",
    },
    "jt_010": {
        "detect_type": "env_keys",
        "detect_keys": ["HUNTER_API_KEY"],
        "detect_env_file": "~/polymarket-bot/.env",
        "on_complete": "Hunter.io ready for Viper Phase 2 cold outreach",
        "match_keywords": ["hunter", "hunter.io"],
        "priority": "nice_to_have",
    },
    "jt_011": {
        "detect_type": "manual",
        "on_complete": "Directory listings live for inbound lead generation",
        "match_keywords": ["directory", "clutch", "goodfirms", "contra", "designrush"],
        "priority": "nice_to_have",
    },
    "jt_014": {
        "detect_type": "env_keys",
        "detect_keys": ["APIFY_TOKEN"],
        "detect_env_file": "~/soren-content/.env",
        "on_complete": "Apify ready for trending sound discovery",
        "match_keywords": ["apify"],
        "priority": "blocking",
    },
    "jt_015": {
        "detect_type": "manual",
        "on_complete": "Affiliate accounts created for Soren monetization",
        "match_keywords": ["affiliate", "audible", "betterhelp", "amazon associates", "onnit"],
        "priority": "blocking",
    },
    "jt_016": {
        "detect_type": "manual",
        "on_complete": "Link-in-bio page live for affiliate revenue",
        "match_keywords": ["stan store", "beacons", "link-in-bio", "linkinbio"],
        "priority": "blocking",
    },
    "jt_017": {
        "detect_type": "manual",
        "on_complete": "Instagram connected to Buffer for auto-posting",
        "match_keywords": ["buffer", "instagram buffer"],
        "priority": "blocking",
    },
    "jt_018": {
        "detect_type": "file_exists",
        "detect_path": "~/soren-content/audio/rage",
        "on_complete": "Audio library populated for Soren reels",
        "match_keywords": ["instrumentals", "audio", "music library"],
        "priority": "nice_to_have",
    },
    "jt_019": {
        "detect_type": "manual",
        "on_complete": "Link-in-bio URLs added to TikTok and IG bios",
        "match_keywords": ["bio link", "link in bio", "tiktok bio", "ig bio"],
        "priority": "blocking",
    },
}


def get_task_rules(task_id):
    return _TASK_RULES.get(task_id, {})


def get_all_rules():
    return _TASK_RULES.copy()


def get_priority(task_id):
    return _TASK_RULES.get(task_id, {}).get("priority", "blocking")


def _load():
    if not TASKS_FILE.exists():
        return {"tasks": []}
    try:
        return json.loads(TASKS_FILE.read_text())
    except Exception:
        return {"tasks": []}


def _save(data):
    data["updated_at"] = datetime.now(ET).isoformat()
    output = json.dumps(data, indent=2)
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(output)
    if OVERSEER_COPY.parent.exists():
        OVERSEER_COPY.write_text(output)


def _parse_env_file(env_path):
    path = Path(os.path.expanduser(env_path))
    if not path.exists():
        return {}
    result = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'\"")
            result[key] = val
    except Exception:
        pass
    return result


def _check_env_keys(rule):
    env_file = rule.get("detect_env_file", "~/polymarket-bot/.env")
    env = _parse_env_file(env_file)
    keys = rule.get("detect_keys", [])
    if not keys:
        return False
    for key in keys:
        val = env.get(key, "")
        if not val or val in ("", "YOUR_KEY_HERE", "xxx", "CHANGE_ME"):
            return False
    return True


def _check_env_value(rule):
    env_file = rule.get("detect_env_file", "~/polymarket-bot/.env")
    env = _parse_env_file(env_file)
    keys = rule.get("detect_keys", [])
    expected = rule.get("detect_expected", "")
    if not keys or not expected:
        return False
    for key in keys:
        val = env.get(key, "")
        if val.lower().strip() != expected.lower().strip():
            return False
    return True


def _check_command(rule):
    cmd = rule.get("detect_command", "")
    expected = rule.get("detect_expected", "")
    if not cmd or not expected:
        return False
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10,
        )
        return expected.lower() in result.stdout.lower()
    except Exception:
        return False


def _check_file_exists(rule):
    path_str = rule.get("detect_path", "")
    if not path_str:
        return False
    return Path(os.path.expanduser(path_str)).exists()


_DETECTORS = {
    "env_keys": _check_env_keys,
    "env_value": _check_env_value,
    "command": _check_command,
    "file_exists": _check_file_exists,
}


def run_detection():
    """Check pending tasks for auto-detection completion."""
    data = _load()
    completed = []

    for task in data.get("tasks", []):
        if task.get("status") != "pending":
            continue

        task_id = task.get("id", "")
        rule = _TASK_RULES.get(task_id, {})
        detect_type = rule.get("detect_type", "manual")
        if detect_type == "manual":
            continue

        detector = _DETECTORS.get(detect_type)
        if not detector:
            continue

        try:
            if detector(rule):
                task["status"] = "completed"
                task["completed_at"] = datetime.now(ET).isoformat()
                task["completed_by"] = "auto_detected"
                completed.append({**task, **rule})
                log.info("[DETECTOR] Auto-completed %s: %s", task_id, task.get("task", "")[:50])
        except Exception as e:
            log.debug("[DETECTOR] Check failed for %s: %s", task_id, str(e)[:100])

    if completed:
        _save(data)
        _notify_completions(completed)

    return completed


def _notify_completions(tasks):
    try:
        import sys
        sys.path.insert(0, str(Path.home() / "shared"))
        from telegram_notify import notify, NotifyType, Urgency

        for task in tasks:
            on_complete = task.get("on_complete", task.get("task", "")[:50])
            detect_keys = task.get("detect_keys", [])
            detected_what = ", ".join(detect_keys) if detect_keys else task.get("phase", "")
            msg = f"Detected: {detected_what} added. {on_complete}."
            notify(NotifyType.HEALTH, msg, Urgency.IMMEDIATE)

    except Exception as e:
        log.warning("[DETECTOR] TG notification failed: %s", str(e)[:100])
