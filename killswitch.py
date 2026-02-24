"""Brotherhood Kill Switch — shared emergency stop for ALL trading agents.

Usage:
    from shared.killswitch import is_killed, activate_killswitch, clear_killswitch

Kill switch file: /tmp/brotherhood_killswitch
- When present, ALL trading agents halt (no new trades, existing positions stay)
- Contains JSON: {"reason": "...", "activated_by": "...", "timestamp": "...", "expires_at": "..."}
- Auto-expires after 24h to prevent stale lockouts
- Shelby /killswitch TG command activates it
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
KILLSWITCH_FILE = Path("/tmp/brotherhood_killswitch")
DEFAULT_EXPIRY_HOURS = 24


def activate_killswitch(
    reason: str = "Manual kill switch",
    activated_by: str = "unknown",
    expiry_hours: float = DEFAULT_EXPIRY_HOURS,
) -> dict:
    """Activate the brotherhood-wide kill switch. All trading agents will halt."""
    ts = datetime.now(ET).isoformat()
    expires_at = time.time() + (expiry_hours * 3600)
    payload = {
        "reason": reason,
        "activated_by": activated_by,
        "timestamp": ts,
        "activated_epoch": time.time(),
        "expires_at": expires_at,
        "expiry_hours": expiry_hours,
    }
    KILLSWITCH_FILE.write_text(json.dumps(payload, indent=2))
    log.critical("[KILLSWITCH] ACTIVATED by %s: %s (expires in %.0fh)",
                 activated_by, reason, expiry_hours)

    try:
        import sys
        sys.path.insert(0, str(Path.home() / "shared"))
        from events import publish
        publish(
            agent="killswitch",
            event_type="killswitch_activated",
            data=payload,
            summary=f"KILL SWITCH by {activated_by}: {reason}",
        )
    except Exception:
        pass

    return payload


def clear_killswitch(cleared_by: str = "unknown") -> dict:
    """Clear the kill switch — resume all trading."""
    if KILLSWITCH_FILE.exists():
        try:
            old = json.loads(KILLSWITCH_FILE.read_text())
        except Exception:
            old = {}
        KILLSWITCH_FILE.unlink()
        log.info("[KILLSWITCH] CLEARED by %s (was: %s)", cleared_by, old.get("reason", "?"))

        try:
            import sys
            sys.path.insert(0, str(Path.home() / "shared"))
            from events import publish
            publish(
                agent="killswitch",
                event_type="killswitch_cleared",
                data={"cleared_by": cleared_by, "was_reason": old.get("reason", "?")},
                summary=f"Kill switch cleared by {cleared_by}",
            )
        except Exception:
            pass

        return {"cleared": True, "was_reason": old.get("reason", "?")}
    return {"cleared": False, "message": "Kill switch was not active"}


def is_killed() -> dict | None:
    """Check if the kill switch is active. Returns kill info or None.

    Auto-clears expired kill switches (default 24h).
    """
    if not KILLSWITCH_FILE.exists():
        return None

    try:
        data = json.loads(KILLSWITCH_FILE.read_text())
    except Exception:
        return {"reason": "corrupted killswitch file", "activated_by": "unknown"}

    expires_at = data.get("expires_at", 0)
    if expires_at > 0 and time.time() > expires_at:
        log.info("[KILLSWITCH] Auto-expired after %.0fh", data.get("expiry_hours", 24))
        KILLSWITCH_FILE.unlink(missing_ok=True)
        return None

    return data


def killswitch_status() -> dict:
    """Get kill switch status for dashboard/API."""
    info = is_killed()
    if info:
        elapsed = time.time() - info.get("activated_epoch", time.time())
        expires_in = info.get("expires_at", 0) - time.time()
        return {
            "active": True,
            "reason": info.get("reason", "?"),
            "activated_by": info.get("activated_by", "?"),
            "timestamp": info.get("timestamp", "?"),
            "elapsed_minutes": round(elapsed / 60, 1),
            "expires_in_hours": round(max(0, expires_in) / 3600, 1),
        }
    return {"active": False}
