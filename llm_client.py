"""Shared LLM Client — Unified Router (Local ↔ Cloud).

Routes LLM calls between local MLX server and cloud APIs based on
task type, per-agent overrides, and automatic fallback.

Usage:
    from shared.llm_client import llm_call

    response = llm_call(
        system="You are Hawk...",
        user="Analyze this market...",
        agent="hawk",
        task_type="analysis",
    )

Cost tracking: Every call logged to ~/shared/llm_costs.jsonl.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)

SHARED_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SHARED_DIR / "llm_config.json"
COSTS_FILE = SHARED_DIR / "llm_costs.jsonl"

# In-memory config cache
_config: dict | None = None
_config_mtime: float = 0


def _load_config() -> dict:
    """Load routing config with file-change caching."""
    global _config, _config_mtime
    if not CONFIG_FILE.exists():
        return _default_config()
    try:
        mtime = CONFIG_FILE.stat().st_mtime
        if _config is not None and mtime == _config_mtime:
            return _config
        _config = json.loads(CONFIG_FILE.read_text())
        _config_mtime = mtime
        return _config
    except Exception:
        return _default_config()


def _default_config() -> dict:
    return {
        "local_server": {
            "base_url": "http://localhost:11434/v1",
            "timeout": 60,
        },
        "models": {
            "local_large": "mlx-community/Qwen2.5-14B-Instruct-4bit",
            "local_small": "mlx-community/Qwen2.5-3B-Instruct-4bit",
            "local_coder": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit",
        },
        "routing": {
            "reasoning": "local_large",
            "writing": "local_large",
            "analysis": "local_large",
            "fast": "local_small",
            "coding": "local_coder",
            "image_gen": "cloud_dalle",
            "voice": "cloud_elevenlabs",
        },
        "agent_overrides": {
            "thor": {"default": "local_coder"},
            "hawk": {"sports_analysis": "cloud_gpt4o"},
        },
        "fallback": {
            "local_large": "cloud_openai",
            "local_small": "cloud_openai",
            "local_coder": "cloud_claude",
            "cloud_openai": "cloud_claude",
        },
        "cloud": {
            "openai_model": "gpt-4o-mini",
            "claude_model": "claude-sonnet-4-20250514",
            "gpt4o_model": "gpt-4o",
        },
    }


def _log_cost(
    agent: str,
    provider: str,
    model: str,
    task_type: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    cost_usd: float,
    fallback: bool = False,
) -> None:
    """Append cost entry to JSONL log."""
    entry = {
        "ts": datetime.now(ET).isoformat(),
        "agent": agent,
        "provider": provider,
        "model": model,
        "task_type": task_type,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "cost_usd": round(cost_usd, 6),
        "fallback": fallback,
    }
    try:
        with open(COSTS_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost. Local = $0."""
    if provider == "local":
        return 0.0
    # Rough per-1M-token pricing
    rates = {
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4o": (2.50, 10.00),
        "claude-sonnet-4-20250514": (3.00, 15.00),
        "grok-4-1-fast-non-reasoning": (0.20, 0.50),
        "grok-3": (3.00, 15.00),
    }
    in_rate, out_rate = rates.get(model, (1.0, 3.0))
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def _is_local_server_up(cfg: dict) -> bool:
    """Quick health check on local LLM server."""
    import urllib.request
    import urllib.error
    base_url = cfg.get("local_server", {}).get("base_url", "http://localhost:11434/v1")
    try:
        req = urllib.request.Request(f"{base_url}/models", method="GET")
        with urllib.request.urlopen(req, timeout=3):
            return True
    except Exception:
        return False


def _call_local(
    cfg: dict, model_key: str, system: str, user: str,
    max_tokens: int, temperature: float,
) -> tuple[str, int, int]:
    """Call local MLX server (OpenAI-compatible). Returns (text, in_tokens, out_tokens)."""
    import urllib.request
    import urllib.error

    base_url = cfg.get("local_server", {}).get("base_url", "http://localhost:11434/v1")
    timeout = cfg.get("local_server", {}).get("timeout", 60)
    model_name = cfg.get("models", {}).get(model_key, model_key)

    payload = json.dumps({
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())

    text = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage", {})
    return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def _call_openai(
    model: str, system: str, user: str,
    max_tokens: int, temperature: float,
) -> tuple[str, int, int]:
    """Call OpenAI API. Returns (text, in_tokens, out_tokens)."""
    from openai import OpenAI
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    text = resp.choices[0].message.content.strip()
    usage = resp.usage
    return text, usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0


def _call_claude(
    model: str, system: str, user: str,
    max_tokens: int, temperature: float,
) -> tuple[str, int, int]:
    """Call Anthropic Claude API. Returns (text, in_tokens, out_tokens)."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        temperature=temperature,
    )
    text = resp.content[0].text.strip()
    return text, resp.usage.input_tokens, resp.usage.output_tokens




def _call_grok(
    model: str, system: str, user: str,
    max_tokens: int, temperature: float,
) -> tuple[str, int, int]:
    """Call xAI Grok API (OpenAI-compatible). Returns (text, in_tokens, out_tokens)."""
    from openai import OpenAI
    client = OpenAI(
        api_key=os.environ.get("XAI_API_KEY", ""),
        base_url="https://api.x.ai/v1",
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    text = resp.choices[0].message.content.strip()
    usage = resp.usage
    return text, usage.prompt_tokens if usage else 0, usage.completion_tokens if usage else 0

def _resolve_route(cfg: dict, agent: str, task_type: str) -> str:
    """Resolve which model route to use based on config."""
    # Check agent-level overrides first
    overrides = cfg.get("agent_overrides", {}).get(agent, {})
    if task_type in overrides:
        return overrides[task_type]
    if "default" in overrides:
        return overrides["default"]

    # Fall back to task_type routing
    return cfg.get("routing", {}).get(task_type, "local_large")


def llm_call(
    system: str,
    user: str,
    agent: str = "unknown",
    task_type: str = "reasoning",
    max_tokens: int = 500,
    temperature: float = 0.3,
    force_cloud: bool = False,
) -> str:
    """Unified LLM call with automatic routing and fallback.

    Args:
        system: System prompt.
        user: User message.
        agent: Which agent is calling (for routing + cost tracking).
        task_type: One of reasoning|writing|analysis|fast|coding|image_gen|voice.
        max_tokens: Max tokens to generate.
        temperature: Sampling temperature.
        force_cloud: Skip local, go straight to cloud.

    Returns:
        Generated text (empty string on total failure).
    """
    cfg = _load_config()
    route = _resolve_route(cfg, agent, task_type)
    cloud_cfg = cfg.get("cloud", {})
    fallback_map = cfg.get("fallback", {})

    # Build execution chain: primary → fallback → last resort
    chain = [route]
    fb = fallback_map.get(route)
    if fb:
        chain.append(fb)
        fb2 = fallback_map.get(fb)
        if fb2:
            chain.append(fb2)

    if force_cloud:
        # Strip local routes from chain
        chain = [r for r in chain if not r.startswith("local_")]
        if not chain:
            chain = ["cloud_openai"]

    for i, target in enumerate(chain):
        is_fallback = i > 0
        t0 = time.time()
        try:
            if target.startswith("local_"):
                if not _is_local_server_up(cfg):
                    log.info("Local LLM server offline, skipping to fallback")
                    continue
                text, in_tok, out_tok = _call_local(
                    cfg, target, system, user, max_tokens, temperature
                )
                provider = "local"
                model = cfg.get("models", {}).get(target, target)

            elif target == "cloud_openai":
                model = cloud_cfg.get("openai_model", "gpt-4o-mini")
                text, in_tok, out_tok = _call_openai(
                    model, system, user, max_tokens, temperature
                )
                provider = "openai"

            elif target == "cloud_gpt4o":
                model = cloud_cfg.get("gpt4o_model", "gpt-4o")
                text, in_tok, out_tok = _call_openai(
                    model, system, user, max_tokens, temperature
                )
                provider = "openai"

            elif target == "cloud_grok_fast":
                model = cloud_cfg.get("grok_fast_model", "grok-4-1-fast-non-reasoning")
                text, in_tok, out_tok = _call_grok(
                    model, system, user, max_tokens, temperature
                )
                provider = "xai"

            elif target == "cloud_grok":
                model = cloud_cfg.get("grok_reasoning_model", "grok-3")
                text, in_tok, out_tok = _call_grok(
                    model, system, user, max_tokens, temperature
                )
                provider = "xai"
            elif target == "cloud_claude":
                model = cloud_cfg.get("claude_model", "claude-sonnet-4-20250514")
                text, in_tok, out_tok = _call_claude(
                    model, system, user, max_tokens, temperature
                )
                provider = "anthropic"

            else:
                log.warning("Unknown route target: %s", target)
                continue

            latency = int((time.time() - t0) * 1000)
            cost = _estimate_cost(provider, model, in_tok, out_tok)

            _log_cost(
                agent=agent, provider=provider, model=model,
                task_type=task_type, input_tokens=in_tok,
                output_tokens=out_tok, latency_ms=latency,
                cost_usd=cost, fallback=is_fallback,
            )

            if is_fallback:
                log.info("LLM fallback: %s → %s for %s/%s", chain[0], target, agent, task_type)

            return text

        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            log.warning("LLM call failed [%s] for %s/%s (%dms): %s",
                        target, agent, task_type, latency, str(e)[:100])
            continue

    log.error("All LLM routes exhausted for %s/%s", agent, task_type)
    return ""


def llm_call_with_tools(
    system: str,
    messages: list[dict],
    tools: list[dict],
    agent: str = "unknown",
    task_type: str = "reasoning",
    max_tokens: int = 1000,
    temperature: float = 0.3,
    force_cloud: bool = False,
) -> dict:
    """OpenAI-compatible tool-calling LLM call (for Shelby's tool loop).

    Routes to local or cloud. Local MLX servers that support tool calling
    use the same OpenAI format. Falls back to cloud if local can't do tools.

    Returns:
        The raw response message dict (with tool_calls if present).
    """
    cfg = _load_config()
    route = _resolve_route(cfg, agent, task_type)
    cloud_cfg = cfg.get("cloud", {})

    # For tool calling, prefer local_large or cloud_openai
    if force_cloud or not route.startswith("local_") or not _is_local_server_up(cfg):
        # Use OpenAI for tool calling (most reliable)
        model = cloud_cfg.get("openai_model", "gpt-4o-mini")
        from openai import OpenAI
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
        t0 = time.time()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}] + messages,
            tools=tools,
            tool_choice="auto",
            max_tokens=max_tokens,
            temperature=temperature,
        )
        latency = int((time.time() - t0) * 1000)
        usage = resp.usage
        _log_cost(
            agent=agent, provider="openai", model=model,
            task_type=task_type,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency,
            cost_usd=_estimate_cost(
                "openai", model,
                usage.prompt_tokens if usage else 0,
                usage.completion_tokens if usage else 0,
            ),
            fallback=force_cloud,
        )
        return resp.choices[0].message

    # Try local server with tool calling
    import urllib.request
    base_url = cfg["local_server"]["base_url"]
    model_name = cfg["models"].get(route, route)
    timeout = cfg["local_server"].get("timeout", 60)

    payload = json.dumps({
        "model": model_name,
        "messages": [{"role": "system", "content": system}] + messages,
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()

    t0 = time.time()
    try:
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp_raw:
            data = json.loads(resp_raw.read().decode())
        latency = int((time.time() - t0) * 1000)
        usage = data.get("usage", {})
        _log_cost(
            agent=agent, provider="local", model=model_name,
            task_type=task_type,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency, cost_usd=0.0,
        )
        # Convert to OpenAI-like message object
        msg = data["choices"][0]["message"]
        return _dict_to_message(msg)
    except Exception as e:
        log.warning("Local tool-call failed, falling back to cloud: %s", str(e)[:100])
        return llm_call_with_tools(
            system, messages, tools, agent, task_type,
            max_tokens, temperature, force_cloud=True,
        )


def _dict_to_message(msg_dict: dict):
    """Convert a dict message to a simple namespace for compatibility."""
    class _Msg:
        pass
    m = _Msg()
    m.content = msg_dict.get("content")
    m.tool_calls = None
    if msg_dict.get("tool_calls"):
        m.tool_calls = []
        for tc in msg_dict["tool_calls"]:
            call = _Msg()
            call.id = tc.get("id", "")
            call.type = tc.get("type", "function")
            func = _Msg()
            func.name = tc["function"]["name"]
            func.arguments = tc["function"]["arguments"]
            call.function = func
            m.tool_calls.append(call)
    return m


# ── Cost Analytics ──

def get_cost_summary(hours: int = 24) -> dict:
    """Aggregate cost data from the last N hours."""
    if not COSTS_FILE.exists():
        return {"total_calls": 0, "total_cost": 0, "by_provider": {}, "by_agent": {}}

    cutoff = datetime.now(ET).timestamp() - hours * 3600
    totals = {"calls": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0}
    by_provider: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}

    with open(COSTS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = entry.get("ts", "")
            try:
                entry_time = datetime.fromisoformat(ts).timestamp()
                if entry_time < cutoff:
                    continue
            except Exception:
                continue

            totals["calls"] += 1
            totals["cost"] += entry.get("cost_usd", 0)
            totals["input_tokens"] += entry.get("input_tokens", 0)
            totals["output_tokens"] += entry.get("output_tokens", 0)

            prov = entry.get("provider", "unknown")
            if prov not in by_provider:
                by_provider[prov] = {"calls": 0, "cost": 0.0, "avg_latency": 0, "latencies": []}
            by_provider[prov]["calls"] += 1
            by_provider[prov]["cost"] += entry.get("cost_usd", 0)
            by_provider[prov]["latencies"].append(entry.get("latency_ms", 0))

            ag = entry.get("agent", "unknown")
            if ag not in by_agent:
                by_agent[ag] = {"calls": 0, "cost": 0.0}
            by_agent[ag]["calls"] += 1
            by_agent[ag]["cost"] += entry.get("cost_usd", 0)

    # Compute avg latencies
    for prov_data in by_provider.values():
        lats = prov_data.pop("latencies")
        prov_data["avg_latency_ms"] = int(sum(lats) / len(lats)) if lats else 0

    return {
        "hours": hours,
        "total_calls": totals["calls"],
        "total_cost": round(totals["cost"], 4),
        "total_input_tokens": totals["input_tokens"],
        "total_output_tokens": totals["output_tokens"],
        "by_provider": by_provider,
        "by_agent": by_agent,
    }
