"""Pattern Miner — Extracts learned patterns from agent decision history.

Runs nightly (or on-demand). Reads all agent SQLite DBs, analyzes resolved
decisions, extracts statistical patterns, and writes them back as learned rules.

Usage:
    python3 ~/shared/pattern_miner.py          # Mine all agents
    python3 ~/shared/pattern_miner.py garves    # Mine specific agent
    python3 ~/shared/pattern_miner.py --stats   # Show memory stats only

Designed to run as Atlas background task or cron job.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Wire up shared
sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_memory import AgentMemory, MEMORY_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [MINER] %(message)s")
log = logging.getLogger(__name__)

MIN_EVIDENCE = 3       # Minimum resolved decisions to extract a pattern
MIN_CONFIDENCE = 0.55  # Only extract patterns with >55% confidence


def get_all_agents() -> list[str]:
    """Find all agent DBs."""
    if not MEMORY_DIR.exists():
        return []
    return sorted(p.stem for p in MEMORY_DIR.glob("*.db"))


def mine_agent(agent: str) -> dict:
    """Mine patterns from a single agent's decision history.

    Returns dict with stats and patterns found.
    """
    mem = AgentMemory(agent)
    stats = mem.get_stats()

    if stats["resolved_decisions"] < MIN_EVIDENCE:
        log.info("%s: only %d resolved decisions (need %d), skipping",
                 agent, stats["resolved_decisions"], MIN_EVIDENCE)
        return {"agent": agent, "skipped": True, "reason": "insufficient_data",
                "resolved": stats["resolved_decisions"]}

    resolved = mem.get_recent_decisions(limit=500, resolved_only=True)
    new_patterns = []

    # ── Strategy 1: Tag-based patterns ──
    # Group by tags and compute win rates
    tag_outcomes = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0})
    for dec in resolved:
        tags = json.loads(dec.get("tags", "[]"))
        score = dec.get("outcome_score", 0)
        for tag in tags:
            tag_outcomes[tag]["total"] += 1
            if score > 0:
                tag_outcomes[tag]["wins"] += 1
            elif score < 0:
                tag_outcomes[tag]["losses"] += 1

    for tag, counts in tag_outcomes.items():
        if counts["total"] < MIN_EVIDENCE:
            continue
        wr = counts["wins"] / max(1, counts["wins"] + counts["losses"])
        if wr >= MIN_CONFIDENCE or wr <= (1 - MIN_CONFIDENCE):
            result = "wins" if wr >= 0.5 else "loses"
            desc = f"Tag '{tag}': {result} {wr:.0%} of the time ({counts['wins']}W/{counts['losses']}L over {counts['total']} decisions)"
            pid = mem.add_pattern(
                pattern_type="tag_performance",
                description=desc,
                evidence_count=counts["total"],
                confidence=wr if wr >= 0.5 else (1 - wr),
            )
            new_patterns.append(desc)
            log.info("%s: %s", agent, desc)

    # ── Strategy 2: Keyword extraction from winning/losing decisions ──
    win_words = Counter()
    loss_words = Counter()
    for dec in resolved:
        words = _extract_keywords(dec.get("context", ""))
        score = dec.get("outcome_score", 0)
        if score > 0:
            win_words.update(words)
        elif score < 0:
            loss_words.update(words)

    # Find words strongly associated with wins or losses
    all_words = set(win_words.keys()) | set(loss_words.keys())
    for word in all_words:
        w = win_words.get(word, 0)
        l = loss_words.get(word, 0)
        total = w + l
        if total < MIN_EVIDENCE:
            continue
        wr = w / total
        if wr >= 0.65:  # Strong win signal
            desc = f"Keyword '{word}' in context: {wr:.0%} win rate ({w}W/{l}L)"
            mem.add_pattern("keyword_signal", desc, evidence_count=total, confidence=wr)
            new_patterns.append(desc)
            log.info("%s: %s", agent, desc)
        elif wr <= 0.35:  # Strong loss signal
            desc = f"Keyword '{word}' in context: {1-wr:.0%} loss rate ({l}L/{w}W)"
            mem.add_pattern("keyword_signal", desc, evidence_count=total, confidence=1 - wr)
            new_patterns.append(desc)
            log.info("%s: %s", agent, desc)

    # ── Strategy 3: Confidence calibration ──
    # Check if the agent's confidence predictions are well-calibrated
    high_conf = [d for d in resolved if d.get("confidence", 0) >= 0.7]
    low_conf = [d for d in resolved if d.get("confidence", 0) < 0.4]

    if len(high_conf) >= MIN_EVIDENCE:
        high_wr = sum(1 for d in high_conf if d.get("outcome_score", 0) > 0) / len(high_conf)
        desc = f"High-confidence decisions (>=0.7): actual win rate {high_wr:.0%} over {len(high_conf)} decisions"
        mem.add_pattern("calibration", desc, evidence_count=len(high_conf), confidence=high_wr)
        new_patterns.append(desc)

    if len(low_conf) >= MIN_EVIDENCE:
        low_wr = sum(1 for d in low_conf if d.get("outcome_score", 0) > 0) / len(low_conf)
        desc = f"Low-confidence decisions (<0.4): actual win rate {low_wr:.0%} over {len(low_conf)} decisions"
        mem.add_pattern("calibration", desc, evidence_count=len(low_conf), confidence=max(low_wr, 1 - low_wr))
        new_patterns.append(desc)

    # ── Strategy 4: Temporal patterns (time-of-day) ──
    hour_outcomes = defaultdict(lambda: {"wins": 0, "losses": 0})
    for dec in resolved:
        try:
            ts = dec.get("timestamp", "")
            hour = int(ts.split("T")[1].split(":")[0])
            score = dec.get("outcome_score", 0)
            if score > 0:
                hour_outcomes[hour]["wins"] += 1
            elif score < 0:
                hour_outcomes[hour]["losses"] += 1
        except (IndexError, ValueError):
            continue

    for hour, counts in hour_outcomes.items():
        total = counts["wins"] + counts["losses"]
        if total < MIN_EVIDENCE:
            continue
        wr = counts["wins"] / total
        if wr >= 0.7 or wr <= 0.3:
            period = "morning" if 6 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening" if 18 <= hour < 22 else "night"
            result = "favorable" if wr >= 0.5 else "unfavorable"
            desc = f"Hour {hour}:00 ({period}): {result} — {wr:.0%} WR ({counts['wins']}W/{counts['losses']}L)"
            mem.add_pattern("temporal", desc, evidence_count=total, confidence=wr if wr >= 0.5 else (1 - wr))
            new_patterns.append(desc)
            log.info("%s: %s", agent, desc)

    # ── Prune weak patterns ──
    all_patterns = mem.get_active_patterns()
    pruned = 0
    for p in all_patterns:
        if p["evidence_count"] <= 1 and p["confidence"] < 0.5:
            mem.deactivate_pattern(p["id"])
            pruned += 1

    mem.close()
    return {
        "agent": agent,
        "skipped": False,
        "resolved_decisions": stats["resolved_decisions"],
        "patterns_extracted": len(new_patterns),
        "patterns_pruned": pruned,
        "total_active_patterns": stats["active_patterns"] + len(new_patterns) - pruned,
        "new_patterns": new_patterns,
    }


def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from context text."""
    text = text.lower()
    # Remove common noise words
    noise = {"the", "and", "for", "with", "from", "this", "that", "was", "are",
             "has", "had", "but", "not", "you", "all", "can", "her", "his",
             "one", "our", "out", "day", "get", "got", "let", "may", "say",
             "she", "too", "use", "way", "who", "how", "its", "did", "now"}
    words = re.findall(r'[a-z][a-z0-9_]+', text)
    return [w for w in words if w not in noise and len(w) >= 3]


def print_stats() -> None:
    """Print memory stats for all agents."""
    agents = get_all_agents()
    if not agents:
        print("No agent memory databases found.")
        return

    print(f"\n{'Agent':12s} {'Decisions':>10s} {'Resolved':>10s} {'Patterns':>10s} {'WR':>8s} {'DB Size':>10s}")
    print("-" * 65)
    for agent in agents:
        mem = AgentMemory(agent)
        s = mem.get_stats()
        wr = f"{s['win_rate']:.1f}%" if s['win_count'] + s['loss_count'] > 0 else "N/A"
        print(f"{agent:12s} {s['total_decisions']:>10d} {s['resolved_decisions']:>10d} "
              f"{s['active_patterns']:>10d} {wr:>8s} {s['db_size_kb']:>8.1f}KB")
        mem.close()
    print()


def mine_all() -> list[dict]:
    """Mine patterns for all agents."""
    agents = get_all_agents()
    if not agents:
        log.info("No agent DBs found in %s", MEMORY_DIR)
        return []

    log.info("Mining patterns for %d agents: %s", len(agents), ", ".join(agents))
    results = []
    for agent in agents:
        try:
            result = mine_agent(agent)
            results.append(result)
        except Exception as e:
            log.error("Failed to mine %s: %s", agent, e)
            results.append({"agent": agent, "skipped": True, "reason": str(e)})

    # Summary
    total_patterns = sum(r.get("patterns_extracted", 0) for r in results)
    mined = [r["agent"] for r in results if not r.get("skipped")]
    skipped = [r["agent"] for r in results if r.get("skipped")]

    log.info("Mining complete: %d agents mined, %d skipped, %d new patterns",
             len(mined), len(skipped), total_patterns)
    return results


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--stats" in args:
        print_stats()
    elif args and args[0] != "--stats":
        # Mine specific agent
        result = mine_agent(args[0])
        print(json.dumps(result, indent=2))
    else:
        # Mine all
        results = mine_all()
        print_stats()
