"""
One-time, upload-time data cleaner powered by Claude.

Runs once when files are uploaded (only if an Anthropic key is set) and only on
values the deterministic rules can't already handle. Every answer is cached to
disk (data/ai_cleanup.json) so the same messy value is never sent to the model
twice, and the actual routing stays rule-based and instant.

It repairs three things, and never invents data it has no basis for:
  locations  unmappable ZIPs (typos, PO-box-only) -> a valid ZIP for the
             city/state, validated against the offline ZIP database first.
  service    free-text "client group" that doesn't map to a discipline -> one
             of the known services (so SLP/OT/PT routing works).
  billing    odd billing wording ("Private Pay", "Cash", "BCBS") -> the
             canonical "self-pay" or "insurance".

A suggestion that doesn't validate (or comes back "unknown") is ignored, so a
bad guess can never silently misroute a patient.
"""
import json

import requests

from . import config

CACHE_PATH = config.DATA_DIR / "ai_cleanup.json"

SERVICE_OPTIONS = ["speech therapy", "occupational therapy",
                   "speech and occupational therapy", "physical therapy"]
BILLING_OPTIONS = ["self-pay", "insurance"]


# ---------- cache ----------
def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        config.DATA_DIR.mkdir(exist_ok=True)
        CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


# ---------- shared Claude call ----------
def _claude_json(prompt: str) -> dict:
    if not config.ANTHROPIC_API_KEY:
        return {}
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 700,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=25)
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            return {}
        return json.loads(text[start:end + 1])
    except Exception:
        return {}


def _claude_text(prompt: str) -> str:
    if not config.ANTHROPIC_API_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": config.ANTHROPIC_MODEL, "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=25)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception:
        return ""


def _valid_zip(zipcode: str, state: str) -> bool:
    try:
        import zipcodes
        hits = zipcodes.matching((zipcode or "").strip()[:5])
        if not hits:
            return False
        if state and hits[0]["state"] != state:
            return False
        return True
    except Exception:
        return False


# ---------- location repair ----------
def _fix_locations(patients: list[dict], cache: dict) -> int:
    todo = []
    for i, p in enumerate(patients):
        z = (p.get("zip") or "").strip()
        if z and _valid_zip(z, p.get("state", "")):
            continue
        if not (p.get("city") or "").strip():
            continue                       # nothing to reason from -> leave for staff
        ckey = f'zip::{p["city"]}|{p.get("state", "")}|{z}'.lower()
        if ckey in cache:
            p["_zipfix"] = cache[ckey]
        else:
            todo.append({"idx": str(i), "ckey": ckey, "city": p["city"],
                         "state": p.get("state", ""), "zip": z})

    if todo:
        lines = "\n".join(
            f'{it["idx"]}: city="{it["city"]}", state="{it["state"]}", bad_zip="{it["zip"]}"'
            for it in todo)
        prompt = ("These US patient locations have a missing or unmappable 5-digit ZIP. "
                  "For each id give the single most likely correct 5-digit USPS ZIP for "
                  "that city/state, or null if unsure. Reply ONLY a JSON object mapping "
                  "id (string) to ZIP string or null.\n\n" + lines)
        answers = _claude_json(prompt)
        for it in todo:
            new = answers.get(it["idx"])
            new = str(new).strip()[:5] if new else None
            cache[it["ckey"]] = new
            patients[int(it["idx"])]["_zipfix"] = new

    fixed = 0
    for p in patients:
        new = p.pop("_zipfix", None)
        if new and _valid_zip(new, p.get("state", "")):
            p["zip_original"] = p.get("zip")
            p["zip"] = new
            p["location_fixed"] = True
            fixed += 1
    return fixed


# ---------- categorical normalization (service / billing) ----------
def _normalize_field(patients: list[dict], field: str, options: list[str],
                     cache: dict, needs_fix) -> int:
    """Map messy values of `field` to one of `options`. needs_fix(p) decides
    which patients are currently unrecognized."""
    distinct = {}
    for p in patients:
        if not needs_fix(p):
            continue
        raw = (p.get(field) or "").strip()
        if raw:
            distinct.setdefault(raw.lower(), raw)

    uncached = [v for v in distinct if f"{field}::{v}" not in cache]
    if uncached:
        listing = "\n".join(f'- "{distinct[v]}"' for v in uncached)
        prompt = (f"Map each messy {field} value to exactly one of these options: "
                  f"{options} or \"unknown\". Reply ONLY a JSON object mapping the "
                  f"original value (string) to the chosen option.\n\n" + listing)
        answers = _claude_json(prompt)
        norm = {str(k).strip().lower(): str(v).strip().lower() for k, v in answers.items()}
        for v in uncached:
            choice = norm.get(distinct[v].lower()) or norm.get(v)
            cache[f"{field}::{v}"] = choice if choice in options else None

    fixed = 0
    for p in patients:
        if not needs_fix(p):
            continue
        raw = (p.get(field) or "").strip().lower()
        choice = cache.get(f"{field}::{raw}")
        if choice in options:
            p[f"{field}_original"] = p.get(field)
            p[field] = choice
            if field == "service":
                p["needed"] = config.SERVICE_TO_DISCIPLINE.get(choice, set())
            fixed += 1
    return fixed


_MISSING_ASK = {
    "location": "their full home address (street, city, state, and ZIP)",
    "service": "which therapy they need (speech, occupational, or physical)",
    "insurance / billing": "how they will pay (self-pay, or their insurance plan name)",
}


def _draft_outreach(patients: list[dict], cache: dict) -> int:
    """For patients still missing intake data, draft a 'please confirm' message.
    One template per distinct set of missing fields (cached), personalized by name."""
    combos = {}                            # combo_key -> readable item list
    for p in patients:
        complete, missing = config.completeness(p)
        if complete:
            continue
        key = ",".join(sorted(missing))
        combos.setdefault(key, [_MISSING_ASK.get(m, m) for m in missing])

    for key, items in combos.items():
        ckey = f"outreach::{key}"
        if ckey in cache:
            continue
        asks = "; ".join(items)
        prompt = ("Write a short, warm message a pediatric therapy clinic sends to a "
                  "patient or their guardian to collect missing intake details so we can "
                  f"match them with a therapist. Politely ask them to confirm: {asks}. "
                  "Start exactly with 'Hi [NAME],'. Keep it under 55 words, one clear "
                  "ask. Do not add any sign-off, closing, signature, clinic name, or "
                  "bracketed placeholders other than [NAME]. Reply with only the message.")
        msg = _claude_text(prompt)
        cache[ckey] = msg or ""

    drafted = 0
    for p in patients:
        complete, missing = config.completeness(p)
        if complete:
            continue
        tmpl = cache.get(f"outreach::{','.join(sorted(missing))}", "")
        if tmpl:
            first = (p.get("name") or "there").split()[0]
            p["outreach"] = tmpl.replace("[NAME]", first)
            drafted += 1
    return drafted


def clean_patients(patients: list[dict]) -> dict:
    """Run the full one-time cleaner. Returns a summary of what was repaired."""
    if not config.ANTHROPIC_API_KEY:
        return {"locations": 0, "services": 0, "billing": 0, "outreach": 0}

    cache = _load_cache()
    locations = _fix_locations(patients, cache)
    services = _normalize_field(
        patients, "service", SERVICE_OPTIONS, cache,
        needs_fix=lambda p: not p.get("needed"))
    billing = _normalize_field(
        patients, "billing", BILLING_OPTIONS, cache,
        needs_fix=lambda p: "self" not in (p.get("billing") or "")
        and (p.get("billing") or "") != "insurance")
    outreach = _draft_outreach(patients, cache)
    _save_cache(cache)
    return {"locations": locations, "services": services,
            "billing": billing, "outreach": outreach}


# kept for backward compatibility
def fix_patient_locations(patients: list[dict]) -> int:
    return clean_patients(patients)["locations"]
