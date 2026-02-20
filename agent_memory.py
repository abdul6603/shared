"""Shared Agent Memory — Per-Agent SQLite Learning Database.

Each agent gets a SQLite DB at ~/shared/memory/{agent}.db with three tables:
- decisions: Every choice the agent makes (with outcome tracking)
- patterns: Learned rules extracted from decisions
- knowledge: Agent-specific facts with TTL

Usage:
    from shared.agent_memory import AgentMemory

    mem = AgentMemory("hawk")
    did = mem.record_decision("BTC high vol", "Take YES position", "F&G=25, vol spike", 0.7)
    mem.record_outcome(did, "Won +$8.50", 0.85)
    context = mem.get_relevant_context("BTC high volatility trade")
    mem.add_pattern("trend", "BTC DOWN in fear markets: 78% WR", evidence_count=12)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).resolve().parent / "memory"


class AgentMemory:
    """Per-agent SQLite learning database."""

    def __init__(self, agent: str):
        self.agent = agent.lower()
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = MEMORY_DIR / f"{self.agent}.db"
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                context TEXT NOT NULL,
                decision TEXT NOT NULL,
                reasoning TEXT DEFAULT '',
                confidence REAL DEFAULT 0.5,
                outcome TEXT DEFAULT '',
                outcome_score REAL DEFAULT 0.0,
                resolved INTEGER DEFAULT 0,
                tags TEXT DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS patterns (
                id TEXT PRIMARY KEY,
                pattern_type TEXT NOT NULL,
                description TEXT NOT NULL,
                evidence_count INTEGER DEFAULT 1,
                confidence REAL DEFAULT 0.5,
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                tags TEXT DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS knowledge (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT DEFAULT '',
                ttl_hours INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(timestamp);
            CREATE INDEX IF NOT EXISTS idx_decisions_resolved ON decisions(resolved);
            CREATE INDEX IF NOT EXISTS idx_patterns_type ON patterns(pattern_type);
            CREATE INDEX IF NOT EXISTS idx_patterns_active ON patterns(active);
            CREATE INDEX IF NOT EXISTS idx_knowledge_cat ON knowledge(category);
            CREATE INDEX IF NOT EXISTS idx_knowledge_key ON knowledge(key);
        """)
        conn.commit()

    # ── Decisions ──

    def record_decision(
        self,
        context: str,
        decision: str,
        reasoning: str = "",
        confidence: float = 0.5,
        tags: list[str] | None = None,
    ) -> str:
        """Log a decision the agent made. Returns decision ID."""
        did = f"dec_{uuid.uuid4().hex[:10]}"
        now = datetime.now(ET).isoformat()
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO decisions (id, timestamp, context, decision, reasoning, confidence, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (did, now, context[:2000], decision[:2000], reasoning[:2000],
             max(0.0, min(1.0, confidence)), json.dumps(tags or [])),
        )
        conn.commit()
        return did

    def record_outcome(self, decision_id: str, outcome: str, score: float = 0.0) -> bool:
        """Record the outcome of a past decision. Returns True if found."""
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE decisions SET outcome = ?, outcome_score = ?, resolved = 1 WHERE id = ?",
            (outcome[:2000], max(-1.0, min(1.0, score)), decision_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def get_recent_decisions(self, limit: int = 20, resolved_only: bool = False) -> list[dict]:
        """Get recent decisions, newest first."""
        conn = self._get_conn()
        where = "WHERE resolved = 1" if resolved_only else ""
        rows = conn.execute(
            f"SELECT * FROM decisions {where} ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_relevant_context(self, situation: str, limit: int = 5) -> list[dict]:
        """Find past decisions relevant to the current situation.

        Uses keyword matching on context field. Returns most recent matches.
        """
        # Extract keywords (3+ char words)
        words = [w.lower() for w in situation.split() if len(w) >= 3]
        if not words:
            return []

        conn = self._get_conn()
        # Build OR query for keyword matching
        conditions = " OR ".join(["LOWER(context) LIKE ?" for _ in words])
        params = [f"%{w}%" for w in words[:10]]  # Cap at 10 keywords

        rows = conn.execute(
            f"SELECT *, "
            f"(SELECT COUNT(*) FROM (SELECT 1 WHERE {conditions})) as relevance "
            f"FROM decisions WHERE {conditions} "
            f"ORDER BY timestamp DESC LIMIT ?",
            params + params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]

    def search_decisions(self, query: str, limit: int = 10) -> list[dict]:
        """Search decisions by context or decision text."""
        conn = self._get_conn()
        pattern = f"%{query}%"
        rows = conn.execute(
            "SELECT * FROM decisions WHERE context LIKE ? OR decision LIKE ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Patterns ──

    def add_pattern(
        self,
        pattern_type: str,
        description: str,
        evidence_count: int = 1,
        confidence: float = 0.5,
        tags: list[str] | None = None,
    ) -> str:
        """Store a learned rule. Reinforces if same pattern_type + description exists."""
        conn = self._get_conn()
        now = datetime.now(ET).isoformat()

        # Check for existing similar pattern
        existing = conn.execute(
            "SELECT id, evidence_count, confidence FROM patterns "
            "WHERE pattern_type = ? AND description = ? AND active = 1",
            (pattern_type, description),
        ).fetchone()

        if existing:
            # Reinforce: bump evidence count + confidence
            new_count = existing["evidence_count"] + evidence_count
            new_conf = min(0.99, existing["confidence"] + 0.05)
            conn.execute(
                "UPDATE patterns SET evidence_count = ?, confidence = ?, updated_at = ? WHERE id = ?",
                (new_count, new_conf, now, existing["id"]),
            )
            conn.commit()
            return existing["id"]

        pid = f"pat_{uuid.uuid4().hex[:10]}"
        conn.execute(
            "INSERT INTO patterns (id, pattern_type, description, evidence_count, confidence, "
            "created_at, updated_at, tags) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, pattern_type[:100], description[:1000], evidence_count,
             max(0.0, min(1.0, confidence)), now, now, json.dumps(tags or [])),
        )
        conn.commit()
        return pid

    def get_active_patterns(self, pattern_type: str | None = None, min_confidence: float = 0.0) -> list[dict]:
        """Get active learned patterns."""
        conn = self._get_conn()
        where = "WHERE active = 1"
        params: list = []
        if pattern_type:
            where += " AND pattern_type = ?"
            params.append(pattern_type)
        if min_confidence > 0:
            where += " AND confidence >= ?"
            params.append(min_confidence)
        rows = conn.execute(
            f"SELECT * FROM patterns {where} ORDER BY confidence DESC, evidence_count DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def deactivate_pattern(self, pattern_id: str) -> bool:
        """Deactivate a pattern that turned out to be wrong."""
        conn = self._get_conn()
        cur = conn.execute("UPDATE patterns SET active = 0 WHERE id = ?", (pattern_id,))
        conn.commit()
        return cur.rowcount > 0

    # ── Knowledge ──

    def set_knowledge(
        self,
        category: str,
        key: str,
        value: str,
        source: str = "",
        ttl_hours: int = 0,
    ) -> str:
        """Store an agent-specific fact. Upserts by category+key."""
        conn = self._get_conn()
        now = datetime.now(ET)
        expires = ""
        if ttl_hours > 0:
            expires = (now + timedelta(hours=ttl_hours)).isoformat()

        # Upsert
        existing = conn.execute(
            "SELECT id FROM knowledge WHERE category = ? AND key = ?",
            (category, key),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE knowledge SET value = ?, source = ?, ttl_hours = ?, "
                "created_at = ?, expires_at = ? WHERE id = ?",
                (value[:5000], source[:200], ttl_hours, now.isoformat(), expires, existing["id"]),
            )
            conn.commit()
            return existing["id"]

        kid = f"kn_{uuid.uuid4().hex[:10]}"
        conn.execute(
            "INSERT INTO knowledge (id, category, key, value, source, ttl_hours, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kid, category[:100], key[:200], value[:5000], source[:200],
             ttl_hours, now.isoformat(), expires),
        )
        conn.commit()
        return kid

    def get_knowledge(self, category: str | None = None, key: str | None = None) -> list[dict]:
        """Get knowledge entries, auto-pruning expired ones."""
        conn = self._get_conn()
        now = datetime.now(ET).isoformat()

        # Prune expired
        conn.execute(
            "DELETE FROM knowledge WHERE expires_at != '' AND expires_at < ?",
            (now,),
        )
        conn.commit()

        where = "WHERE 1=1"
        params: list = []
        if category:
            where += " AND category = ?"
            params.append(category)
        if key:
            where += " AND key = ?"
            params.append(key)

        rows = conn.execute(
            f"SELECT * FROM knowledge {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Stats ──

    def get_stats(self) -> dict:
        """Memory health metrics for dashboard."""
        conn = self._get_conn()

        total_decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        resolved_decisions = conn.execute("SELECT COUNT(*) FROM decisions WHERE resolved = 1").fetchone()[0]
        active_patterns = conn.execute("SELECT COUNT(*) FROM patterns WHERE active = 1").fetchone()[0]
        total_knowledge = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]

        # Win rate (for agents that track +/- outcomes)
        wins = conn.execute("SELECT COUNT(*) FROM decisions WHERE resolved = 1 AND outcome_score > 0").fetchone()[0]
        losses = conn.execute("SELECT COUNT(*) FROM decisions WHERE resolved = 1 AND outcome_score < 0").fetchone()[0]

        # Avg confidence of resolved decisions
        avg_conf = conn.execute(
            "SELECT AVG(confidence) FROM decisions WHERE resolved = 1"
        ).fetchone()[0]

        # Recent pattern count (last 7 days)
        week_ago = (datetime.now(ET) - timedelta(days=7)).isoformat()
        recent_patterns = conn.execute(
            "SELECT COUNT(*) FROM patterns WHERE created_at > ?", (week_ago,)
        ).fetchone()[0]

        return {
            "agent": self.agent,
            "total_decisions": total_decisions,
            "resolved_decisions": resolved_decisions,
            "unresolved_decisions": total_decisions - resolved_decisions,
            "active_patterns": active_patterns,
            "total_knowledge": total_knowledge,
            "win_count": wins,
            "loss_count": losses,
            "win_rate": round(wins / max(1, wins + losses) * 100, 1),
            "avg_confidence": round(avg_conf or 0, 3),
            "recent_patterns_7d": recent_patterns,
            "db_size_kb": round(self.db_path.stat().st_size / 1024, 1) if self.db_path.exists() else 0,
        }

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
