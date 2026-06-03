"""
Insurance handling.

Two jobs:
  1. Clean a messy plan name into a canonical payer (rules first, LLM only
     for the leftovers, cached so we call the API at most once per name).
  2. Decide payment is OK: self pay always passes; insurance passes if the
     plan is in the provider's network. If the provider's network is unknown
     (not in the export, would come from Medplum) we pass but flag it.
"""
import re

import requests

from . import config

# Rule-based canonicalizer. Order matters: more specific first.
_RULES = [
    (r"healthkeepers|anthem.*medicaid|medicaid.*anthem", "Anthem HealthKeepers (Medicaid)"),
    (r"anthem|bcbs|blue cross", "Blue Cross Blue Shield / Anthem"),
    (r"carefirst", "CareFirst BCBS"),
    (r"aetna better health", "Aetna Better Health (Medicaid)"),
    (r"aetna", "Aetna"),
    (r"cigna", "Cigna"),
    (r"tricare", "Tricare"),
    (r"united|uhc|optum", "UnitedHealthcare"),
    (r"medicaid", "Medicaid"),
    (r"medicare", "Medicare"),
]

_llm_cache: dict[str, str] = {}


def _rule_normalize(plan: str) -> str | None:
    p = (plan or "").lower()
    for pattern, canonical in _RULES:
        if re.search(pattern, p):
            return canonical
    return None


def _llm_normalize(plan: str) -> str | None:
    if not (config.USE_LLM and config.ANTHROPIC_API_KEY):
        return None
    if plan in _llm_cache:
        return _llm_cache[plan]
    prompt = (
        "Map this US health insurance plan name to its canonical payer "
        "(parent insurer) name. Reply with only the payer name, nothing else.\n\n"
        f"Plan: {plan}"
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": config.ANTHROPIC_MODEL,
                "max_tokens": 40,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        text = "".join(b.get("text", "") for b in resp.json().get("content", [])).strip()
        out = text or plan
        _llm_cache[plan] = out
        return out
    except Exception:
        return None


def normalize_payer(plan: str) -> str:
    """Canonical payer name for a plan. Rules first, LLM for leftovers."""
    if not plan:
        return ""
    return _rule_normalize(plan) or _llm_normalize(plan) or plan


def payment_ok(patient: dict, provider: dict) -> tuple[bool, str]:
    """Returns (passes, note)."""
    billing = patient["billing"]
    if "self" in billing:                      # self-pay
        return True, "self pay"

    payer = normalize_payer(patient["plan_name"])
    nets = provider.get("networks")
    if nets is None:                           # unknown -> Medplum would fill this
        if config.ACCEPT_UNKNOWN_NETWORK:
            return True, f"{payer or 'insurance'} (network not verified)"
        return False, "provider network unknown"
    if payer.lower() in nets:
        return True, f"in network: {payer}"
    return False, f"out of network: {payer or 'insurance'}"
