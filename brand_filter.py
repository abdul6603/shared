"""Brand Filter — GPT-4o powered brand assessment engine.

Evaluates opportunities against Soren's full brand identity:
  CHARACTER, CONTENT_PILLARS, SEO_KEYWORDS, CONTENT_RULES, values.

Falls back to rule-based keyword matching if GPT-4o call fails.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# ── Load OpenAI API key from Soren's .env ──
_SOREN_ENV = Path.home() / "soren-content" / ".env"
_OPENAI_KEY: str | None = None


def _get_openai_key() -> str | None:
    global _OPENAI_KEY
    if _OPENAI_KEY:
        return _OPENAI_KEY
    # Check env var first
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        _OPENAI_KEY = key
        return key
    # Parse from Soren's .env
    if _SOREN_ENV.exists():
        for line in _SOREN_ENV.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                _OPENAI_KEY = line.split("=", 1)[1].strip()
                return _OPENAI_KEY
    return None


# ── Soren Brand Context (embedded for speed — no import from soren-content) ──

SOREN_BRAND_CONTEXT = """
# SOREN — Brand Identity

## Archetype
Lone Wolf / Dark Warrior

## Backstory
Had his heart shattered years ago. Instead of breaking, he rebuilt himself into something unrecognizable. Now he's a machine — cold discipline, relentless progress, zero excuses. He doesn't talk about the pain. He channels it. Every rep, every deal, every 4AM alarm is revenge on who he used to be.

## Voice
Raw. Intense. Poetic. No fluff. Short sharp sentences that hit like a gut punch. Channels pain into content. Every word is deliberate.

## Values (7 core)
1. Discipline over motivation
2. Silence over explanation
3. Progress over perfection
4. Solitude over toxic company
5. Actions over words
6. Pain as fuel
7. Legacy over comfort

## Traits
- Lone wolf — moves alone, trusts few
- Narcissist in the best way — obsessed with his own evolution
- Emotionally bulletproof — the heartbreak forged armor
- Disciplined to a fault — gym, diet, work, repeat
- 1% mentality — sees the world differently than the 99%
- Warrior spirit — treats every day like a battlefield
- Silent confidence — doesn't announce, just arrives
- Dark aesthetic — moody, cinematic, intense

## Content Pillars (10)
1. dark_motivation — Cinematic edits with voiceover about discipline, pain, growth (daily)
2. gym_warrior — Raw gym footage, heavy lifts, intensity (3x/week)
3. lone_wolf_lifestyle — Walking alone at night, empty roads, solo meals (2x/week)
4. wisdom_quotes — Text-on-dark-background quotes about pain and growth (daily)
5. mindset_monologue — Face-to-camera talks about 1% mentality (2x/week)
6. progress_showcase — Before/after, physique updates, silent flex (1x/week)
7. heartbreak_to_power — Raw vulnerability turned fuel, origin story (2x/week)
8. stoic_lessons — Marcus Aurelius, Epictetus, Seneca for the modern warrior (3x/week)
9. dark_humor — Dry dark wit about relationships, society, mediocrity (2x/week)
10. night_rituals — Evening routine: reading, journaling, planning (1x/week)

## SEO Keywords
dark motivation, lone wolf mindset, lone wolf quotes, stoic philosophy, stoic quotes, stoicism, discipline over motivation, self improvement for men, sigma mindset, sigma grindset, heartbreak recovery, glow up, gym motivation, fitness discipline, silent strength, quiet confidence, nobody is coming to save you, dark motivation quotes, stoic quotes for men, how to be disciplined

## Content Rules (non-negotiable)
- Never post with another platform's watermark
- Never use copyrighted film clips — original footage only
- Hook in first 0.5 seconds
- Never ignore first 60 minutes — reply to every comment
- Never post same pillar twice in a row
- Design for saves and shares, not likes
- Speak keywords in voiceovers
- Always end with a CTA
- Always loop videos
"""

PILLAR_NAMES = [
    "dark_motivation", "gym_warrior", "lone_wolf_lifestyle", "wisdom_quotes",
    "mindset_monologue", "progress_showcase", "heartbreak_to_power",
    "stoic_lessons", "dark_humor", "night_rituals",
]

SOREN_VALUES = [
    "Discipline over motivation", "Silence over explanation",
    "Progress over perfection", "Solitude over toxic company",
    "Actions over words", "Pain as fuel", "Legacy over comfort",
]

# ── Rule-based fallback keywords (from viper/soren_scout.py) ──
_BRAND_KEYWORDS = {
    "core": ["motivation", "discipline", "stoic", "warrior", "grind", "lone wolf",
             "dark", "mindset", "self improvement", "mental strength", "resilience",
             "hustle", "focus", "dark motivation", "sigma", "masculinity"],
    "fitness": ["gym", "workout", "bodybuilding", "fitness", "supplement", "protein",
                "creatine", "pre-workout", "athletic", "training", "lifting"],
    "mindset": ["meditation", "journaling", "habits", "productivity", "stoicism",
                "marcus aurelius", "philosophy", "morning routine", "cold shower",
                "dopamine", "self discipline"],
}


def assess_brand_fit(opportunity: dict) -> dict:
    """Assess an opportunity's brand fit using GPT-4o, with rule-based fallback.

    Args:
        opportunity: dict with at least 'title', 'description', optionally 'type', 'category', 'url'

    Returns:
        dict with brand_fit_score, pillar_match, archetype_alignment,
        value_alignment, content_suggestion, risk_flags, auto_verdict, reasoning
    """
    key = _get_openai_key()
    if key:
        try:
            return _gpt4o_assess(opportunity, key)
        except Exception:
            log.exception("GPT-4o brand assessment failed, falling back to rule-based")

    return _rule_based_assess(opportunity)


def _gpt4o_assess(opportunity: dict, api_key: str) -> dict:
    """Call GPT-4o for deep semantic brand assessment."""
    import requests

    opp_text = json.dumps({
        "title": opportunity.get("title", ""),
        "description": opportunity.get("description", ""),
        "type": opportunity.get("type", ""),
        "category": opportunity.get("category", ""),
        "url": opportunity.get("url", ""),
        "estimated_value": opportunity.get("estimated_value", ""),
    }, indent=2)

    prompt = f"""You are a brand strategist for Soren, a dark motivation content creator.

{SOREN_BRAND_CONTEXT}

## Opportunity to Assess
{opp_text}

## Task
Evaluate this opportunity's fit with Soren's brand identity. Return a JSON object with these exact keys:

1. "brand_fit_score": integer 0-100 (how well does this match Soren's brand DNA?)
2. "pillar_match": string — best matching content pillar from the 10 listed (use snake_case name, or "none" if no match)
3. "archetype_alignment": string — "Strong", "Moderate", "Weak", or "Misaligned"
4. "value_alignment": list of strings — which of Soren's 7 values this maps to (empty list if none)
5. "content_suggestion": string — concrete content idea if Soren were to act on this (format, caption direction, which pillar)
6. "risk_flags": list of strings — any brand misalignment risks, copyright issues, reputation risks (empty list if clean)
7. "auto_verdict": string — "auto_approved" if score >= 70, "needs_review" if 40-69, "auto_rejected" if < 40
8. "reasoning": string — 1-2 sentence explanation of the score

Return ONLY the JSON object, no markdown fencing, no extra text."""

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 500,
        },
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown fencing if present
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    result = json.loads(content)

    # Enforce verdict tiers
    score = result.get("brand_fit_score", 0)
    if score >= 70:
        result["auto_verdict"] = "auto_approved"
    elif score >= 40:
        result["auto_verdict"] = "needs_review"
    else:
        result["auto_verdict"] = "auto_rejected"

    return result


def _rule_based_assess(opportunity: dict) -> dict:
    """Fallback: rule-based keyword matching (same logic as soren_scout._score_brand_fit)."""
    text = f"{opportunity.get('title', '')} {opportunity.get('description', '')}".lower()

    # Niche keyword scoring
    niche_hits = 0
    niche_total = 0
    for category, keywords in _BRAND_KEYWORDS.items():
        weight = 3 if category == "core" else 2
        for kw in keywords:
            niche_total += weight
            if kw in text:
                niche_hits += weight
    niche_score = (niche_hits / max(niche_total, 1)) * 100

    # Opportunity relevance
    opp_keywords = [
        ("brand deal", 15), ("sponsorship", 15), ("affiliate", 15), ("ambassador", 15),
        ("partnership", 12), ("commission", 12), ("creator program", 15),
        ("influencer", 10), ("ugc", 12), ("monetiz", 10),
        ("trending", 8), ("viral", 8),
    ]
    opp_score = min(100, sum(pts for kw, pts in opp_keywords if kw in text))

    raw = niche_score * 0.4 + opp_score * 0.6
    if any(phrase in text for phrase in ["dark motivation", "lone wolf", "sigma", "stoic mindset"]):
        raw = min(100, raw + 25)

    score = min(100, max(0, int(raw)))

    # Determine pillar match
    pillar_keywords = {
        "dark_motivation": ["motivation", "dark", "discipline", "pain", "growth"],
        "gym_warrior": ["gym", "workout", "fitness", "lifting", "supplement"],
        "lone_wolf_lifestyle": ["lone wolf", "solo", "alone", "sigma"],
        "wisdom_quotes": ["quotes", "wisdom", "philosophy"],
        "stoic_lessons": ["stoic", "marcus aurelius", "stoicism", "epictetus"],
        "heartbreak_to_power": ["heartbreak", "breakup", "rebuild"],
        "mindset_monologue": ["mindset", "mentality", "focus"],
        "dark_humor": ["humor", "meme", "satire"],
        "night_rituals": ["routine", "journal", "evening"],
        "progress_showcase": ["transformation", "before after", "glow up"],
    }
    best_pillar = "none"
    best_pillar_score = 0
    for pillar, kws in pillar_keywords.items():
        hits = sum(1 for kw in kws if kw in text)
        if hits > best_pillar_score:
            best_pillar_score = hits
            best_pillar = pillar

    # Value alignment
    matched_values = [v for v in SOREN_VALUES if any(w in text for w in v.lower().split())]

    # Archetype alignment
    if score >= 70:
        alignment = "Strong"
    elif score >= 40:
        alignment = "Moderate"
    elif score >= 20:
        alignment = "Weak"
    else:
        alignment = "Misaligned"

    # Auto verdict
    if score >= 70:
        verdict = "auto_approved"
    elif score >= 40:
        verdict = "needs_review"
    else:
        verdict = "auto_rejected"

    return {
        "brand_fit_score": score,
        "pillar_match": best_pillar,
        "archetype_alignment": alignment,
        "value_alignment": matched_values[:3],
        "content_suggestion": f"Create a {best_pillar.replace('_', ' ')} piece based on this opportunity.",
        "risk_flags": [],
        "auto_verdict": verdict,
        "reasoning": f"Rule-based assessment: {score}/100 brand fit (GPT-4o unavailable). Best pillar: {best_pillar}.",
    }
