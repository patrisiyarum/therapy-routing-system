"""
The workflow engine. Everything that changes state lives here.

Matching rules (the brain):
  - Triage incomplete patients to needs-info.
  - Filter: service, insurance (self-pay skips network filter), accepting/lifecycle.
  - Geocode, then rank by pending-1st priority, distance, score (self-pay bonus).
  - Active offer (#1): among Pending 1st within a distance band, pick whoever
    has the fewest pending offers (fair split); tie-break nearer.
  - Each patient gets up to 3 providers in a queue; pending-1st providers are tried first.
  - Provider #1 has 48h to accept; on timeout or decline, offer moves to #2, then #3.
  - Fill-to-80% providers: target is 80% of capacity for display, but accepts go to full
    stated capacity; at 80% accepted -> lifecycle becomes steady state; at full -> at capacity.
"""
import time

from . import config, fhir, geocode, scoring
from .store import store


# ---------- loading ----------
def load_files(patients: list[dict], providers: list[dict]) -> dict:
    store.clear()
    geocode._cache.clear()
    for i, pr in enumerate(providers):
        store.create("Practitioner", fhir.practitioner_resource(pr, i))
    complete = 0
    for p in patients:
        res = store.create("Patient", fhir.patient_resource(p))
        if res["completeness"]["complete"]:
            complete += 1
    return {"patients": len(patients), "providers": len(providers), "complete": complete}


# ---------- coordinate + lookup helpers ----------
def _provider_index() -> dict:
    out = {}
    for pr in store.search("Practitioner"):
        md = pr["matchData"]
        coords = geocode.geocode(address=md["address"], zipcode=md["zip"], state=md["state"])
        out[pr["id"]] = (md, coords)
    return out


def _patient_coords(md: dict):
    return geocode.geocode(zipcode=md["zip"], state=md["state"])


def _tasks_for_patient(pid: str) -> list[dict]:
    return store.search("Task", lambda t: fhir.ref_id(t["for"]) == pid)


def _accepted_counts() -> dict:
    counts = {}
    for t in store.search("Task", lambda t: t["status"] == "completed"):
        pid = fhir.ref_id(t["owner"])
        counts[pid] = counts.get(pid, 0) + 1
    return counts


def _active_pending_counts() -> dict:
    counts = {}
    for t in store.search("Task", lambda t: t["status"] == "requested"):
        pid = fhir.ref_id(t["owner"])
        counts[pid] = counts.get(pid, 0) + 1
    return counts


def caseload(prac_id: str) -> int:
    return len(store.search("Task", lambda t: t["status"] == "completed"
                            and fhir.ref_id(t["owner"]) == prac_id))


def _provider_has_room(prac_id: str, prov_idx: dict, accepted: dict) -> bool:
    md = prov_idx[prac_id][0]
    return accepted.get(prac_id, 0) < scoring.stated_capacity(md)


def _rank_key(prac_id: str, ev: dict, prov_idx: dict) -> tuple:
    pmd = prov_idx[prac_id][0]
    dist = ev["distance_mi"]
    return (scoring.lifecycle_rank(pmd),
            -ev.get("schedule_overlap", 0),
            0 if ev["mode"] == "in person" else 1,
            dist if dist is not None else float("inf"),
            -ev["score"])


def _eligible_ranked(md: dict, p_coords, prov_idx: dict, exclude: set) -> list[tuple]:
    ranked = []
    for prac_id, (pmd, coords) in prov_idx.items():
        if prac_id in exclude:
            continue
        ev = scoring.evaluate(md, pmd, p_coords, coords)
        if ev["eligible"]:
            ranked.append((prac_id, ev))
    ranked.sort(key=lambda t: _rank_key(t[0], t[1], prov_idx))
    return ranked


def _pick_active_provider(candidates: list[tuple], prov_idx: dict,
                          active_pending: dict) -> tuple:
    """Spread active offers across Pending 1st providers in the same distance band."""
    pending_first = [(prac_id, ev) for prac_id, ev in candidates
                     if prov_idx[prac_id][0]["lifecycle"] == "pending 1st"]
    if len(pending_first) < 2:
        return candidates[0]

    dists = [ev["distance_mi"] for _, ev in pending_first
             if ev["distance_mi"] is not None]
    if not dists:
        return candidates[0]

    band = min(dists) + config.PENDING_FIRST_BALANCE_BAND_MI
    in_band = [(prac_id, ev) for prac_id, ev in pending_first
               if ev["distance_mi"] is not None and ev["distance_mi"] <= band]
    if len(in_band) < 2:
        return candidates[0]

    in_band.sort(key=lambda t: (
        active_pending.get(t[0], 0),
        t[1]["distance_mi"],
        -t[1]["score"],
    ))
    return in_band[0]


def _tried_providers(tasks: list[dict]) -> set:
    """Providers the patient declined or was already placed with."""
    return {fhir.ref_id(t["owner"]) for t in tasks
            if t["status"] in ("rejected", "completed")}


def _sync_provider_lifecycle(prac_id: str) -> None:
    pr = store.read("Practitioner", prac_id)
    if not pr:
        return
    md = pr["matchData"]
    n = caseload(prac_id)
    stated = scoring.stated_capacity(md)
    target = scoring.fill_target(md)
    if n >= stated:
        md["lifecycle"] = "at capacity"
        md["accepting"] = "N"
    elif md["lifecycle"] == "fill to 80%" and n >= target:
        md["lifecycle"] = "steady state"
    pr["matchData"] = md
    store.update(pr)


def _cancel_sibling_tasks(pid: str, keep_id: str | None = None) -> None:
    for t in _tasks_for_patient(pid):
        if t["id"] == keep_id:
            continue
        if t["status"] in ("requested", "on-hold"):
            t["status"] = "cancelled"
            t["statusReason"] = "patient placed elsewhere"
            store.update(t)


def _activate_task(t: dict) -> dict:
    now = time.time()
    t["status"] = "requested"
    t["createdAt"] = now
    t["expiresAt"] = now + config.OFFER_TIMEOUT_HOURS * 3600
    return store.update(t)


def _advance_patient_queue(pid: str) -> bool:
    """Promote the next on-hold offer, or build a new queue if none left."""
    tasks = _tasks_for_patient(pid)
    if any(t["status"] == "completed" for t in tasks):
        return True
    if any(t["status"] == "requested" for t in tasks):
        return True

    on_hold = sorted(
        [t for t in tasks if t["status"] == "on-hold"],
        key=lambda t: t.get("queuePosition", 99))
    for t in on_hold:
        prac_id = fhir.ref_id(t["owner"])
        prov_idx = _provider_index()
        if prac_id not in prov_idx:
            continue
        if not _provider_has_room(prac_id, prov_idx, _accepted_counts()):
            t["status"] = "cancelled"
            t["statusReason"] = "provider at full capacity"
            store.update(t)
            continue
        _activate_task(t)
        return True

    pat = store.read("Patient", pid)
    if not pat or not pat["completeness"]["complete"]:
        return False
    md = fhir.patient_match(pat)
    exclude = _tried_providers(tasks)
    ranked = _eligible_ranked(md, _patient_coords(md), _provider_index(), exclude)
    return _assign_patient_queue(pid, ranked) > 0


def _assign_patient_queue(pid: str, ranked: list[tuple],
                          active_pending: dict | None = None) -> int:
    """Create up to MATCH_QUEUE_SIZE offers: first active, rest on-hold."""
    prov_idx = _provider_index()
    accepted = _accepted_counts()
    if active_pending is None:
        active_pending = _active_pending_counts()
    used: set[str] = set()
    picks: list[tuple] = []

    for slot in range(config.MATCH_QUEUE_SIZE):
        candidates = []
        for prac_id, ev in ranked:
            if prac_id in used:
                continue
            if not _provider_has_room(prac_id, prov_idx, accepted):
                continue
            candidates.append((prac_id, ev))
        if not candidates:
            break
        if slot == 0:
            pick = _pick_active_provider(candidates, prov_idx, active_pending)
        else:
            pick = candidates[0]
        picks.append(pick)
        used.add(pick[0])
        if slot == 0:
            prac_id = pick[0]
            active_pending[prac_id] = active_pending.get(prac_id, 0) + 1

    if not picks:
        return 0

    for i, (prac_id, ev) in enumerate(picks):
        status = "requested" if i == 0 else "on-hold"
        store.create("Task", fhir.task_resource(pid, prac_id, ev,
                                                status=status, queue_position=i + 1))
    return len(picks)


def expire_stale_offers() -> int:
    """48h timeout: cancel active offer and advance to next provider in queue."""
    now = time.time()
    n = 0
    for t in store.search("Task", lambda t: t["status"] == "requested"):
        if t.get("expiresAt", 0) > now:
            continue
        t["status"] = "cancelled"
        t["statusReason"] = "no response within 48 hours"
        store.update(t)
        n += 1
        _advance_patient_queue(fhir.ref_id(t["for"]))
    return n


# ---------- matching ----------
def run_matching() -> dict:
    expired = expire_stale_offers()
    prov_idx = _provider_index()
    accepted = _accepted_counts()

    waiting = []
    for pat in store.search("Patient", lambda p: p["completeness"]["complete"]):
        tasks = _tasks_for_patient(pat["id"])
        if any(t["status"] == "completed" for t in tasks):
            continue
        if any(t["status"] in ("requested", "on-hold") for t in tasks):
            continue
        md = fhir.patient_match(pat)
        exclude = _tried_providers(tasks)
        ranked = _eligible_ranked(md, _patient_coords(md), prov_idx, exclude)
        waiting.append((pat["id"], ranked))

    created = 0
    active_pending = _active_pending_counts()
    for pid, ranked in waiting:
        created += _assign_patient_queue(pid, ranked, active_pending)
    return {"suggested": created, "expired": expired}


def rebalance_matching() -> dict:
    """Cancel open offers and rebuild queues with current matching rules."""
    for t in store.search("Task"):
        if t["status"] in ("requested", "on-hold"):
            t["status"] = "cancelled"
            t["statusReason"] = "rebalanced"
            store.update(t)
    return run_matching()


def reroute_patient(pid: str) -> bool:
    return _advance_patient_queue(pid)


# ---------- provider actions ----------
def accept_task(task_id: str) -> dict | None:
    t = store.read("Task", task_id)
    if not t or t["status"] != "requested":
        return t
    prac_id = fhir.ref_id(t["owner"])
    prov_idx = _provider_index()
    if prac_id in prov_idx:
        md = prov_idx[prac_id][0]
        if caseload(prac_id) >= scoring.stated_capacity(md):
            return t
    pid = fhir.ref_id(t["for"])
    t["status"] = "completed"
    store.update(t)
    _cancel_sibling_tasks(pid, keep_id=task_id)
    _sync_provider_lifecycle(prac_id)
    return t


def decline_task(task_id: str, reason: str) -> dict | None:
    t = store.read("Task", task_id)
    if not t or t["status"] != "requested":
        return t
    t["status"] = "rejected"
    t["statusReason"] = reason.strip() or "no reason given"
    store.update(t)
    _advance_patient_queue(fhir.ref_id(t["for"]))
    return t


# ---------- views ----------
def patient_status(pat: dict) -> str:
    if not pat["completeness"]["complete"]:
        return "needs_info"
    tasks = _tasks_for_patient(pat["id"])
    if any(t["status"] == "completed" for t in tasks):
        return "accepted"
    if any(t["status"] in ("requested", "on-hold") for t in tasks):
        return "suggested"
    return "manual_review"


def buckets() -> dict:
    pats = store.search("Patient")
    statuses = [patient_status(p) for p in pats]
    return {
        "patients": len(pats),
        "needs_info": statuses.count("needs_info"),
        "suggested": statuses.count("suggested"),
        "accepted": statuses.count("accepted"),
        "manual_review": statuses.count("manual_review"),
    }


def providers_overview() -> list[dict]:
    prov = store.search("Practitioner")
    rows = []
    for pr in prov:
        md = pr["matchData"]
        accepted = caseload(pr["id"])
        stated = scoring.stated_capacity(md)
        target = scoring.fill_target(md)
        pending = len(store.search("Task", lambda t: t["status"] == "requested"
                                    and fhir.ref_id(t["owner"]) == pr["id"]))
        acc = (md.get("accepting") or "").strip().upper()
        rows.append({
            "id": pr["id"], "name": md["name"], "discipline": md["discipline"],
            "region": md["region"], "accepting": acc or "—",
            "lifecycle": scoring.lifecycle_label(md["lifecycle"]),
            "lifecycle_raw": md["lifecycle"], "capacity": target,
            "stated_capacity": stated, "fill_to_80": md["lifecycle"] == "fill to 80%",
            "accepted": accepted, "pending": pending,
            "at_full": accepted >= stated,
        })
    order = {"pending 1st": 0, "fill to 80%": 1, "steady state": 2,
             "unknown": 3, "at capacity": 4, "not live": 5}
    rows.sort(key=lambda r: (r["pending"] == 0, order.get(r["lifecycle_raw"], 3)))
    return rows


def provider_suggestions(prac_id: str) -> list[dict]:
    tasks = store.search("Task", lambda t: t["status"] == "requested"
                         and fhir.ref_id(t["owner"]) == prac_id)
    out = []
    for t in tasks:
        pat = store.read("Patient", fhir.ref_id(t["for"]))
        md = pat["matchData"]
        out.append({"task": t, "patient": md, "patient_res": pat})
    out.sort(key=lambda r: (r["task"]["distance_mi"] is None,
                            r["task"]["distance_mi"] or 0.0))
    return out


def needs_info_list() -> list[dict]:
    pats = store.search("Patient", lambda p: not p["completeness"]["complete"])
    return [{"id": p["id"], "name": p["matchData"]["name"],
             "service": p["matchData"]["service"] or "(none)",
             "city": p["matchData"]["city"], "state": p["matchData"]["state"],
             "missing": p["completeness"]["missing"],
             "outreach": p["matchData"].get("outreach", "")} for p in pats]


def provider_review_list() -> list[dict]:
    out = []
    for pr in store.search("Practitioner"):
        md = pr["matchData"]
        reason = scoring.data_conflict(md)
        if reason:
            out.append({"id": pr["id"], "name": md["name"],
                        "discipline": md["discipline"], "region": md["region"],
                        "accepting": md.get("accepting") or "(blank)",
                        "lifecycle": scoring.lifecycle_label(md["lifecycle"]),
                        "reason": reason})
    return out


def manual_review_list() -> list[dict]:
    out = []
    for p in store.search("Patient", lambda p: p["completeness"]["complete"]):
        if patient_status(p) == "manual_review":
            md = p["matchData"]
            tasks = _tasks_for_patient(p["id"])
            declined = sum(1 for t in tasks
                           if t["status"] in ("rejected", "cancelled"))
            out.append({"id": p["id"], "name": md["name"], "service": md["service"],
                        "city": md["city"], "state": md["state"], "declined": declined})
    return out
