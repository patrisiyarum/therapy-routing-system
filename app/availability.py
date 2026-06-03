"""
Schedule overlap for matching.

Uses simple day + time-block sets (Mon–Sun, AM / PM / Evening). Patients and
providers without explicit schedules are treated as flexible and stay eligible.

Provider free-text notes (e.g. "Wed slots, evenings") are parsed when present.
"""
import re

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
BLOCKS = ("am", "pm", "evening")
ALL_DAYS = set(DAYS)
ALL_BLOCKS = set(BLOCKS)

_DAY_PATTERNS = [
    (r"\bmon(day)?\b", "mon"), (r"\btue(s(day)?)?\b", "tue"), (r"\bwed(nes(day)?)?\b", "wed"),
    (r"\bthu(rs(day)?)?\b", "thu"), (r"\bfri(day)?\b", "fri"), (r"\bsat(urday)?\b", "sat"),
    (r"\bsun(day)?\b", "sun"), (r"\bweekdays?\b", "weekday"), (r"\bweekends?\b", "weekend"),
]
_BLOCK_PATTERNS = [
    (r"\bmorning?s?\b|\bam\b", "am"),
    (r"\bafternoon?s?\b", "pm"),
    (r"\bevening?s?\b", "evening"),
    (r"\bpm\b", "pm"),
]


def _parse_days_blocks(text: str) -> tuple[set[str], set[str]]:
    t = (text or "").lower()
    if not t:
        return set(), set()
    if any(w in t for w in ("flexible", "any time", "anytime", "open schedule", "go with the flow")):
        return set(), set()

    days: set[str] = set()
    blocks: set[str] = set()
    for pat, token in _DAY_PATTERNS:
        if re.search(pat, t):
            if token == "weekday":
                days.update({"mon", "tue", "wed", "thu", "fri"})
            elif token == "weekend":
                days.update({"sat", "sun"})
            else:
                days.add(token)
    for pat, token in _BLOCK_PATTERNS:
        if re.search(pat, t):
            blocks.add(token)

    return days, blocks


def _parse_list_field(raw: str, valid: set[str]) -> set[str]:
    out = set()
    for part in re.split(r"[,;/|]+", (raw or "").lower()):
        token = part.strip()[:3] if part.strip() else ""
        for v in valid:
            if token == v or part.strip().lower() == v:
                out.add(v)
                break
        if "weekday" in part.lower():
            out.update({"mon", "tue", "wed", "thu", "fri"})
        if "weekend" in part.lower():
            out.update({"sat", "sun"})
    return out & valid


def schedule_from_fields(days_raw: str = "", blocks_raw: str = "", note: str = "") -> dict:
    days = _parse_list_field(days_raw, ALL_DAYS)
    blocks = _parse_list_field(blocks_raw, ALL_BLOCKS)
    if note:
        nd, nb = _parse_days_blocks(note)
        days |= nd
        blocks |= nb
    flexible = not days and not blocks
    if days and not blocks:
        blocks = set(ALL_BLOCKS)
    if blocks and not days:
        days = set(ALL_DAYS)
    return {"days": days, "blocks": blocks, "flexible": flexible}


def _schedule(raw: dict | None) -> dict:
    s = raw or {}
    return {
        "flexible": s.get("flexible", True),
        "days": set(s.get("days") or []),
        "blocks": set(s.get("blocks") or []),
    }


def overlap(patient: dict, provider: dict) -> tuple[bool, float, str]:
    """Returns (eligible, overlap_score 0–1, note)."""
    ps = _schedule(patient.get("schedule"))
    prs = _schedule(provider.get("schedule"))
    if ps.get("flexible") or prs.get("flexible"):
        return True, 0.5, "flexible schedule"

    pd, pb = ps.get("days") or set(), ps.get("blocks") or set()
    rd, rb = prs.get("days") or set(), prs.get("blocks") or set()
    shared_days = pd & rd
    shared_blocks = pb & rb
    if not shared_days or not shared_blocks:
        return False, 0.0, "no overlapping days/times"

    day_frac = len(shared_days) / max(len(pd), len(rd), 1)
    block_frac = len(shared_blocks) / max(len(pb), len(rb), 1)
    score = min(1.0, max(0.35, (day_frac + block_frac) / 2))
    return True, round(score, 2), f"overlap {len(shared_days)}d / {len(shared_blocks)} blocks"
