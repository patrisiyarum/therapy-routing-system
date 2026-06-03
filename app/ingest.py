"""
Read the two uploaded files and clean them into tidy records.

Handles the real mess in the data: trailing spaces in headers,
"Viriginia" typos, blank states, "No Address on file", mixed casing.
Accepts .csv or .xlsx.
"""
import io
import re
from datetime import datetime

import pandas as pd

from . import availability, config

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def _read_any(file_bytes: bytes, filename: str) -> pd.DataFrame:
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(io.BytesIO(file_bytes), dtype=str)
    else:
        df = pd.read_csv(io.BytesIO(file_bytes), dtype=str)
    # normalize headers: strip spaces, lower for lookup
    df.columns = [str(c).strip() for c in df.columns]
    return df.fillna("")


def _norm_state(raw: str, zipcode: str = "") -> str:
    s = (raw or "").strip()
    fix = {"viriginia": "VA", "virginia": "VA", "va": "VA", "maryland": "MD",
           "md": "MD", "dc": "DC", "washington dc": "DC"}
    key = s.lower()
    if key in fix:
        return fix[key]
    if len(s) == 2:
        return s.upper()
    # fall back to the zip code if the state is blank or odd
    if zipcode:
        try:
            import zipcodes
            hit = zipcodes.matching(zipcode.strip()[:5])
            if hit:
                return hit[0]["state"]
        except Exception:
            pass
    return s.upper()


def _age(birth: str) -> int | None:
    birth = (birth or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            d = datetime.strptime(birth, fmt)
            today = datetime.today()
            return today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        except ValueError:
            continue
    return None


def parse_patients(file_bytes: bytes, filename: str) -> list[dict]:
    df = _read_any(file_bytes, filename)

    def col(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_id = col("Unique ID", "id")
    c_name = col("client preferred name", "client legal name")
    c_last = col("client last name")
    c_group = col("client group")
    c_status = col("Current Status")
    c_dob = col("Client birth date")
    c_zip = col("zipcode", "zip")
    c_city = col("city")
    c_state = col("state")
    c_bill = col("billing type")
    c_planname = col("insurance1 plan ID")  # holds the human plan name in this data
    c_payerid = col("insurance1 payer ID (FROM THE OFFICEALLY LIST)")
    c_tele = col("telehealth", "telehealth ok", "patient telehealth")
    c_avail_days = col("availability days", "patient availability days")
    c_avail_times = col("availability times", "patient availability", "preferred times")

    out = []
    for _, r in df.iterrows():
        zipcode = (r.get(c_zip, "") or "").strip()[:5]
        state = _norm_state(r.get(c_state, ""), zipcode)
        group = (r.get(c_group, "") or "").strip()
        tele_raw = (r.get(c_tele, "") or "").strip().lower() if c_tele else ""
        if tele_raw in ("y", "yes", "true", "1"):
            tele = True
        elif tele_raw in ("n", "no", "false", "0"):
            tele = False
        else:
            tele = config.PATIENT_TELEHEALTH_DEFAULT  # from Medplum in real life
        first = (r.get(c_name, "") or "").strip()
        last = (r.get(c_last, "") or "").strip()
        out.append({
            "id": (r.get(c_id, "") or "").strip(),
            "name": (first + " " + last).strip() or first or "(unnamed)",
            "status": (r.get(c_status, "") or "").strip(),
            "service": group,
            "needed": config.SERVICE_TO_DISCIPLINE.get(group.lower(), set()),
            "age": _age(r.get(c_dob, "")),
            "city": (r.get(c_city, "") or "").strip(),
            "state": state,
            "zip": zipcode,
            "billing": (r.get(c_bill, "") or "").strip().lower(),  # 'insurance' / 'self-pay'
            "plan_name": (r.get(c_planname, "") or "").strip(),
            "payer_id": (r.get(c_payerid, "") or "").strip(),
            "telehealth": tele,
            "schedule": availability.schedule_from_fields(
                r.get(c_avail_days, "") if c_avail_days else "",
                r.get(c_avail_times, "") if c_avail_times else "",
            ),
        })
    # keep every active patient; completeness (location/service/billing) is
    # checked later so incomplete patients route to the needs-info queue.
    return [p for p in out if p["status"].lower() == "active"]


def parse_providers(file_bytes: bytes, filename: str) -> list[dict]:
    df = _read_any(file_bytes, filename)

    def col(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_name = col("Name")
    c_type = col("Type")
    c_region = col("Region")
    c_accept = col("Accepting Patients")
    c_cap = col("Caseload Capacity")  # trailing space already stripped
    c_addr = col("Address")
    c_life = col("Lifecycle State")
    c_range = col("Range (min)")
    c_radius = col("Radius (mi)")
    c_tele = col("telehealth", "provider telehealth")
    c_net = col("Networks", "Accepted Insurances", "insurance networks")
    c_note = col("Note", "Notes", "Availability", "availability notes")
    c_avail_days = col("availability days", "provider availability days")
    c_avail_times = col("availability times", "provider availability")

    out = []
    for _, r in df.iterrows():
        addr = (r.get(c_addr, "") or "").strip()
        usable_addr = addr and "no address" not in addr.lower()
        zip_hit = ZIP_RE.search(addr) if usable_addr else None
        zipcode = zip_hit.group(1) if zip_hit else ""

        region = (r.get(c_region, "") or "").strip()
        state = config.REGION_STATE.get(region.lower(), "")

        life = (r.get(c_life, "") or "").strip().lower()
        # strip leading numbering like "2. pending 1st"
        life = re.sub(r"^\s*\d+\.\s*", "", life)

        radius = (r.get(c_radius, "") or "").strip()
        rng = (r.get(c_range, "") or "").strip()
        try:
            radius_mi = float(radius) if radius else None
        except ValueError:
            radius_mi = None
        try:
            range_min = float(rng) if rng else None
        except ValueError:
            range_min = None

        cap_raw = (r.get(c_cap, "") or "").strip()
        try:
            capacity = int(float(cap_raw)) if cap_raw else 0
        except ValueError:
            capacity = 0

        tele_raw = (r.get(c_tele, "") or "").strip().lower() if c_tele else ""
        if tele_raw in ("y", "yes", "true", "1"):
            tele = True
        elif tele_raw in ("n", "no", "false", "0"):
            tele = False
        else:
            tele = config.PROVIDER_TELEHEALTH_DEFAULT

        networks = None
        if c_net and (r.get(c_net, "") or "").strip():
            networks = {x.strip().lower() for x in re.split(r"[;,|]", r.get(c_net)) if x.strip()}

        out.append({
            "name": (r.get(c_name, "") or "").strip(),
            "discipline": (r.get(c_type, "") or "").strip().upper(),  # SLP / OT / PT
            "region": region,
            "state": state,
            "accepting": (r.get(c_accept, "") or "").strip().upper(),  # Y / N / ''
            "capacity": capacity,
            "address": addr,
            "zip": zipcode,
            "lifecycle": life or "unknown",
            "radius_mi": radius_mi,
            "range_min": range_min,
            "telehealth": tele,
            "networks": networks,  # set of accepted payer names, or None if unknown
            "schedule": availability.schedule_from_fields(
                r.get(c_avail_days, "") if c_avail_days else "",
                r.get(c_avail_times, "") if c_avail_times else "",
                (r.get(c_note, "") or "").strip() if c_note else "",
            ),
        })
    return out
