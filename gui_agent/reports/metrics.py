"""Token, cost, and context-pressure helpers for report rendering."""

from __future__ import annotations

from gui_agent.core.config import model_price

# Token cost: priced per MODULE using that module's model rate (config.yaml `pricing`).
# Different modules may run different models (e.g. action_policy=35b, output=flash),
# so cost is summed module-by-module rather than from one flat rate.

# _Timer module name → llm config key.
_MODULE_CFG: dict[str, str] = {
    "transition": "supervisor",
    "orchestrator.coding": "orchestrator",
    "orchestrator.coding_reviewed.generate": "orchestrator",
    "orchestrator.coding_reviewed.review": "orchestrator",
    "action_policy": "action_policy",
}


def _sum_tokens(token_usage: dict) -> tuple[int, int]:
    """Sum per-module {input, output} into a (total_input, total_output) pair."""
    ti = sum(int(v.get("input", 0)) for v in (token_usage or {}).values())
    to = sum(int(v.get("output", 0)) for v in (token_usage or {}).values())
    return ti, to


# Set from context.json's run-level `models` map (config_key → model). Missing → "",
# which model_price() prices at the configured default — no active-config fallback.
_MODELS_MAP: dict[str, str] = {}


def _module_model(module: str) -> str:
    return _MODELS_MAP.get(_MODULE_CFG.get(module, module), "")


def _token_cost(token_usage: dict) -> float:
    """Cost (currency units) for one token_usage dict, summed per module by its model price."""
    total = 0.0
    for module, tu in (token_usage or {}).items():
        pin, pout = model_price(_module_model(module))
        total += int(tu.get("input", 0)) / 1_000_000 * pin + int(tu.get("output", 0)) / 1_000_000 * pout
    return total


def _fmt_tokens(n: int) -> str:
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


CONTEXT_WINDOW = 128_000  # model context budget to monitor against (peak single-call input)
_CTX_WARN = 0.75          # amber ≥ 75% of the window
_CTX_DANGER = 0.90        # red   ≥ 90%


def _ctx_color(used: int) -> str:
    """Color for a context-size figure by its share of CONTEXT_WINDOW."""
    frac = used / CONTEXT_WINDOW if CONTEXT_WINDOW else 0
    return "#dc2626" if frac >= _CTX_DANGER else "#f59e0b" if frac >= _CTX_WARN else "#64748b"
