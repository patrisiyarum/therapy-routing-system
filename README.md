# Therapy Routing System

A living patient routing system, not a one-shot matcher. You upload the patient
and provider files. Patients with enough data are routed to a provider as a
pending suggestion. Each provider opens their profile, reads why each patient
fits, and adds or declines them. Caseloads update live and feed back into who
gets matched next. Patients without enough data wait in a separate queue.

Runs fully offline out of the box. The Anthropic and AWS keys are optional.

---

## Quick start

```bash
cd therapy-routing-system
./run.sh
```

Open http://127.0.0.1:8000, upload the two files in `sample_data/`, and the
dashboard fills in. Click a Pending 1st provider to open their profile and try
Add and Decline.

---

## The workflow

1. **Triage on upload.** Every patient is checked for the three things we need:
   a location we can map, a known service, and a billing type. Missing any one
   sends them to the **needs-info** queue with the missing field flagged. They
   never enter matching until the data is filled in.

2. **Route the rest.** Each complete patient is scored against every provider
   (location and service lead, lifecycle priority breaks ties) and suggested to
   their best provider that still has an open slot. The suggestion is a pending
   record, not a done deal.

3. **Provider responds.** On their profile a provider sees the patients
   suggested for them, each with a plain summary of why they fit, grounded in
   the geocoded distance and the match facts. They **Add** (accept) or
   **Decline** with a reason.

4. **Caseload updates live.** Add increases the caseload, which lowers that
   provider's open slots, so they stop being suggested once full. Decline sends
   the patient straight to their next best provider and stores the reason.

5. **Golden cases first.** A self-pay patient for a Pending 1st provider is the
   golden case: the cleanest way to fill a brand new provider. It gets a scoring
   **bonus**, so golden patients usually top a provider's list, but a great
   nearby non-golden patient can still compete. (Bonus, not a hard override.)

6. **Nobody disappears.** If every eligible provider declines a patient, they
   land in **manual review** rather than vanishing.

---

## Decisions baked in

- **Storage: Medplum.** Patients are `Patient` resources, providers are
  `Practitioner`, and every suggestion plus its accept/decline lifecycle is a
  `Task` (status requested, completed, or rejected). Caseload is just the count
  of completed Tasks. You don't have a Medplum instance yet, so this runs against
  a local FHIR-shaped store (`app/store.py`) that mirrors the slice of Medplum we
  use. To go live, replace that one class with a Medplum client; the method names
  (create, read, update, search) line up with Medplum's API and nothing else
  changes.
- **Golden case: a bonus,** not a hard rule.
- **Decline: re-route only for now,** with every reason stored on the Task so you
  can show the data and add learning later.

---

## Where the keys plug in

Copy `.env.example` to `.env` and add your own rotated keys.

- **Anthropic** (`app/summary.py`): writes the "why this fits" summary on a
  provider's profile, grounded in the real distance and match facts. Each summary
  is cached on its Task, so a profile never regenerates or re-bills the same text.
  It never decides eligibility or scores; that stays in code so every routing
  decision is repeatable.
- **AWS Location** (`app/geocode.py`): street-level geocoding. Off or
  unavailable, it falls back to offline zip-code distances automatically.

---

## Architecture

Full system design (Medplum target, matching pipeline, queues, fair Pending 1st
split, rebalance, and what is built today): **[ARCHITECTURE.md](ARCHITECTURE.md)** at
the repo root.

---

## Project layout

```
app/
  config.py     settings, weights, golden bonus, the completeness rule
  store.py      local FHIR store (the Medplum swap point)
  fhir.py       builds Patient / Practitioner / Task resources
  ingest.py     read + clean the two files
  geocode.py    AWS Location + offline zip fallback + distance
  insurance.py  normalize plan names, in-network check
  scoring.py    hard filters + weighted score + golden bonus
  router.py     triage, matching, accept, decline + reroute, the buckets
  summary.py    grounded "why this fits" text (LLM or template), cached
  main.py       the web app + screens
  templates/    dashboard, provider profile, needs-info
data/           the local store lives here (created on first upload)
sample_data/    your two files, ready to upload
ARCHITECTURE.md matching pipeline, Medplum mapping, built vs planned
```

---

## Presenting it

Upload the two files. Walk the dashboard: 253 routed, 28 in needs-info, caseloads
at zero. Open a Pending 1st provider and show the golden self-pay patients at the
top with their fit summaries. Click Add on one and return to the dashboard to show
that provider's caseload tick up. Open another patient and Decline with a reason,
then show they have been re-routed to a new provider. Finish on the needs-info
queue to show the system never guesses on incomplete data.
