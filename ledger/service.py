"""Cloud Run entry points. Cloud Run IAM authenticates every caller (Cloud
Tasks presents an OIDC token for the worker service account); /inbound also
requires the provider's HMAC signature.
"""

from __future__ import annotations

import json
import urllib.request

from fastapi import FastAPI, HTTPException, Request

from . import coordination, inbound, runtime, trace
from .config import build_info, settings

app = FastAPI(title="mission-ledger worker")


@app.get("/healthz")
def healthz():
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


def _metadata(path: str) -> str:
    req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/" + path,
                                 headers={"Metadata-Flavor": "Google"})
    return urllib.request.urlopen(req, timeout=3).read().decode()


def _test_permissions(url: str, token: str, permissions: list[str]) -> list[str]:
    req = urllib.request.Request(url, data=json.dumps({"permissions": permissions}).encode(),
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=10).read()).get("permissions", [])


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
