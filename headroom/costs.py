"""Subscription cost catalog and helpers.

Costs are operator-declared (what you pay), not scraped from providers.
Known list-price defaults seed the wizard; ``monthly_cost_usd`` on each
account always wins once set.
"""

# USD / month list prices. Keys are (provider, normalized plan label substring).
# Order matters: first match wins. Keep labels short and distinctive.
DEFAULT_MONTHLY_COSTS = (
    # Claude Code / Claude.ai
    ("claude", "max 20x", 200.0),
    ("claude", "max 5x", 100.0),
    ("claude", "max", 100.0),
    ("claude", "team", 30.0),
    ("claude", "pro", 20.0),
    # ChatGPT / Codex
    ("codex", "pro lite", 200.0),
    ("codex", "chatgpt pro", 200.0),
    ("codex", "pro", 200.0),
    ("codex", "plus", 20.0),
    ("codex", "free", 0.0),
    # xAI Grok / SuperGrok
    ("grok", "heavy", 300.0),
    ("grok", "supergrok", 30.0),
    ("grok", "grok", 0.0),
    # Manus
    ("manus", "team", 199.0),
    ("manus", "pro", 39.0),
    ("manus", "starter", 19.0),
    ("manus", "free", 0.0),
    # NVIDIA Build / NIM free tier (promo: free for a year)
    ("nvidia", "free", 0.0),
    ("nvidia", "nvidia", 0.0),
)


def normalize_plan(plan):
    return " ".join(str(plan or "").lower().split())


def default_monthly_cost(provider, plan=None):
    """Best-effort list-price guess for a provider/plan, or None."""
    provider = (provider or "").lower().strip()
    label = normalize_plan(plan)
    for prov, needle, amount in DEFAULT_MONTHLY_COSTS:
        if prov != provider:
            continue
        if not label or needle in label:
            return float(amount)
    return None


def resolve_monthly_cost(account, plan=None):
    """Return the account's monthly USD cost, preferring explicit config."""
    if not isinstance(account, dict):
        return None
    raw = account.get("monthly_cost_usd")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value >= 0 and value == value else None  # reject NaN
    return default_monthly_cost(account.get("provider"),
                                plan or account.get("plan"))


def _money(amount):
    amount = float(amount)
    if amount == int(amount):
        return f"${int(amount)}"
    return f"${amount:.2f}"


def annual_cost(monthly):
    """Translate monthly USD to yearly (×12), or None."""
    if monthly is None:
        return None
    if isinstance(monthly, bool) or not isinstance(monthly, (int, float)):
        return None
    if monthly < 0 or monthly != monthly:  # NaN
        return None
    return float(monthly) * 12.0


def format_usd(amount, *, period="month"):
    """Format a USD amount. period is 'month', 'year', or 'both' (mo + /yr)."""
    if amount is None:
        return None
    if period == "year":
        return f"{_money(amount)}/yr"
    if period == "both":
        yearly = annual_cost(amount)
        if yearly is None:
            return None
        return f"{_money(amount)}/mo · {_money(yearly)}/yr"
    return f"{_money(amount)}/mo"


def format_monthly_with_annual(monthly):
    """Show monthly cost and its yearly translation."""
    return format_usd(monthly, period="both")
