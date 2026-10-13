"""Execution manifest: what exactly ran when a mission first started.

ADK's resume guide warns against changing a stopped workflow before resuming
it. Personas and skills are versioned files, so the manifest pins their
content hashes, the code revision, the ADK version and the model descriptor.
On resume, a mismatch stops the mission for review instead of silently
resuming against whatever is on main two days later.
"""

from __future__ import annotations

import hashlib
import importlib.metadata as md
import pathlib

from .config import REPO_ROOT, build_info, settings


def tree_sha(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    for f in files:
        h.update(str(f.relative_to(REPO_ROOT)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def persona_path(name: str) -> pathlib.Path:
    return REPO_ROOT / "personas" / f"{name}.md"


def skill_path(name: str) -> pathlib.Path:
    return REPO_ROOT / "skills" / name


def build(mission: dict, model_descriptor: dict, session_id: str) -> dict:
    skills = {name: tree_sha(skill_path(name)) for name in mission["skills"]}
    return {
        "code_revision": build_info()["git_commit"],
        "adk_version": md.version("google-adk"),
        "persona": {"name": mission["persona"], "sha": tree_sha(persona_path(mission["persona"]))},
        "skills": skills,
        "skills_sha": hashlib.sha256(repr(sorted(skills.items())).encode()).hexdigest()[:16],
        "model": model_descriptor,
        "policy_version": settings().policy_version,
        "session": {"id": session_id, "agent_engine_id": settings().agent_engine_id,
                    "ttl_seconds": settings().session_ttl_seconds},
    }


def verify(pinned: dict, mission: dict, model_descriptor: dict) -> list[str]:
    """Return mismatches between the pinned manifest and this process."""
    current = build(mission, model_descriptor, pinned["session"]["id"])
    out = []
    for key in ("code_revision", "adk_version", "persona", "skills", "model", "policy_version"):
        if pinned.get(key) != current.get(key):
            out.append(f"{key}: pinned={pinned.get(key)!r} current={current.get(key)!r}")
    return out
