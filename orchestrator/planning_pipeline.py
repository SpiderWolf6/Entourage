"""planning pipeline — EM → PO → Architect → PL → HR.

five agents run sequentially. each emits events via PlanningCallbacks so the
frontend gets real-time updates over SSE.

flow:
  1. engineering manager  — conversational intake, produces a structured brief
  2. product owner        — brief → full requirements doc
  3. architect            — requirements → tech architecture + stack selection
  4. project lead         — architecture → sprint plan with task assignments
  5. hr director          — sprint plan → dev agent personas (spawned for execution)
"""

from __future__ import annotations

import os
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from agents.registry import AgentRegistry, discover_agents
from agents.stacks.profiles import auto_detect_stack, get_profile
from orchestrator.pipeline_state import PipelineState
from orchestrator.clarification_bus import clarification_bus

log = logging.getLogger(__name__)

# rough cost estimate used only for the pipeline_done total — per-agent costs come from the llm client
COST_PER_TOKEN = 0.000002

PLANNING_PHASES = ["engineering_manager", "product_owner", "architect", "project_lead", "hr"]

PHASE_LABELS = {
    "engineering_manager": "Clarifying Requirements",
    "product_owner":       "Refining Requirements",
    "architect":           "Designing Architecture",
    "project_lead":        "Planning Sprints",
    "hr":                  "Assembling Team",
}


@dataclass
class PlanningEvent:
    type: str
    agent: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanningCallbacks:
    on_event: Callable[[PlanningEvent], Awaitable[None]] | None = None


async def _emit(cb: PlanningCallbacks, event: PlanningEvent):
    if cb.on_event:
        await cb.on_event(event)


@dataclass
class PlanningResult:
    artifacts: dict[str, str] = field(default_factory=dict)
    token_usage: dict[str, int] = field(default_factory=dict)
    total_cost: float = 0.0
    stack: str = ""
    spawned_agents: list[dict] = field(default_factory=list)


class BudgetExceededError(Exception):
    def __init__(self, agent: str, used: int, budget: int):
        self.agent = agent
        self.used = used
        self.budget = budget
        super().__init__(f"agent '{agent}' used {used} tokens, exceeding budget of {budget}")


def _split_monitor_summary(output: str) -> tuple[str, str]:
    """split agent output into (artifact, monitor_summary).

    agents may embed a MONITOR_SUMMARY: block between their main output and MEMORY_ENTRY.
    we strip it from the artifact (so it doesn't pollute stored artifacts) and return
    it separately so the pipeline can emit it as an agent_summary event.
    MEMORY_ENTRY is reattached to the artifact because state parsing needs it.
    """
    marker = "MONITOR_SUMMARY:"
    if marker not in output:
        return output, ""
    parts = output.split(marker, 1)
    artifact = parts[0].rstrip()
    rest = parts[1].strip()
    if "MEMORY_ENTRY:" in rest:
        summary_part, memory_part = rest.split("MEMORY_ENTRY:", 1)
        summary = summary_part.strip()
        artifact = artifact + "\nMEMORY_ENTRY:" + memory_part
    else:
        summary = rest
    return artifact, summary


def _count_tokens(text: str) -> int:
    """rough token count: 4 chars per token (good enough for budget checks)."""
    return max(1, len(text) // 4)


def _check_budget(agent_name: str, output: str, budgets: dict[str, int]):
    """raise BudgetExceededError if output exceeds the configured token budget."""
    if agent_name not in budgets:
        return
    budget = budgets[agent_name]
    if budget <= 0:
        return  # 0 = unlimited
    used = _count_tokens(output)
    if used > budget:
        raise BudgetExceededError(agent_name, used, budget)


async def run_planning_pipeline(
    project_id: str,
    user_story: str,
    callbacks: PlanningCallbacks | None = None,
    config: dict[str, Any] | None = None,
) -> PlanningResult:
    """run the full planning pipeline and return artifacts + cost.

    config keys:
      stack_preference: str       override stack detection (default "auto")
      budgets: {agent: max_tokens}  0 = unlimited per agent
    """
    discover_agents()
    cb = callbacks or PlanningCallbacks()
    config = config or {}
    budgets: dict[str, int] = config.get("budgets", {})
    result = PlanningResult()

    state = PipelineState(
        project_id=project_id,
        project_path="",
        user_story=user_story,
        stack=config.get("stack_preference", "auto"),
    )

    await _emit(cb, PlanningEvent(
        type="pipeline_start",
        data={"project_id": project_id, "phases": PLANNING_PHASES},
    ))

    brief      = await _run_em(state, user_story, cb, budgets)
    result.artifacts["engineering_manager"] = brief

    po_output  = await _run_po(state, brief, cb, budgets)
    result.artifacts["product_owner"] = po_output

    arch_output = await _run_architect(state, po_output, config, cb, budgets)
    result.artifacts["architect"] = arch_output
    result.stack = state.stack

    pl_output  = await _run_project_lead(state, cb, budgets)
    result.artifacts["project_lead"] = pl_output

    hr_output  = await _run_hr(state, cb, budgets)
    result.artifacts["hr"] = hr_output
    result.spawned_agents = state.architecture.get("spawned_agents", [])

    # rough total — per-agent costs are tracked separately by the llm client
    total_tokens = sum(_count_tokens(v) for v in result.artifacts.values())
    result.total_cost = total_tokens * COST_PER_TOKEN
    result.token_usage = {k: _count_tokens(v) for k, v in result.artifacts.items()}

    await _emit(cb, PlanningEvent(
        type="pipeline_done",
        data={
            "total_cost":    result.total_cost,
            "stack":         result.stack,
            "spawned_agents": [a["name"] for a in result.spawned_agents],
        },
    ))

    return result


# ── phase runners ──────────────────────────────────────────────────────────────

async def _run_em(
    state: PipelineState,
    user_story: str,
    cb: PlanningCallbacks,
    budgets: dict[str, int],
) -> str:
    """em converses with the user up to MAX_ROUNDS times then produces a brief.

    the clarification_bus bridges the async wait here with the /api/po-answer http
    endpoint — when the user submits their answer, the bus resolves the future.
    """
    from llm.client import async_call_llm_tracked

    agent_name = "engineering_manager"
    await _emit(cb, PlanningEvent(type="agent_start", agent=agent_name,
                                   data={"label": PHASE_LABELS[agent_name]}))

    MAX_ROUNDS = 3
    conversation = ""
    current_input = user_story
    total_input_tokens = 0
    total_output_tokens = 0

    prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "engineering_manager.txt")
    with open(os.path.normpath(prompt_path), "r", encoding="utf-8") as f:
        system_prompt = f.read()

    for round_num in range(1, MAX_ROUNDS + 1):
        await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                       data={"content": f"Round {round_num}: analyzing the idea..."}))

        prompt_parts = []
        if conversation:
            prompt_parts.append(f"=== Conversation so far ===\n{conversation}")
        prompt_parts.append(f"=== Latest message ===\n{current_input}")
        prompt = "\n\n".join(prompt_parts)

        resp = await async_call_llm_tracked(system_prompt, prompt, max_tokens=512, model="mini")
        output = resp.text.strip()
        total_input_tokens += resp.input_tokens
        total_output_tokens += resp.output_tokens

        _check_budget(agent_name, output, budgets)

        conversation += f"\nEM: {output}\n"

        if "BRIEF_READY:" in output:
            # em is satisfied — extract and store the brief
            brief = output.split("BRIEF_READY:", 1)[1].strip()
            cost = total_input_tokens * 0.000002 + total_output_tokens * 0.000008
            await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                           data={"content": "Brief ready — handing off to Product Owner."}))
            await _emit(cb, PlanningEvent(type="agent_artifact", agent=agent_name,
                                           data={"artifact_type": "brief", "content": brief}))
            await _emit(cb, PlanningEvent(type="agent_done", agent=agent_name, data={
                "tokens_used": total_input_tokens + total_output_tokens,
                "cost_usd": cost,
            }))
            if not state.architecture:
                state.architecture = {}
            state.architecture["em_brief"] = brief
            state.architecture["em_conversation"] = conversation
            return brief

        # em has a question — send it to the frontend and wait for the user's answer
        await _emit(cb, PlanningEvent(
            type="em_question",
            agent=agent_name,
            data={"question": output, "round": round_num, "max_rounds": MAX_ROUNDS},
        ))
        await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                       data={"content": "Waiting for your response..."}))

        answer = await clarification_bus.wait_for_answer(state.project_id, timeout=300.0)

        # immediately clear the question banner before the next round starts
        await _emit(cb, PlanningEvent(type="clarification_received", agent=agent_name,
                                       data={"round": round_num}))
        await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                       data={"content": "Got it — thinking it through..."}))

        if answer.strip():
            conversation += f"User: {answer}\n"
            current_input = answer
        else:
            await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                           data={"content": "No response — proceeding with available info."}))
            break

    # max rounds reached — force a final brief from whatever we have
    await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                   data={"content": "Finalizing brief..."}))
    force_prompt = (
        f"{conversation}\n\n"
        "The conversation has reached its limit. Output BRIEF_READY: now with "
        "the best brief you can produce from what you have."
    )
    resp = await async_call_llm_tracked(system_prompt, force_prompt, max_tokens=400, model="mini")
    final = resp.text
    total_input_tokens += resp.input_tokens
    total_output_tokens += resp.output_tokens
    brief = final.split("BRIEF_READY:", 1)[-1].strip() if "BRIEF_READY:" in final else user_story

    if not state.architecture:
        state.architecture = {}
    state.architecture["em_brief"] = brief
    state.architecture["em_conversation"] = conversation

    total_tokens = total_input_tokens + total_output_tokens
    cost = total_input_tokens * 0.000002 + total_output_tokens * 0.000008
    await _emit(cb, PlanningEvent(type="agent_artifact", agent=agent_name,
                                   data={"artifact_type": "brief", "content": brief}))
    await _emit(cb, PlanningEvent(type="agent_done", agent=agent_name, data={
        "tokens_used": total_tokens,
        "cost_usd": cost,
    }))
    return brief


async def _run_po(
    state: PipelineState,
    brief: str,
    cb: PlanningCallbacks,
    budgets: dict[str, int],
) -> str:
    agent_name = "product_owner"
    await _emit(cb, PlanningEvent(type="agent_start", agent=agent_name,
                                   data={"label": PHASE_LABELS[agent_name]}))
    await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                   data={"content": "Writing structured requirements..."}))

    agent = AgentRegistry.create(agent_name)
    task = {"description": brief, "task": brief}
    output = await agent.run(task, {}, state)

    _check_budget(agent_name, output, budgets)
    artifact, summary = _split_monitor_summary(output)

    await _emit(cb, PlanningEvent(type="agent_artifact", agent=agent_name,
                                   data={"artifact_type": "requirements", "content": artifact}))
    if summary:
        await _emit(cb, PlanningEvent(type="agent_summary", agent=agent_name,
                                       data={"content": summary}))
    await _emit(cb, PlanningEvent(type="agent_done", agent=agent_name, data={
        "tokens_used": agent.last_input_tokens + agent.last_output_tokens,
        "cost_usd": agent.last_cost_usd,
    }))
    return artifact


async def _run_architect(
    state: PipelineState,
    po_output: str,
    config: dict[str, Any],
    cb: PlanningCallbacks,
    budgets: dict[str, int],
) -> str:
    agent_name = "architect"
    await _emit(cb, PlanningEvent(type="agent_start", agent=agent_name,
                                   data={"label": PHASE_LABELS[agent_name]}))

    suggested_stack = config.get("stack_preference", "auto")
    if suggested_stack == "auto":
        await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                       data={"content": "Detecting project type..."}))
        suggested_stack = await auto_detect_stack(state.user_story)

    await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                   data={"content": f"Designing architecture for {suggested_stack}..."}))
    state.stack = suggested_stack

    agent = AgentRegistry.create(agent_name)
    task = {"description": po_output, "task": po_output, "stack": suggested_stack}
    output = await agent.run(task, {}, state)

    _check_budget(agent_name, output, budgets)
    artifact, summary = _split_monitor_summary(output)

    await _emit(cb, PlanningEvent(type="agent_artifact", agent=agent_name,
                                   data={"artifact_type": "architecture", "content": artifact}))
    if summary:
        await _emit(cb, PlanningEvent(type="agent_summary", agent=agent_name,
                                       data={"content": summary}))
    await _emit(cb, PlanningEvent(type="agent_done", agent=agent_name, data={
        "stack": state.stack,
        "tokens_used": agent.last_input_tokens + agent.last_output_tokens,
        "cost_usd": agent.last_cost_usd,
    }))
    return artifact


async def _run_project_lead(
    state: PipelineState,
    cb: PlanningCallbacks,
    budgets: dict[str, int],
) -> str:
    agent_name = "project_lead"
    await _emit(cb, PlanningEvent(type="agent_start", agent=agent_name,
                                   data={"label": PHASE_LABELS[agent_name]}))
    await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                   data={"content": "Planning sprints and identifying critical path..."}))

    agent = AgentRegistry.create(agent_name)

    # always include qa_dev even if the stack profile doesn't list it
    profile = get_profile(state.stack or "flask_react")
    dev_agents = list(profile.default_agents)
    if "qa_dev" not in dev_agents:
        dev_agents.append("qa_dev")

    task = {
        "description": "Create the sprint plan based on the architecture.",
        "task": "Create the sprint plan.",
        "mode": "plan",
        "available_agents": dev_agents,
    }
    output = await agent.run(task, {}, state)

    _check_budget(agent_name, output, budgets)
    artifact, summary = _split_monitor_summary(output)

    await _emit(cb, PlanningEvent(type="agent_artifact", agent=agent_name,
                                   data={"artifact_type": "sprint_plan", "content": artifact}))
    if summary:
        await _emit(cb, PlanningEvent(type="agent_summary", agent=agent_name,
                                       data={"content": summary}))
    await _emit(cb, PlanningEvent(type="agent_done", agent=agent_name, data={
        "tokens_used": agent.last_input_tokens + agent.last_output_tokens,
        "cost_usd": agent.last_cost_usd,
    }))
    return artifact


async def _run_hr(
    state: PipelineState,
    cb: PlanningCallbacks,
    budgets: dict[str, int],
) -> str:
    """hr reads the sprint plan and emits one agent_spawned event per dev persona."""
    import traceback as _tb

    agent_name = "hr"
    await _emit(cb, PlanningEvent(type="agent_start", agent=agent_name,
                                   data={"label": PHASE_LABELS[agent_name]}))
    await _emit(cb, PlanningEvent(type="agent_thinking", agent=agent_name,
                                   data={"content": "Reviewing sprint plan and assembling team..."}))

    agent = AgentRegistry.create(agent_name)

    sprint_plan = ""
    arch_summary = ""
    if state.architecture:
        import json
        sprint_plan = json.dumps(state.sprint_plan, indent=2) if state.sprint_plan else ""
        arch_summary = (state.architecture.get("ARCHITECTURE_MD", "") or
                        state.architecture.get("raw_output", ""))[:3000]

    task = {
        "description": "Spawn the right dev agents for this sprint plan.",
        "task": "Spawn dev agents.",
        "sprint_plan": sprint_plan,
        "arch_summary": arch_summary,
    }

    try:
        output = await agent.run(task, {}, state)
    except Exception as e:
        log.error("HR agent failed: %s", e, exc_info=True)
        raise

    _check_budget(agent_name, output, budgets)
    artifact, summary = _split_monitor_summary(output)
    spawned = state.architecture.get("spawned_agents", [])

    # emit one event per spawned agent so the frontend can show them in the war room
    for spawned_agent in spawned:
        await _emit(cb, PlanningEvent(
            type="agent_spawned",
            agent=agent_name,
            data={
                "spawned_name":      spawned_agent["name"],
                "spawned_archetype": spawned_agent["archetype"],
                "system_prompt":     spawned_agent.get("system_prompt", ""),
            },
        ))

    await _emit(cb, PlanningEvent(type="agent_artifact", agent=agent_name,
                                   data={"artifact_type": "team_roster", "content": artifact}))
    if summary:
        await _emit(cb, PlanningEvent(type="agent_summary", agent=agent_name,
                                       data={"content": summary}))
    await _emit(cb, PlanningEvent(type="agent_done", agent=agent_name, data={
        "spawned_count": len(spawned),
        "tokens_used": agent.last_input_tokens + agent.last_output_tokens,
        "cost_usd": agent.last_cost_usd,
    }))
    return artifact
