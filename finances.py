"""Brotherhood Financial Tracker — subscriptions, API costs, revenue.

Unified finance module. All agents import from here.
Claude Overseer reads via ingest.py. Shelby shows via /finances.
Dashboard tab at /api/finances.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("shared.finances")

ET = timezone(timedelta(hours=-5))

FINANCES_FILE = Path.home() / "polymarket-bot" / "data" / "finances.json"
OVERSEER_COPY = Path.home() / "claude_overseer" / "data" / "finances.json"
LLM_COSTS_FILE = Path.home() / "shared" / "llm_costs.jsonl"

_EMPTY: dict = {
    "subscriptions": [],
    "api_costs": [],
    "one_time_costs": [],
    "revenue": [],
    "updated_at": None,
}


# ── Load / Save ──────────────────────────────────────────────

def _load() -> dict:
    if not FINANCES_FILE.exists():
        return {k: list(v) if isinstance(v, list) else v for k, v in _EMPTY.items()}
    try:
        return json.loads(FINANCES_FILE.read_text())
    except Exception:
        return {k: list(v) if isinstance(v, list) else v for k, v in _EMPTY.items()}


def _save(data: dict) -> None:
    data["updated_at"] = datetime.now(ET).isoformat()
    output = json.dumps(data, indent=2)
    FINANCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    FINANCES_FILE.write_text(output)
    if OVERSEER_COPY.parent.exists():
        OVERSEER_COPY.write_text(output)


# ── ID generation ────────────────────────────────────────────

def _next_id(items: list[dict], prefix: str) -> str:
    nums = []
    for item in items:
        tid = item.get("id", "")
        try:
            nums.append(int(tid.replace(f"{prefix}_", "")))
        except (ValueError, AttributeError):
            pass
    return f"{prefix}_{max(nums, default=0) + 1:03d}"


# ── Subscriptions ────────────────────────────────────────────

def add_subscription(
    name: str,
    project: str,
    category: str,
    cost: float,
    billing_cycle: str = "monthly",
    next_billing: str = "",
    notes: str = "",
) -> str:
    """Add or update a subscription. Deduplicates by name. Returns sub_id."""
    data = _load()
    subs = data.setdefault("subscriptions", [])

    # Dedup by name
    for s in subs:
        if s.get("name", "").lower() == name.lower() and s.get("status") == "active":
            s.update(project=project, category=category, cost=cost,
                     billing_cycle=billing_cycle, next_billing=next_billing,
                     notes=notes)
            _save(data)
            return s["id"]

    sub_id = _next_id(subs, "sub")
    subs.append({
        "id": sub_id,
        "name": name,
        "project": project,
        "category": category,
        "cost": cost,
        "billing_cycle": billing_cycle,
        "next_billing": next_billing,
        "notes": notes,
        "status": "active",
        "added_at": datetime.now(ET).isoformat(),
    })
    _save(data)
    return sub_id


def remove_subscription(sub_id: str) -> bool:
    """Cancel a subscription by ID."""
    data = _load()
    for s in data.get("subscriptions", []):
        if s["id"] == sub_id:
            s["status"] = "cancelled"
            s["cancelled_at"] = datetime.now(ET).isoformat()
            _save(data)
            return True
    return False


# ── API Costs ────────────────────────────────────────────────

def add_api_cost(service: str, project: str, amount: float, details: str = "") -> None:
    """Append an API cost entry."""
    data = _load()
    costs = data.setdefault("api_costs", [])
    costs.append({
        "service": service,
        "project": project,
        "amount": round(amount, 4),
        "details": details,
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
        "timestamp": datetime.now(ET).isoformat(),
    })
    _save(data)


# ── One-Time Costs ───────────────────────────────────────────

def add_one_time_cost(item: str, project: str, amount: float, notes: str = "") -> None:
    """Append a one-time cost entry."""
    data = _load()
    costs = data.setdefault("one_time_costs", [])
    costs.append({
        "id": _next_id(costs, "otc"),
        "item": item,
        "project": project,
        "amount": round(amount, 2),
        "notes": notes,
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
    })
    _save(data)


# ── Revenue ──────────────────────────────────────────────────

def add_revenue(
    source: str,
    project: str,
    amount: float,
    client: str = "",
    revenue_type: str = "one-time",
    notes: str = "",
) -> None:
    """Append a revenue entry."""
    data = _load()
    rev = data.setdefault("revenue", [])
    rev.append({
        "id": _next_id(rev, "rev"),
        "source": source,
        "project": project,
        "amount": round(amount, 2),
        "client": client,
        "type": revenue_type,
        "notes": notes,
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
    })
    _save(data)


# ── Monthly Summary ──────────────────────────────────────────

def get_monthly_summary(month: str | None = None) -> dict:
    """Calculate live monthly summary. month format: '2026-03'."""
    if not month:
        month = datetime.now(ET).strftime("%Y-%m")

    data = _load()

    # Subscriptions (active, matching billing cycle)
    total_subs = 0.0
    for s in data.get("subscriptions", []):
        if s.get("status") != "active":
            continue
        cost = s.get("cost", 0)
        cycle = s.get("billing_cycle", "monthly")
        if cycle == "yearly":
            total_subs += cost / 12
        else:
            total_subs += cost

    # API costs this month
    total_api = 0.0
    for c in data.get("api_costs", []):
        if c.get("date", "").startswith(month):
            total_api += c.get("amount", 0)

    # One-time costs this month
    total_one_time = 0.0
    for c in data.get("one_time_costs", []):
        if c.get("date", "").startswith(month):
            total_one_time += c.get("amount", 0)

    # Revenue this month
    total_revenue = 0.0
    for r in data.get("revenue", []):
        if r.get("date", "").startswith(month):
            total_revenue += r.get("amount", 0)

    total_costs = total_subs + total_api + total_one_time
    net = total_revenue - total_costs

    # By-project breakdown
    projects: dict[str, dict] = {}
    for s in data.get("subscriptions", []):
        if s.get("status") != "active":
            continue
        p = s.get("project", "other")
        projects.setdefault(p, {"costs": 0, "revenue": 0})
        cost = s.get("cost", 0)
        if s.get("billing_cycle") == "yearly":
            cost /= 12
        projects[p]["costs"] += cost

    for c in data.get("api_costs", []):
        if c.get("date", "").startswith(month):
            p = c.get("project", "other")
            projects.setdefault(p, {"costs": 0, "revenue": 0})
            projects[p]["costs"] += c.get("amount", 0)

    for c in data.get("one_time_costs", []):
        if c.get("date", "").startswith(month):
            p = c.get("project", "other")
            projects.setdefault(p, {"costs": 0, "revenue": 0})
            projects[p]["costs"] += c.get("amount", 0)

    for r in data.get("revenue", []):
        if r.get("date", "").startswith(month):
            p = r.get("project", "other")
            projects.setdefault(p, {"costs": 0, "revenue": 0})
            projects[p]["revenue"] += r.get("amount", 0)

    # Round project values
    for p in projects:
        projects[p]["costs"] = round(projects[p]["costs"], 2)
        projects[p]["revenue"] = round(projects[p]["revenue"], 2)
        projects[p]["net"] = round(projects[p]["revenue"] - projects[p]["costs"], 2)

    return {
        "month": month,
        "total_subscriptions": round(total_subs, 2),
        "total_api": round(total_api, 2),
        "total_one_time": round(total_one_time, 2),
        "total_costs": round(total_costs, 2),
        "total_revenue": round(total_revenue, 2),
        "net": round(net, 2),
        "by_project": projects,
    }


# ── Upcoming Renewals ───────────────────────────────────────

def get_upcoming_renewals(days: int = 7) -> list[dict]:
    """Subscriptions renewing within N days."""
    data = _load()
    now = datetime.now(ET)
    cutoff = now + timedelta(days=days)
    upcoming = []

    for s in data.get("subscriptions", []):
        if s.get("status") != "active":
            continue
        nb = s.get("next_billing", "")
        if not nb:
            continue
        try:
            renewal = datetime.fromisoformat(nb)
            if renewal.tzinfo is None:
                renewal = renewal.replace(tzinfo=ET)
            if now <= renewal <= cutoff:
                upcoming.append({
                    "name": s["name"],
                    "project": s.get("project", ""),
                    "cost": s.get("cost", 0),
                    "renewal_date": nb,
                    "days_until": (renewal - now).days,
                })
        except (ValueError, TypeError):
            continue

    return sorted(upcoming, key=lambda x: x.get("days_until", 999))


# ── Alerts ───────────────────────────────────────────────────

def get_alerts() -> list[str]:
    """Check for financial alerts."""
    alerts = []
    data = _load()
    month = datetime.now(ET).strftime("%Y-%m")
    today = datetime.now(ET).strftime("%Y-%m-%d")

    # Upcoming renewals (7 days)
    renewals = get_upcoming_renewals(7)
    for r in renewals:
        alerts.append(f"Renewal in {r['days_until']}d: {r['name']} (${r['cost']:.2f})")

    # API cost today > $10
    today_api = sum(
        c.get("amount", 0) for c in data.get("api_costs", [])
        if c.get("date") == today
    )
    if today_api > 10:
        alerts.append(f"API spend today: ${today_api:.2f} (above $10 threshold)")

    # API cost this month > $200
    month_api = sum(
        c.get("amount", 0) for c in data.get("api_costs", [])
        if c.get("date", "").startswith(month)
    )
    if month_api > 200:
        alerts.append(f"API spend this month: ${month_api:.2f} (above $200 threshold)")

    return alerts


# ── Telegram Formatting ──────────────────────────────────────

def format_finances_text() -> str:
    """Telegram-formatted financial snapshot (HTML)."""
    summary = get_monthly_summary()
    data = _load()
    renewals = get_upcoming_renewals(7)
    alerts = get_alerts()

    lines = ["\U0001f4b0 <b>BROTHERHOOD FINANCES</b>\n"]

    # Monthly summary
    net = summary["net"]
    net_icon = "\U0001f7e2" if net >= 0 else "\U0001f534"
    lines.append(f"<b>{summary['month']}</b>")
    lines.append(f"Costs: <b>${summary['total_costs']:.2f}</b>")
    lines.append(f"  Subs: ${summary['total_subscriptions']:.2f}")
    lines.append(f"  API: ${summary['total_api']:.2f}")
    lines.append(f"  One-time: ${summary['total_one_time']:.2f}")
    lines.append(f"Revenue: <b>${summary['total_revenue']:.2f}</b>")
    lines.append(f"Net: {net_icon} <b>${net:.2f}</b>\n")

    # Active subscriptions
    active_subs = [s for s in data.get("subscriptions", []) if s.get("status") == "active"]
    if active_subs:
        lines.append(f"<b>Subscriptions ({len(active_subs)})</b>")
        for s in active_subs:
            cost_str = f"${s['cost']:.2f}" if s["cost"] > 0 else "FREE"
            lines.append(f"  {s['name']}: {cost_str}/{s.get('billing_cycle', 'mo')}")

    # Upcoming renewals
    if renewals:
        lines.append(f"\n\u23f0 <b>Renewals ({len(renewals)})</b>")
        for r in renewals:
            lines.append(f"  {r['name']}: ${r['cost']:.2f} in {r['days_until']}d")

    # Alerts
    if alerts:
        lines.append("\n\u26a0\ufe0f <b>Alerts</b>")
        for a in alerts:
            lines.append(f"  {a}")

    # By project
    if summary["by_project"]:
        lines.append("\n<b>By Project</b>")
        for proj, vals in sorted(summary["by_project"].items()):
            lines.append(f"  {proj}: -${vals['costs']:.2f} +${vals['revenue']:.2f} = ${vals['net']:.2f}")

    return "\n".join(lines)


# ── LLM Cost Ingestion ───────────────────────────────────────

def ingest_llm_costs(hours: int = 24) -> float:
    """Read llm_costs.jsonl, sum last N hours, return total."""
    if not LLM_COSTS_FILE.exists():
        return 0.0

    cutoff = time.time() - (hours * 3600)
    total = 0.0

    try:
        for line in LLM_COSTS_FILE.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", 0)
                if ts >= cutoff:
                    total += entry.get("cost_usd", 0)
            except (json.JSONDecodeError, TypeError):
                continue
    except Exception as e:
        log.warning("Failed to read LLM costs: %s", e)

    return round(total, 4)
