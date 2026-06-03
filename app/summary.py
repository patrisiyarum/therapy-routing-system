"""
The 'why we think this patient is a good fit' text a provider reads.

Grounded in the real match facts: geocoded distance, mode, service, lifecycle,
payment, and whether it's a golden case. Written by Claude when USE_LLM is on,
otherwise a clear templated sentence. Either way we cache it onto the Task so a
provider profile never regenerates (and never re-bills) the same summary.
"""
import requests

from . import config, scoring
from .store import store


def _facts(patient: dict, prov_name: str, t: dict) -> str:
    where = (f"{t['distance_mi']} miles away, in person" if t["mode"] == "in person"
             else "reachable by telehealth" if t["mode"] == "telehealth" else "")
    golden = " This is a golden case: self pay for a brand new provider." if t["golden"] else ""
    return (f"Patient needs {patient['service']} in {patient['city']}, {patient['state']}, "
            f"paying by {patient['billing']}. Provider {prov_name} is {where}, "
            f"payment status {t['pay_note']}, match score {t['score']} out of 100.{golden}")


def _template(patient: dict, t: dict) -> str:
    bits = []
    if t["mode"] == "in person" and t["distance_mi"] is not None:
        bits.append(f"{t['distance_mi']} mi away, in person")
    elif t["mode"] == "telehealth":
        bits.append("covered by telehealth")
    bits.append(f"matches {patient['service']}")
    bits.append(t["pay_note"])
    text = "Good fit: " + "; ".join(bits) + "."
    if t["golden"]:
        text += " Golden case (self pay, helps fill a new caseload)."
    return text


def _llm(patient: dict, prov_name: str, t: dict) -> str | None:
    if not (config.USE_LLM and config.ANTHROPIC_API_KEY):
        return None
    prompt = ("In one warm, concrete sentence for a therapy provider, say why this "
              "patient is a good fit for them. Use the location and the facts given. "
              "No preamble.\n\n" + _facts(patient, prov_name, t))
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 90,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=15)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip() or None
    except Exception:
        return None


def summary_for(task: dict, patient: dict, prov_name: str) -> str:
    """Return the cached summary, generating + caching it on first view.

    Uses the instant templated sentence; no live AI calls are made so provider
    profiles load immediately even with long candidate lists.
    """
    if task.get("summary"):
        return task["summary"]
    text = _template(patient, task)
    task["summary"] = text
    store.update(task)
    return text
