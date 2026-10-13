"""Cloud Run entry points. Cloud Run IAM authenticates every caller (Cloud
Tasks presents an OIDC token for the worker service account); /inbound also
requires the provider's HMAC signature.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from fastapi import FastAPI, HTTPException, Request

from . import coordination, inbound, intake, runtime, trace
from .config import build_info, settings

app = FastAPI(title="mission-ledger worker")


@app.get("/health")
def health():
    return {"ok": True, "build": build_info(), "process": trace.process_identity()}


@app.post("/tasks/start")
async def start(req: Request):
    body = await req.json()
    try:
        return await runtime.start(body["mission_id"])
    except coordination.LeaseUnavailable as e:
        raise HTTPException(409, str(e))  # Cloud Tasks retries later


@app.post("/tasks/resume")
async def resume(req: Request):
    body = await req.json()
    try:
        return await runtime.resume(body["mission_id"], body["event_id"])
    except coordination.LeaseUnavailable as e:
        raise HTTPException(409, str(e))


@app.post("/inbound")
async def inbound_reply(req: Request):
    raw = await req.body()
    if not inbound.verify(settings().webhook_secret, raw, req.headers.get("X-Sandbox-Signature")):
        raise HTTPException(401, "bad signature")
    return inbound.accept(coordination.client(settings().project), json.loads(raw))


@app.post("/tasks/reconcile-wakes")
def reconcile():
    return {"woken": inbound.reconcile_pending_wakes(coordination.client(settings().project))}


@app.post("/tasks/intake")
def run_intake():
    """Cloud Scheduler, every minute: BigQuery missions -> start tasks."""
    return intake.run(coordination.client(settings().project))


@app.post("/tasks/sweep")
def sweep():
    """Cloud Scheduler: missions left RUNNING after their lease expired -> review."""
    return {"sent_to_review": runtime.sweep_interrupted(coordination.client(settings().project))}


@app.get("/diag/model-permission")
def model_permission():
    """Does this process's identity hold aiplatform.endpoints.predict right now?
    testIamPermissions only; no model is called."""
    token = json.loads(_metadata("instance/service-accounts/default/token"))["access_token"]
    got = _test_permissions(
        f"https://cloudresourcemanager.googleapis.com/v3/projects/{settings().project}:testIamPermissions",
        token, ["aiplatform.endpoints.predict"])
    return {"process": trace.process_identity(), "granted": got}


def _metadata(path: str) -> str:
    req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/" + path,
                                 headers={"Metadata-Flavor": "Google"})
    return urllib.request.urlopen(req, timeout=3).read().decode()


def _test_permissions(url: str, token: str, permissions: list[str]) -> list[str]:
    req = urllib.request.Request(url, data=json.dumps({"permissions": permissions}).encode(),
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"}, method="POST")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=10).read()).get("permissions", [])
    except urllib.error.HTTPError as e:  # a refusal is itself evidence; return it
        return [f"ERROR {e.code}: {e.read().decode()[:300]}"]


MEMORY_PERMS = ["aiplatform.memories.create", "aiplatform.memories.update",
                "aiplatform.memories.delete", "aiplatform.memories.generate",
                "aiplatform.memories.get", "aiplatform.memories.list",
                "aiplatform.memories.retrieve", "aiplatform.sessions.create",
                "aiplatform.sessionEvents.append"]


@app.get("/diag/identity")
def identity():
    """Which permissions does the identity running THIS process hold?

    Uses testIamPermissions, which calls no Memory Bank endpoint and costs
    nothing. It is a permission check, not a refused write.
    """
    email = _metadata("instance/service-accounts/default/email")
    token = json.loads(_metadata("instance/service-accounts/default/token"))["access_token"]
    s = settings()
    project_grants = _test_permissions(
        f"https://cloudresourcemanager.googleapis.com/v3/projects/{s.project}:testIamPermissions",
        token, MEMORY_PERMS)
    publisher = f"memory-publisher@{s.project}.iam.gserviceaccount.com"
    impersonation = _test_permissions(
        f"https://iam.googleapis.com/v1/projects/-/serviceAccounts/{publisher}:testIamPermissions",
        token, ["iam.serviceAccounts.getAccessToken", "iam.serviceAccounts.actAs",
                "iam.serviceAccounts.signJwt"])
    return {"process": trace.process_identity(), "service_account": email,
            "granted_on_project": sorted(project_grants),
            "denied_on_project": sorted(set(MEMORY_PERMS) - set(project_grants)),
            "can_impersonate_publisher": sorted(impersonation)}


@app.post("/diag/memory-write-probe")
def memory_write_probe():
    """Live IAM proof under the worker's real identity: one valid read, then
    one valid memories.create that the platform must refuse. Disabled unless
    LEDGER_ENABLE_PROBES=yes (a successful create would cost an embedding).
    """
    import os

    import vertexai

    if os.environ.get("LEDGER_ENABLE_PROBES") != "yes":
        raise HTTPException(404, "probes disabled")
    s = settings()
    client = vertexai.Client(project=s.project, location=s.location)
    engine = f"projects/{s.project}/locations/{s.location}/reasoningEngines/{s.agent_engine_id}"
    out = {"process": trace.process_identity(),
           "service_account": _metadata("instance/service-accounts/default/email")}
    try:
        out["read_ok"] = True
        out["memories_listed"] = len(list(client.agent_engines.memories.list(name=engine)))
    except Exception as e:  # noqa: BLE001
        out["read_ok"], out["read_error"] = False, str(e)[:300]
    try:
        client.agent_engines.memories.create(name=engine, fact="probe written by the worker identity",
                                             scope={"probe": "worker"})
        out["write_refused"] = False
    except Exception as e:  # noqa: BLE001
        out["write_refused"], out["write_error"] = True, str(e)[:300]
    return out
