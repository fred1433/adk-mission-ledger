"""Builds an empty ADK agent for one mission segment, then equips it.

The agent receives a persona (personas/*.md), the skills its mission asks for
(skills/*, loaded with ADK's SkillToolset from the versioned folder), and
exactly three mission tools:

  propose_action   records a proposal and runs the gate; never sends
  wait_for_reply   long-running; returns nothing, so ADK pauses the invocation
  propose_memory   writes a quarantined memory candidate; never publishes

There is no send tool and no provider credential in the agent's reach. The
agent object lives for one segment and is discarded.
"""

from __future__ import annotations

import dataclasses

from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.skills import load_skill_from_dir
from google.adk.tools import FunctionTool, LongRunningFunctionTool
from google.adk.tools.skill_toolset import SkillToolset
from google.adk.tools.tool_context import ToolContext

from . import actions, contacts, coordination, gate, memory, trace
from .coordination import Lease
from .manifest import persona_path, skill_path

APP_NAME = "mission_ledger"

RULES = """
Operating rules (enforced in code, repeated here so the model knows them):
- You cannot send anything. Call propose_action; a separate dispatcher sends
  only what the gate approved.
- Use only the purposes declared by your loaded skills.
- After a message that expects an answer, call wait_for_reply and stop.
- You may suggest a memory with propose_memory. It is quarantined until a
  validator approves it.
"""


@dataclasses.dataclass
class MissionCtx:
    db: object
    lease: Lease
    mission: dict
    manifest: dict
    thread_token: str


def _skill_meta(skills) -> tuple[list[str], list[str]]:
    purposes, channels = [], []
    for s in skills:
        meta = s.frontmatter.metadata or {}
        purposes += str(meta.get("purposes", "")).split()
        channels += str(meta.get("channels", "")).split()
    return sorted(set(purposes)), sorted(set(channels))


def build(ctx: MissionCtx, model) -> tuple[App, list[str]]:
    skills = [load_skill_from_dir(skill_path(n)) for n in ctx.mission["skills"]]
    purposes, channels = _skill_meta(skills)
    db, mission = ctx.db, ctx.mission

    def propose_action(purpose: str, channel: str, recipient: str, body: str,
                       tool_context: ToolContext, note: str = "") -> dict:
        """Propose one outbound action. It is gated; it is not sent by this call.

        Args:
          purpose: one of the purposes declared by the loaded skills.
          channel: "email" or "draft".
          recipient: the contact alias to address.
          body: the full text.
          note: optional remark for the human log; it never changes the gate.
        """
        ref = contacts.resolve(db, recipient)
        a, created = actions.record_proposal(
            db, ctx.lease, purpose=purpose, channel=channel, recipient=recipient,
            contact_id=ref.contact_id, body=body, note=note, allowed_purposes=purposes,
            policy_version=ctx.manifest["policy_version"],
            persona_sha=ctx.manifest["persona"]["sha"], skills_sha=ctx.manifest["skills_sha"],
            fc_ref={"invocation_id": tool_context.invocation_id,
                    "function_call_id": tool_context.function_call_id})
        trace.record(db, mission["mission_id"], "action.proposed", action_id=a["action_id"],
                     created=created, purpose=purpose, model_note=note or None,
                     generation=ctx.lease.generation)
        if a["status"] == "PROPOSED":
            verdict = gate.evaluate(
                a, mission,
                reserved=coordination.reservation_held_by(db, a["contact_id"], mission["mission_id"]),
                suppressed=actions.is_suppressed(db, a["contact_id"]),
                allowed_channels=channels)
            a = actions.set_verdict(db, ctx.lease, a["action_id"], verdict)
            trace.record(db, mission["mission_id"], "gate.verdict", action_id=a["action_id"],
                         decision=verdict["decision"], stage=verdict["stage"],
                         reasons=verdict["reasons"], reviewer_mode=verdict.get("reviewer_mode"))
            if verdict.get("usage"):
                trace.record(db, mission["mission_id"], "model.usage", source="reviewer",
                             model=verdict["reviewer_mode"], **verdict["usage"])
        return {"action_id": a["action_id"], "status": a["status"],
                "reasons": (a.get("gate") or {}).get("reasons", [])}

    def wait_for_reply(action_id: str, tool_context: ToolContext) -> None:
        """Park the mission until the contact answers the message `action_id`.

        Returns nothing now; the reply arrives later as this call's response.
        """
        trace.record(db, mission["mission_id"], "agent.waiting", action_id=action_id,
                     pending_call_id=tool_context.function_call_id)
        return None

    def propose_memory(fact: str, scope_kind: str, topic: str, evidence: str) -> dict:
        """Suggest a memory. It is quarantined until a validator approves it.

        Args:
          fact: one sentence.
          scope_kind: "contact" (about this contact) or "playbook" (a general rule).
          topic: short snake_case topic key.
          evidence: what in this mission supports it.
        """
        scope = ({"contact_id": mission["contact_id"]} if scope_kind == "contact"
                 else {"playbook": mission["skills"][0]})
        c = memory.quarantine(db, mission_id=mission["mission_id"], fact=fact, scope=scope,
                              topic=topic, evidence=evidence)
        trace.record(db, mission["mission_id"], "memory.quarantined",
                     candidate_id=c["candidate_id"], topic=topic, scope=scope)
        return {"candidate_id": c["candidate_id"], "status": c["status"]}

    tools = [FunctionTool(propose_action), LongRunningFunctionTool(wait_for_reply),
             FunctionTool(propose_memory), SkillToolset(skills=skills)]
    agent = LlmAgent(
        name="mission_agent", model=model,
        instruction=persona_path(mission["persona"]).read_text() + RULES,
        tools=tools)
    app = App(name=APP_NAME, root_agent=agent,
              resumability_config=ResumabilityConfig(is_resumable=True))
    names = ["propose_action", "wait_for_reply", "propose_memory",
             "skills:" + ",".join(s.frontmatter.name for s in skills)]
    return app, names
