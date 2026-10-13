"""Offline tests: no network, no Google Cloud, no model calls."""

import asyncio
import json

import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from ledger import actions, contacts, gate, memory, models
from ledger.config import REPO_ROOT


def test_alias_normalisation():
    assert contacts.normalise_alias(" Jo.Lee@Example.COM ") == "email:jo.lee@example.com"
    assert contacts.normalise_alias("+1 (555) 010-0199") == "phone:+15550100199"
    assert contacts.normalise_alias("@JoLee") == "handle:jolee"
    with pytest.raises(ValueError):
        contacts.normalise_alias("5550100")  # no country code: refuse to guess


def test_action_id_is_business_level():
    assert actions.action_id("m-1", "first_contact") == "m-1.first_contact.t0.v1"
    assert actions.action_id("m-1", "first_contact", turn=1, revision=2) == "m-1.first_contact.t1.v2"
    with pytest.raises(ValueError):
        actions.action_id("m-1", "Send Now!")


def test_document_ids_do_not_collide():
    from ledger.coordination import doc_id
    assert doc_id("c/1") != doc_id("c_1") and "/" not in doc_id("c/1")


def test_stop_requests():
    from ledger.runtime import is_stop_request
    assert is_stop_request("STOP") and is_stop_request("  unsubscribe please")
    assert not is_stop_request("Thursday at 15:00 works, don't stop now")


def test_digest_binds_payload_and_versions():
    a = {"channel": "email", "contact_id": "c", "recipient": "r", "body": "hello there",
         "policy_version": "v1", "persona_sha": "p", "skills_sha": "s"}
    d0 = actions.payload_digest(a)
    for k, v in [("body", "hello there!"), ("recipient", "x"), ("policy_version", "v2"), ("skills_sha", "t")]:
        assert actions.payload_digest({**a, k: v}) != d0


MISSION = {"mission_id": "m", "kind": "consultation_follow_up", "contact_id": "c"}


def _action(body):
    return {"purpose": "first_contact", "channel": "email", "contact_id": "c", "body": body}


def test_gate_deterministic_reject_skips_reviewer():
    v = gate.evaluate(_action("Hi, this costs $450 and starts Thursday."), MISSION, True, False, ["email"])
    assert v["decision"] == "REJECT" and v["stage"] == "deterministic" and v["reviewer_mode"] is None


@pytest.mark.parametrize("body,check", [
    ("Hi ${mission.facts.first_name}, Tuesday or Thursday?", "no_unresolved_placeholders"),
    ("Hi Jo, book here: https://evil.test/x before Thursday please", "links_only_to_allowed_domains"),
    ("short", "length_within_bounds"),
])
def test_gate_checks(body, check):
    v = gate.evaluate(_action(body), MISSION, True, False, ["email"])
    assert check in v["reasons"]


def test_gate_requires_reservation_and_no_suppression():
    body = "Hi Jo, I can offer Tuesday at 10:00 or Thursday at 15:00. Which suits you?"
    assert "contact_reserved_by_this_mission" in gate.evaluate(_action(body), MISSION, False, False, ["email"])["reasons"]
    assert "contact_not_suppressed" in gate.evaluate(_action(body), MISSION, True, True, ["email"])["reasons"]


def test_reviewer_ambiguity_is_needs_review():
    assert gate._normalise({"verdict": "MAYBE"}, "x")["decision"] == "NEEDS_REVIEW"
    assert gate._normalise(None, "x")["decision"] == "NEEDS_REVIEW"


def test_fixture_model_picks_turn_from_history():
    m, desc = models.for_mission({"kind": "consultation_follow_up", "contact_alias": "jo@example.com",
                                  "facts": {"first_name": "Jo"}})
    assert desc["mode"] == "fixture"
    hist = [types.Content(role="user", parts=[types.Part(text="go")]),
            types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="load_skill"))]),
            types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
                name="load_skill", response={}))])]

    async def first():
        async for r in m.generate_content_async(LlmRequest(contents=hist)):
            return r

    r = asyncio.run(first())
    fc = r.content.parts[0].function_call
    assert fc.name == "propose_action" and "Hi Jo," in fc.args["body"]
    assert fc.args["recipient"] == "jo@example.com"


def test_live_mode_needs_two_switches(monkeypatch):
    from ledger import config
    config.settings.cache_clear()
    monkeypatch.setenv("LEDGER_MODEL_MODE", "live")
    assert not config.settings().paid_models_enabled
    config.settings.cache_clear()
    monkeypatch.setenv("LEDGER_ALLOW_PAID_MODEL_CALLS", "yes")
    assert config.settings().paid_models_enabled
    config.settings.cache_clear()


def test_memory_rules():
    c = {"fact": "Offering a discount makes hesitant contacts reply faster.", "scope": {"playbook": "x"},
         "evidence": "one reply"}
    failed = [x["check"] for x in memory.rule_checks(c, supporting_missions=1) if not x["passed"]]
    assert failed == ["playbook_needs_two_missions"]
    c2 = {"fact": "Call them on +1 555 010 0199 after lunch.", "scope": {"contact_id": "c"}, "evidence": "e"}
    assert "no_raw_contact_data" in [x["check"] for x in memory.rule_checks(c2, 1) if not x["passed"]]


def test_fake_bank_refuses_generate_and_update():
    bank = memory.FakeMemoryBank()
    bank.create(name="engines/1", fact="Prefers afternoon appointments.", scope={"contact_id": "c"})
    assert [m["fact"] for m in bank.list(name="engines/1")] == ["Prefers afternoon appointments."]
    with pytest.raises(AssertionError):
        bank.generate()
    with pytest.raises(AssertionError):
        bank.update()


def test_skill_metadata_declares_purposes():
    from google.adk.skills import load_skill_from_dir
    from ledger.agent_factory import _skill_meta
    skills = [load_skill_from_dir(REPO_ROOT / "skills" / n) for n in ("follow-up-writer", "reply-triage")]
    purposes, channels = _skill_meta(skills)
    assert purposes == ["confirm_after_reply", "first_contact"] and channels == ["email"]


def test_fixtures_are_valid_json():
    for p in (REPO_ROOT / "fixtures").rglob("*.json"):
        json.loads(p.read_text())


def test_image_contains_every_runtime_folder():
    copied = {line.split()[1] for line in (REPO_ROOT / "Dockerfile").read_text().splitlines()
              if line.startswith("COPY ")}
    for folder in ("ledger", "skills", "personas", "fixtures", "policies"):
        assert folder in copied, folder
