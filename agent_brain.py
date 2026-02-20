"""Shared Agent Brain — LLM + Memory Combined.

Wraps llm_client + agent_memory into one interface. Before answering,
the brain automatically:
1. Searches memory for similar past situations
2. Pulls learned patterns
3. Injects this context into the LLM prompt
4. The model sees its own history and makes better decisions

Usage:
    from shared.agent_brain import AgentBrain

    brain = AgentBrain("hawk", system_prompt="You are Hawk...")
    result = brain.think("BTC high vol, F&G=25", "Should I take this trade?")
    did = brain.remember_decision("BTC high vol", result, confidence=0.7)
    brain.remember_outcome(did, "Won +$8.50", score=0.85)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from shared.llm_client import llm_call
from shared.agent_memory import AgentMemory

log = logging.getLogger(__name__)


@dataclass
class ThinkResult:
    content: str
    memory_context: str  # What memory was injected
    patterns_used: int   # How many patterns were referenced
    decisions_found: int # How many similar past decisions found


class AgentBrain:
    """Combined LLM + Memory interface for agent intelligence."""

    def __init__(
        self,
        agent: str,
        system_prompt: str = "",
        task_type: str = "reasoning",
        max_context_decisions: int = 5,
        max_context_patterns: int = 8,
    ):
        self.agent = agent.lower()
        self.system_prompt = system_prompt
        self.task_type = task_type
        self.max_context_decisions = max_context_decisions
        self.max_context_patterns = max_context_patterns
        self.memory = AgentMemory(agent)

    def think(
        self,
        situation: str,
        question: str,
        task_type: str | None = None,
        max_tokens: int = 500,
        temperature: float = 0.3,
        include_memory: bool = True,
    ) -> ThinkResult:
        """Think about a situation with memory context.

        Args:
            situation: Description of current situation/context.
            question: What to answer or decide.
            task_type: Override task_type for routing.
            max_tokens: Max tokens to generate.
            temperature: Sampling temperature.
            include_memory: Whether to inject memory context.

        Returns:
            ThinkResult with the response and memory metadata.
        """
        memory_context = ""
        patterns_used = 0
        decisions_found = 0

        if include_memory:
            memory_context, patterns_used, decisions_found = self._build_memory_context(situation)

        # Build enriched system prompt
        enriched_system = self.system_prompt
        if memory_context:
            enriched_system += (
                "\n\n--- YOUR MEMORY (learned from past experience) ---\n"
                + memory_context
                + "\n--- END MEMORY ---\n"
                "\nUse this memory to inform your decision, but don't blindly follow patterns "
                "if the current situation is significantly different."
            )

        # Build user message
        user_msg = f"SITUATION: {situation}\n\n{question}"

        response = llm_call(
            system=enriched_system,
            user=user_msg,
            agent=self.agent,
            task_type=task_type or self.task_type,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        return ThinkResult(
            content=response,
            memory_context=memory_context,
            patterns_used=patterns_used,
            decisions_found=decisions_found,
        )

    def _build_memory_context(self, situation: str) -> tuple[str, int, int]:
        """Build memory context string from relevant decisions + patterns.

        Returns (context_str, patterns_count, decisions_count).
        """
        parts = []

        # 1. Active patterns (highest confidence first)
        patterns = self.memory.get_active_patterns(min_confidence=0.4)
        patterns = patterns[:self.max_context_patterns]
        if patterns:
            parts.append("LEARNED PATTERNS:")
            for p in patterns:
                conf_pct = int(p["confidence"] * 100)
                parts.append(
                    f"  [{conf_pct}% confidence, {p['evidence_count']} evidence] "
                    f"{p['description']}"
                )

        # 2. Similar past decisions
        decisions = self.memory.get_relevant_context(situation, limit=self.max_context_decisions)
        if decisions:
            parts.append("\nSIMILAR PAST DECISIONS:")
            for d in decisions:
                outcome = ""
                if d.get("resolved"):
                    score = d.get("outcome_score", 0)
                    emoji = "WIN" if score > 0 else "LOSS" if score < 0 else "NEUTRAL"
                    outcome = f" → {emoji}: {d.get('outcome', '')[:100]}"
                parts.append(
                    f"  Context: {d['context'][:150]}\n"
                    f"  Decision: {d['decision'][:150]}\n"
                    f"  Confidence: {d.get('confidence', 0):.0%}{outcome}"
                )

        context = "\n".join(parts)
        return context, len(patterns), len(decisions)

    # ── Memory shortcuts ──

    def remember_decision(
        self,
        context: str,
        decision: str,
        reasoning: str = "",
        confidence: float = 0.5,
        tags: list[str] | None = None,
    ) -> str:
        """Record a decision. Returns decision ID."""
        return self.memory.record_decision(context, decision, reasoning, confidence, tags)

    def remember_outcome(self, decision_id: str, outcome: str, score: float = 0.0) -> bool:
        """Record the outcome of a past decision."""
        return self.memory.record_outcome(decision_id, outcome, score)

    def learn_pattern(
        self,
        pattern_type: str,
        description: str,
        evidence_count: int = 1,
        confidence: float = 0.5,
    ) -> str:
        """Store or reinforce a learned rule."""
        return self.memory.add_pattern(pattern_type, description, evidence_count, confidence)

    def remember_fact(
        self,
        category: str,
        key: str,
        value: str,
        source: str = "",
        ttl_hours: int = 0,
    ) -> str:
        """Store a fact in the knowledge base."""
        return self.memory.set_knowledge(category, key, value, source, ttl_hours)

    def get_patterns(self, min_confidence: float = 0.4) -> list[dict]:
        """Get active learned patterns."""
        return self.memory.get_active_patterns(min_confidence=min_confidence)

    def get_stats(self) -> dict:
        """Memory stats for dashboard."""
        return self.memory.get_stats()

    def close(self) -> None:
        """Clean up."""
        self.memory.close()
