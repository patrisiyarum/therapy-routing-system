"""
Score one patient against one provider.

Stage 1 hard filters (must pass all): right discipline, licensed in state,
live and accepting, has capacity room, within in-person driving range, and
payment works. (Telehealth is not used; out-of-range patients go to review.)

Stage 2 score (0-100ish): location leads, lifecycle priority is a tie breaker.
Selection is distance-first: each patient is matched to the nearest eligible
provider with an open slot.
"""
from . import availability, config, geocode, insurance


def effective_radius(provider: dict) -> float:
    if provider["radius_mi"]:
        return provider["radius_mi"]
    if provider["range_min"]:
        return provider["range_min"] / 60.0 * config.AVG_DRIVE_MPH
    return float("inf")   # no stated limit -> provider has no distance cap


def stated_capacity(provider: dict) -> int:
    """Full caseload limit from the roster."""
    return provider["capacity"]


def fill_target(provider: dict) -> int:
    """Soft target for 'fill to 80%' lifecycle (display + auto stage change)."""
    cap = stated_capacity(provider)
    if provider["lifecycle"] == "fill to 80%":
        return int(cap * config.FILL_TO_TARGET)
    return cap


def effective_capacity(provider: dict) -> int:
    """Dashboard capacity bar target (80% milestone or full)."""
    return fill_target(provider)


def lifecycle_rank(provider: dict) -> int:
    """Lower = higher priority when building patient match queues."""
    order = {"pending 1st": 0, "fill to 80%": 1, "steady state": 2,
             "unknown": 3, "at capacity": 4, "not live": 5}
    return order.get(provider["lifecycle"], 3)


def evaluate(patient: dict, provider: dict, p_coords, prov_coords) -> dict:
    fails = []

    service_ok = provider["discipline"] in patient["needed"]
    if not service_ok:
        fails.append(f"does not provide {patient['service']}")

    state_ok = bool(provider["state"]) and provider["state"] == patient["state"]
    if not state_ok:
        fails.append("not licensed in patient's state")

    available_ok = (provider["lifecycle"] not in ("not live", "at capacity")
                    and provider.get("accepting") != "N")
    if not available_ok:
        fails.append("not live / not accepting / at capacity")

    pay_ok, pay_note = insurance.payment_ok(patient, provider)
    if not pay_ok:
        fails.append(pay_note)

    sched_ok, sched_score, sched_note = availability.overlap(patient, provider)
    if not sched_ok:
        fails.append(f"schedule: {sched_note}")

    dist = geocode.distance_miles(p_coords, prov_coords)
    radius = effective_radius(provider)
    in_person_ok = dist is not None and dist <= radius
    if not in_person_ok:
        if dist is None:
            fails.append("no mappable location")
        else:
            fails.append(f"too far ({dist:.0f} mi)")

    mode = "in person" if in_person_ok else "n/a"
    result = {
        "eligible": not fails, "fails": fails,
        "distance_mi": round(dist, 1) if dist is not None else None,
        "radius_mi": round(radius, 1), "mode": mode, "pay_note": pay_note,
        "score": 0.0, "parts": {}, "golden": False, "schedule_overlap": 0.0,
    }
    if fails:
        return result

    result["schedule_overlap"] = sched_score

    # --- scoring --- (eligible always means an in-person match now)
    closeness = max(0.0, 1.0 - (dist / radius)) if radius else 1.0
    loc = config.W_LOCATION * closeness
    svc = config.W_SERVICE
    life = config.W_LIFECYCLE * config.LIFECYCLE_PRIORITY.get(provider["lifecycle"], 0.30)
    pref = config.W_PREFERENCE * 0.6

    avail = config.W_AVAILABILITY * sched_score
    parts = {"location": round(loc, 1), "service": round(svc, 1),
             "lifecycle": round(life, 1), "availability": round(avail, 1),
             "preference": round(pref, 1)}
    if "self" in (patient.get("billing") or ""):
        parts["self-pay"] = round(config.SELF_PAY_BONUS, 1)

    result["parts"] = parts
    result["golden"] = False
    result["score"] = round(min(100.0, sum(parts.values())), 1)
    return result


def lifecycle_label(life: str) -> str:
    return {"pending 1st": "Pending 1st", "fill to 80%": "Fill to 80%",
            "steady state": "Steady state", "at capacity": "At capacity",
            "not live": "Not live", "unknown": "Unknown"}.get(life, life.title())


ACTIVE_LIFECYCLES = {"pending 1st", "fill to 80%", "steady state"}


def data_conflict(provider: dict) -> str | None:
    """Flag a provider whose Accepting Patients (Y/N) column contradicts their
    lifecycle state. These are held out of matching until a human resolves them."""
    acc = (provider.get("accepting") or "").strip().upper()
    life = provider["lifecycle"]
    if acc == "N" and life in ACTIVE_LIFECYCLES:
        return f"Lifecycle is '{lifecycle_label(life)}' (active) but Accepting Patients = N"
    if acc == "Y" and life == "not live":
        return "Accepting Patients = Y but Lifecycle is 'Not live'"
    return None
