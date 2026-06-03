"""
Builders that turn our cleaned dicts into FHIR-shaped resources, and helpers
to read our custom fields back out.

In production these map to standard Medplum resources:
  Patient        demographics + address (insurance -> Coverage,
                 service-needed -> ServiceRequest)
  Practitioner   the provider (discipline/region/capacity -> PractitionerRole)
  Task           the suggestion + accept/decline lifecycle

To keep the stand-in simple we tuck our working fields under a "matchData"
key on each resource. The shape of everything else is real FHIR.
"""
import time

from . import config


def _sched_json(sched: dict) -> dict:
    return {
        "flexible": sched.get("flexible", True),
        "days": sorted(sched.get("days") or []),
        "blocks": sorted(sched.get("blocks") or []),
    }


def patient_resource(p: dict) -> dict:
    complete, missing = config.completeness(p)
    md = {**p, "needed": sorted(p["needed"])}
    if "schedule" in md:
        md["schedule"] = _sched_json(md["schedule"])
    return {
        "resourceType": "Patient",
        "id": p["id"],
        "name": [{"text": p["name"]}],
        "address": [{"city": p["city"], "state": p["state"], "postalCode": p["zip"]}],
        "matchData": md,
        "completeness": {"complete": complete, "missing": missing},
    }


def practitioner_resource(pr: dict, index: int) -> dict:
    md = dict(pr)
    if "schedule" in md:
        md["schedule"] = _sched_json(md["schedule"])
    return {
        "resourceType": "Practitioner",
        "id": f"prac-{index}",
        "name": [{"text": pr["name"]}],
        "matchData": md,
    }


def task_resource(patient_id: str, prac_id: str, ev: dict, *,
                  status: str = "requested", queue_position: int = 1) -> dict:
    """Match offer. requested = active 48h window; on-hold = queued backup."""
    now = time.time()
    return {
        "resourceType": "Task",
        "status": status,                      # requested | on-hold | completed | rejected | cancelled
        "intent": "proposal",
        "for": {"reference": f"Patient/{patient_id}"},
        "owner": {"reference": f"Practitioner/{prac_id}"},
        "score": ev["score"],
        "golden": ev.get("golden", False),
        "mode": ev["mode"],
        "distance_mi": ev["distance_mi"],
        "pay_note": ev["pay_note"],
        "parts": ev["parts"],
        "summary": "",
        "statusReason": "",
        "createdAt": now,
        "expiresAt": now + config.OFFER_TIMEOUT_HOURS * 3600,
        "queuePosition": queue_position,
    }


# --- small reference helpers ---
def ref_id(reference) -> str:
    if isinstance(reference, dict):
        reference = reference.get("reference", "")
    return reference.split("/", 1)[1] if "/" in reference else reference


def patient_match(patient_resource: dict) -> dict:
    """Return the working dict, restoring the 'needed' set."""
    md = dict(patient_resource["matchData"])
    md["needed"] = set(md.get("needed", []))
    return md
