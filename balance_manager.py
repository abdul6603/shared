"""Shared Balance Manager — cross-agent wallet coordination for Polymarket.

4 agents (Garves, Maker, Hawk, Oracle) share ONE wallet.
This module prevents overcommitting USDC via weight-based allocation.

SQLite WAL mode for safe concurrent access from multiple processes.
Fail-open design: if this module errors, agents fall through to their own limits.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path.home() / "shared" / "balance_manager.db"

# Defaults (env-configurable per agent)
DEFAULT_WEIGHTS = {
    "garves": 5,
    "hawk": 3,
    "oracle": 2,
    "maker": 1,
}
CASH_RESERVE_PCT = float(os.environ.get("BALANCE_CASH_RESERVE_PCT", "0.20"))
STALE_TIMEOUT_S = int(os.environ.get("BALANCE_STALE_TIMEOUT_S", "600"))


def _get_conn() -> sqlite3.Connection:
    """Open a WAL-mode connection with busy timeout."""
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash REAL NOT NULL DEFAULT 0,
            positions_value REAL NOT NULL DEFAULT 0,
            portfolio_total REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            name TEXT PRIMARY KEY,
            weight REAL NOT NULL DEFAULT 1,
            current_exposure REAL NOT NULL DEFAULT 0,
            last_heartbeat REAL NOT NULL DEFAULT 0
        )
    """)
    # Seed wallet_state if empty
    conn.execute("""
        INSERT OR IGNORE INTO wallet_state (id, cash, positions_value, portfolio_total, updated_at)
        VALUES (1, 0, 0, 0, 0)
    """)
    conn.commit()


class BalanceManager:
    """Cross-agent balance coordinator.

    Each agent creates an instance with its name, registers its weight,
    and calls can_trade() before placing orders.
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self._conn = _get_conn()
        _ensure_tables(self._conn)
        log.info("[BALANCE] Shared balance manager initialized for %s", agent_name)

    def register(self, weight: float) -> None:
        """Register or update agent with allocation weight (idempotent)."""
        self._conn.execute("""
            INSERT INTO agents (name, weight, current_exposure, last_heartbeat)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(name) DO UPDATE SET weight = excluded.weight
        """, (self.agent_name, weight, time.time()))
        self._conn.commit()

    def update_wallet(self, cash: float, positions_value: float) -> None:
        """Update shared wallet state. Called by Garves every 2 min."""
        now = time.time()
        portfolio = cash + positions_value
        self._conn.execute("""
            UPDATE wallet_state
            SET cash = ?, positions_value = ?, portfolio_total = ?, updated_at = ?
            WHERE id = 1
        """, (cash, positions_value, portfolio, now))
        self._conn.commit()

    def report_exposure(self, exposure_usd: float) -> None:
        """Report current exposure (also serves as heartbeat)."""
        now = time.time()
        self._conn.execute("""
            UPDATE agents SET current_exposure = ?, last_heartbeat = ?
            WHERE name = ?
        """, (exposure_usd, now, self.agent_name))
        self._conn.commit()

    def can_trade(self, amount: float) -> tuple[bool, str]:
        """THE GATE: check if agent can place a trade of given USD amount.

        Returns (allowed, reason).
        """
        now = time.time()

        # Clean up stale agents first
        self._cleanup_stale(now)

        # Get wallet state
        row = self._conn.execute(
            "SELECT cash, positions_value, portfolio_total, updated_at FROM wallet_state WHERE id = 1"
        ).fetchone()
        if not row or row["updated_at"] == 0:
            return True, "no wallet data yet — passthrough"

        wallet_age = now - row["updated_at"]
        if wallet_age > 300:
            log.warning("[BALANCE] Wallet state is %.0fs old (>5min) — data may be stale", wallet_age)

        cash = row["cash"]
        portfolio = row["portfolio_total"]

        if portfolio <= 0:
            return True, "portfolio is zero — passthrough"

        # Deployable = portfolio * (1 - reserve)
        deployable = portfolio * (1.0 - CASH_RESERVE_PCT)

        # Get all agents and their weights
        agents = self._conn.execute(
            "SELECT name, weight, current_exposure FROM agents"
        ).fetchall()

        total_weight = sum(a["weight"] for a in agents)
        if total_weight <= 0:
            return True, "no agents registered — passthrough"

        # Find my weight and exposure
        my_weight = 0.0
        my_exposure = 0.0
        for a in agents:
            if a["name"] == self.agent_name:
                my_weight = a["weight"]
                my_exposure = a["current_exposure"]
                break

        if my_weight <= 0:
            return True, "agent not registered — passthrough"

        # My allocation
        my_allocation = deployable * (my_weight / total_weight)

        # Check 1: Would exceed allocation?
        if my_exposure + amount > my_allocation:
            return False, (
                f"allocation exceeded: ${my_exposure:.2f} + ${amount:.2f} = "
                f"${my_exposure + amount:.2f} > ${my_allocation:.2f} "
                f"(weight {my_weight}/{total_weight}, deployable ${deployable:.2f})"
            )

        # Check 2: Enough cash to actually pay?
        if cash < amount:
            return False, f"insufficient cash: ${cash:.2f} < ${amount:.2f} needed"

        return True, "ok"

    def get_portfolio_summary(self) -> dict:
        """Return full portfolio summary for dashboard."""
        now = time.time()
        self._cleanup_stale(now)

        row = self._conn.execute(
            "SELECT cash, positions_value, portfolio_total, updated_at FROM wallet_state WHERE id = 1"
        ).fetchone()

        wallet = {
            "cash": row["cash"] if row else 0,
            "positions_value": row["positions_value"] if row else 0,
            "portfolio_total": row["portfolio_total"] if row else 0,
            "updated_at": row["updated_at"] if row else 0,
            "age_s": round(now - row["updated_at"], 1) if row and row["updated_at"] else 0,
        }

        agents = self._conn.execute(
            "SELECT name, weight, current_exposure, last_heartbeat FROM agents"
        ).fetchall()

        total_weight = sum(a["weight"] for a in agents)
        portfolio = wallet["portfolio_total"]
        deployable = portfolio * (1.0 - CASH_RESERVE_PCT) if portfolio > 0 else 0

        agent_list = []
        total_exposure = 0.0
        for a in agents:
            allocation = deployable * (a["weight"] / total_weight) if total_weight > 0 else 0
            heartbeat_age = round(now - a["last_heartbeat"], 1) if a["last_heartbeat"] else 0
            agent_list.append({
                "name": a["name"],
                "weight": a["weight"],
                "allocation": round(allocation, 2),
                "exposure": round(a["current_exposure"], 2),
                "utilization_pct": round(
                    (a["current_exposure"] / allocation * 100) if allocation > 0 else 0, 1
                ),
                "heartbeat_age_s": heartbeat_age,
                "alive": heartbeat_age < STALE_TIMEOUT_S if a["last_heartbeat"] else False,
            })
            total_exposure += a["current_exposure"]

        return {
            "wallet": wallet,
            "agents": agent_list,
            "deployable": round(deployable, 2),
            "reserve": round(portfolio * CASH_RESERVE_PCT, 2) if portfolio > 0 else 0,
            "reserve_pct": CASH_RESERVE_PCT,
            "total_weight": total_weight,
            "total_exposure": round(total_exposure, 2),
            "total_utilization_pct": round(
                (total_exposure / deployable * 100) if deployable > 0 else 0, 1
            ),
            "timestamp": now,
        }

    def _cleanup_stale(self, now: float) -> None:
        """Zero exposure for agents that haven't sent a heartbeat recently."""
        self._conn.execute("""
            UPDATE agents SET current_exposure = 0
            WHERE last_heartbeat > 0 AND (? - last_heartbeat) > ?
        """, (now, STALE_TIMEOUT_S))
        self._conn.commit()


def can_trade_safe(bm: BalanceManager | None, amount: float) -> tuple[bool, str]:
    """Fail-open wrapper: if balance manager errors or is None, allow trade."""
    if bm is None:
        return True, "no balance manager"
    try:
        return bm.can_trade(amount)
    except Exception as e:
        log.debug("[BALANCE] can_trade_safe fallthrough: %s", str(e)[:100])
        return True, "fallthrough"
