"""
All settings in one place. Keys come from the environment (.env), never code.
Everything has a safe default so the app runs fully offline.
"""
import os
from pathlib import Path


def _flag(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ---- storage ----
# Local FHIR-shaped store stands in for Medplum until a real instance is wired.
DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH = DATA_DIR / "store.db"

# ---- API keys (optional) ----
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()
AWS_LOCATION_API_KEY = os.getenv("AWS_LOCATION_API_KEY", "").strip()
AWS_REGION = os.getenv("AWS_REGION", "us-east-1").strip()
AWS_PLACE_INDEX = os.getenv("AWS_PLACE_INDEX", "").strip()

USE_LLM = _flag("USE_LLM", False)

# ---- matching rules ----
DEFAULT_RADIUS_MILES = float(os.getenv("DEFAULT_RADIUS_MILES", "25"))
AVG_DRIVE_MPH = float(os.getenv("AVG_DRIVE_MPH", "30"))
PATIENT_TELEHEALTH_DEFAULT = _flag("PATIENT_TELEHEALTH_DEFAULT", True)
PROVIDER_TELEHEALTH_DEFAULT = _flag("PROVIDER_TELEHEALTH_DEFAULT", True)
ACCEPT_UNKNOWN_NETWORK = _flag("ACCEPT_UNKNOWN_NETWORK", True)

# ---- scoring weights (base, sum = 100) ----
W_LOCATION = float(os.getenv("W_LOCATION", "35"))
W_SERVICE = float(os.getenv("W_SERVICE", "30"))
W_LIFECYCLE = float(os.getenv("W_LIFECYCLE", "20"))
W_TELEHEALTH = float(os.getenv("W_TELEHEALTH", "8"))
W_PREFERENCE = float(os.getenv("W_PREFERENCE", "7"))
W_AVAILABILITY = float(os.getenv("W_AVAILABILITY", "10"))
# At the same distance, self-pay patients score higher than insurance.
SELF_PAY_BONUS = float(os.getenv("SELF_PAY_BONUS", "6"))

# Providers in the "fill to 80%" stage are only filled to this fraction of their
# stated capacity, leaving headroom.
FILL_TO_TARGET = float(os.getenv("FILL_TO_TARGET", "0.8"))

# Each complete patient is offered to up to this many providers in order.
MATCH_QUEUE_SIZE = int(os.getenv("MATCH_QUEUE_SIZE", "3"))
# Active offer expires; patient advances to the next queued provider.
OFFER_TIMEOUT_HOURS = float(os.getenv("OFFER_TIMEOUT_HOURS", "48"))

# Among Pending 1st providers within this many miles of the closest one,
# assign the active offer to whoever has the fewest pending patients (tie: nearer).
PENDING_FIRST_BALANCE_BAND_MI = float(os.getenv("PENDING_FIRST_BALANCE_BAND_MI", "20"))

LIFECYCLE_PRIORITY = {
    "pending 1st": 1.00,
    "fill to 80%": 0.65,
    "steady state": 0.30,
    "unknown": 0.30,
}

REGION_STATE = {
    "nova": "VA", "rva": "VA", "va beach": "VA", "other va": "VA",
    "chicago": "IL", "pa": "PA", "denver": "CO", "fl": "FL",
}

SERVICE_TO_DISCIPLINE = {
    "speech therapy": {"SLP"},
    "occupational therapy": {"OT"},
    "speech and occupational therapy": {"SLP", "OT"},
    "physical therapy": {"PT"},
}

# A patient is matchable only with all three. Missing any -> needs-info queue.
def completeness(p: dict) -> tuple[bool, list[str]]:
    missing = []
    if not p.get("zip"):                       # need something we can geocode
        missing.append("location")
    if not p.get("needed"):                    # service must map to a discipline
        missing.append("service")
    billing = p.get("billing", "")
    billing_ok = ("self" in billing) or (billing == "insurance" and p.get("plan_name"))
    if not billing_ok:
        missing.append("insurance / billing")
    return (len(missing) == 0, missing)
