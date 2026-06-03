"""
Web app for the routing workflow. Run with:  uvicorn app.main:app --reload

Screens:
  /                 operations dashboard: upload, buckets, provider list
  /provider/{id}    a provider's profile: caseload + suggested patients,
                    each with a 'why this fits' summary, Add / Decline
  /needs-info       patients missing data, with the missing field called out
"""
from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from . import cleanup, config, ingest, router
from .store import store

BASE = Path(__file__).parent
app = FastAPI(title="Therapy Routing System")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _keys():
    return {"anthropic": bool(config.ANTHROPIC_API_KEY),
            "aws": bool(config.AWS_LOCATION_API_KEY)}


def _loaded() -> bool:
    return len(store.search("Patient")) > 0


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if _loaded():
        router.expire_stale_offers()
    ctx = {"keys": _keys(), "loaded": _loaded(),
           "uploaded": request.query_params.get("uploaded") == "1",
           "upload_n": request.query_params.get("patients", ""),
           "upload_p": request.query_params.get("providers", "")}
    if _loaded():
        ctx["buckets"] = router.buckets()
        ctx["providers"] = router.providers_overview()
        ctx["manual"] = router.manual_review_list()
        ctx["review"] = router.provider_review_list()
    return templates.TemplateResponse(request=request, name="dashboard.html", context=ctx)


@app.post("/upload")
async def upload(patients_file: UploadFile = File(...),
                 providers_file: UploadFile = File(...)):
    patients = ingest.parse_patients(await patients_file.read(), patients_file.filename)
    providers = ingest.parse_providers(await providers_file.read(), providers_file.filename)
    cleanup.clean_patients(patients)   # one-time Claude cleaner: locations, service, billing
    stats = router.load_files(patients, providers)
    router.run_matching()
    return RedirectResponse(
        f"/?uploaded=1&patients={stats['patients']}&providers={stats['providers']}",
        status_code=303,
    )


@app.post("/run-matching")
def rematch():
    router.run_matching()
    return RedirectResponse("/", status_code=303)


@app.post("/rebalance")
def rebalance():
    router.rebalance_matching()
    return RedirectResponse("/?rebalanced=1", status_code=303)


@app.get("/provider/{prac_id}", response_class=HTMLResponse)
def provider(request: Request, prac_id: str):
    pr = store.read("Practitioner", prac_id)
    if not pr:
        return RedirectResponse("/", status_code=303)
    md = pr["matchData"]
    suggestions = router.provider_suggestions(prac_id)
    from .scoring import lifecycle_label
    stated = md["capacity"]
    target = router.scoring.effective_capacity(md)
    info = {
        "id": prac_id, "name": md["name"], "discipline": md["discipline"],
        "region": md["region"], "lifecycle": lifecycle_label(md["lifecycle"]),
        "lifecycle_raw": md["lifecycle"], "capacity": target,
        "stated_capacity": stated, "fill_to_80": md["lifecycle"] == "fill to 80%",
        "accepted": router.caseload(prac_id),
    }
    info["pending"] = len(suggestions)
    return templates.TemplateResponse(request=request, name="provider.html",
                                      context={"p": info, "suggestions": suggestions})


@app.post("/provider/{prac_id}/accept")
def accept(prac_id: str, task_id: str = Form(...)):
    router.accept_task(task_id)
    return RedirectResponse(f"/provider/{prac_id}", status_code=303)


@app.post("/provider/{prac_id}/decline")
def decline(prac_id: str, task_id: str = Form(...), reason: str = Form(default="")):
    router.decline_task(task_id, reason)
    return RedirectResponse(f"/provider/{prac_id}", status_code=303)


@app.get("/needs-info", response_class=HTMLResponse)
def needs_info(request: Request):
    return templates.TemplateResponse(request=request, name="needs_info.html",
                                      context={"rows": router.needs_info_list()})
