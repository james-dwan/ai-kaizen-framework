"""Root cause investigations as flows.

Every exception ticket can spawn its own long-running LangGraph flow that walks
the team through a structured investigation — effectively a living A3:

    frame_problem -> collect_data -> brainstorm_causes -> five_whys
        -> sensei_gate -> design_countermeasure -> verify -> standardize
                 |____________(needs work)___________^

Design principles:

- **Human gates are non-optional.** Every stage pauses with a LangGraph
  ``interrupt()`` and cannot advance until a human responds. Without that, the
  flow degrades into automation-with-oversight — the opposite of the point.
- **The Sensei is the Jidoka layer on the thinking.** A weak 5 Whys stops the
  investigation the same way bad data stops an invoice: the ``sensei_gate``
  routes back to ``five_whys`` with socratic questions until the analysis is
  sound (or the round limit forces an explicit human override).
- **The ticket and the flow are two views of the same thing.** The Kanban
  ticket ID is the checkpointer thread ID; on completion the ticket is updated
  with the full A3 and closed.
- **Specialist agents live in the nodes.** Each stage is the natural home for
  a specialist (problem-statement writer, Pareto analyst, fishbone
  facilitator, pilot designer, communicator). Today they are light heuristics
  plus optional LLM drafts; the graph shape is where the roster will grow.

Because investigations span days, pass a persistent checkpointer (e.g.
``langgraph-checkpoint-sqlite``) in production; ``MemorySaver`` is fine for a
single interactive session.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.types import interrupt

from .config import KaizenConfig
from .exception_handler import FiveWhysAnalysis
from .kanban_integration import KanbanBoard
from .runlog import RunLog
from .sensei_agent import SenseiAgent

#: Fishbone categories adapted for knowledge/agentic work (classic 6M variants
#: assume a factory floor).
FISHBONE_CATEGORIES = ["People", "Process", "Tools & Automation", "Data", "Environment"]

MAX_SENSEI_ROUNDS = 3


class InvestigationState(TypedDict, total=False):
    # identity
    ticket_id: str
    rule: str
    summary: str
    # stage outputs (the A3, section by section)
    problem_statement: str
    pareto: Dict[str, int]
    recurrence: int
    observations: str
    fishbone: Dict[str, List[str]]
    whys: List[str]
    root_cause: str
    sensei_questions: List[str]
    sensei_rounds: int
    sensei_override: bool
    countermeasure: str
    pilot_plan: str
    verified: bool
    verification_notes: str
    status: str  # in_progress | standardized | abandoned


class InvestigationGraphBuilder:
    """Builds the investigation flow for one process."""

    def __init__(
        self,
        config: KaizenConfig,
        board: KanbanBoard,
        runlog: Optional[RunLog] = None,
        sensei: Optional[SenseiAgent] = None,
        llm: Any = None,
    ):
        self.config = config
        self.board = board
        self.runlog = runlog or RunLog()
        self.sensei = sensei or SenseiAgent(config, llm=llm)
        self.llm = llm

    # ------------------------------------------------------------------
    # Nodes — each ends in an interrupt(); the flow cannot advance alone.
    # ------------------------------------------------------------------

    def frame_problem(self, state: InvestigationState) -> Dict[str, Any]:
        draft = self._draft(
            "problem_statement",
            fallback=(f"{state.get('summary', 'An abnormality occurred')} "
                      f"(rule: {state.get('rule', 'unknown')}). "
                      "State what happened, where, when, and the gap versus the standard."),
            context=state,
        )
        answer = interrupt({
            "stage": "frame_problem",
            "instruction": ("Draft problem statement below. Reply with an improved statement, "
                            "or 'ok' to accept. A good statement is specific, measurable, and "
                            "blame-free."),
            "draft": draft,
        })
        statement = draft if _accepted(answer) else str(answer).strip()
        self._log(state, "frame_problem")
        return {"problem_statement": statement, "status": "in_progress"}

    def collect_data(self, state: InvestigationState) -> Dict[str, Any]:
        # Pareto of exception rules across the whole run log: is this the
        # biggest problem, or just the loudest one today?
        exceptions = [e for e in self.runlog.events() if e.get("type") == "exception"]
        pareto = dict(Counter(e.get("rule", "unknown") for e in exceptions).most_common())
        recurrence = pareto.get(state.get("rule", ""), 0)
        answer = interrupt({
            "stage": "collect_data",
            "instruction": ("Go and see. Review the Pareto below — does the data support "
                            "focusing here? Reply with what you observed at the actual "
                            "process (or 'ok' if the Pareto speaks for itself)."),
            "pareto": pareto,
            "recurrence_of_this_rule": recurrence,
        })
        observations = "" if _accepted(answer) else str(answer).strip()
        self._log(state, "collect_data")
        return {"pareto": pareto, "recurrence": recurrence, "observations": observations}

    def brainstorm_causes(self, state: InvestigationState) -> Dict[str, Any]:
        drafted = self._draft_fishbone(state)
        answer = interrupt({
            "stage": "brainstorm_causes",
            "instruction": ("Fishbone brainstorm. Candidate causes per category below. "
                            "Add your own as lines of 'Category: cause', or 'ok' to accept."),
            "categories": FISHBONE_CATEGORIES,
            "candidates": drafted,
        })
        fishbone = dict(drafted)
        if not _accepted(answer):
            for line in str(answer).splitlines():
                if ":" in line:
                    category, cause = line.split(":", 1)
                    fishbone.setdefault(category.strip(), []).append(cause.strip())
        self._log(state, "brainstorm_causes")
        return {"fishbone": fishbone}

    def five_whys(self, state: InvestigationState) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stage": "five_whys",
            "instruction": ("Work the causal chain together. Reply with one 'why' per line, "
                            "most likely branch from the fishbone first; the LAST line is "
                            "your root cause."),
            "problem_statement": state.get("problem_statement", ""),
            "fishbone": state.get("fishbone", {}),
        }
        if state.get("sensei_questions"):
            payload["sensei_questions"] = state["sensei_questions"]
            payload["previous_chain"] = state.get("whys", [])
        answer = str(interrupt(payload)).strip()
        lines = [line.strip() for line in answer.splitlines() if line.strip()]
        whys, root_cause = (lines[:-1], lines[-1]) if lines else ([], "")
        self._log(state, "five_whys")
        return {"whys": whys, "root_cause": root_cause}

    def sensei_gate(self, state: InvestigationState) -> Dict[str, Any]:
        analysis = FiveWhysAnalysis(
            problem=state.get("problem_statement", ""),
            whys=state.get("whys", []),
            root_cause=state.get("root_cause", ""),
            countermeasure=state.get("countermeasure", ""),
        )
        review = self.sensei.review(analysis, require_countermeasure=False)
        rounds = state.get("sensei_rounds", 0) + 1
        update: Dict[str, Any] = {"sensei_rounds": rounds, "sensei_questions": review.questions}
        if review.ready:
            self._log(state, "sensei_gate", result="ready")
            return update
        if rounds >= MAX_SENSEI_ROUNDS:
            # The sensei never blocks forever, but overriding is an explicit,
            # recorded human decision — not a silent fall-through.
            answer = interrupt({
                "stage": "sensei_override",
                "instruction": (f"The sensei still has questions after {rounds} rounds. "
                                "Reply 'proceed' to continue anyway (recorded as an override) "
                                "or anything else to keep working the analysis."),
                "questions": review.questions,
            })
            if str(answer).strip().lower() in ("proceed", "override", "yes", "y"):
                update["sensei_override"] = True
                self._log(state, "sensei_gate", result="human_override")
                return update
        self._log(state, "sensei_gate", result="needs_work")
        return update

    def route_after_sensei(self, state: InvestigationState) -> str:
        if not state.get("sensei_questions") or state.get("sensei_override"):
            return "design_countermeasure"
        return "five_whys"

    def design_countermeasure(self, state: InvestigationState) -> Dict[str, Any]:
        draft = self._draft(
            "countermeasure",
            fallback=("Change the process/tool/standard so the root cause cannot recur "
                      "(prefer poka-yoke over reminders)."),
            context=state,
        )
        answer = interrupt({
            "stage": "design_countermeasure",
            "instruction": ("Agree the countermeasure and pilot. Reply with two lines:\n"
                            "  countermeasure: <what will change>\n"
                            "  pilot: <smallest safe experiment — sandbox mode is your friend>"),
            "root_cause": state.get("root_cause", ""),
            "draft": draft,
        })
        countermeasure, pilot = _parse_labeled(str(answer), "countermeasure", "pilot")
        self._log(state, "design_countermeasure")
        return {"countermeasure": countermeasure or draft,
                "pilot_plan": pilot or "Trial the change in sandbox mode and compare SQDIP."}

    def verify(self, state: InvestigationState) -> Dict[str, Any]:
        answer = interrupt({
            "stage": "verify",
            "instruction": ("Run the pilot, then come back. Did the countermeasure work? "
                            "Reply 'yes: <evidence>' or 'no: <what you saw>'."),
            "pilot_plan": state.get("pilot_plan", ""),
        })
        text = str(answer).strip()
        verified = text.lower().startswith(("yes", "y:", "y "))
        notes = text.split(":", 1)[1].strip() if ":" in text else text
        self._log(state, "verify", verified=verified)
        return {"verified": verified, "verification_notes": notes}

    def standardize(self, state: InvestigationState) -> Dict[str, Any]:
        status = "standardized" if state.get("verified") else "in_progress"
        a3 = a3_markdown(state)
        ticket_id = state.get("ticket_id")
        if ticket_id and not self.config.sandbox:
            self.board.update_ticket(
                ticket_id,
                description=a3,
                status="done" if state.get("verified") else "in_progress",
            )
        self.runlog.record(
            "investigation_completed",
            ticket_id=ticket_id,
            rule=state.get("rule"),
            status=status,
            sensei_rounds=state.get("sensei_rounds", 0),
            sensei_override=state.get("sensei_override", False),
            verified=state.get("verified", False),
        )
        return {"status": status}

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    def build(self, checkpointer: Any = None):
        graph = StateGraph(InvestigationState)
        graph.add_node("frame_problem", self.frame_problem)
        graph.add_node("collect_data", self.collect_data)
        graph.add_node("brainstorm_causes", self.brainstorm_causes)
        graph.add_node("five_whys", self.five_whys)
        graph.add_node("sensei_gate", self.sensei_gate)
        graph.add_node("design_countermeasure", self.design_countermeasure)
        graph.add_node("verify", self.verify)
        graph.add_node("standardize", self.standardize)

        graph.set_entry_point("frame_problem")
        graph.add_edge("frame_problem", "collect_data")
        graph.add_edge("collect_data", "brainstorm_causes")
        graph.add_edge("brainstorm_causes", "five_whys")
        graph.add_edge("five_whys", "sensei_gate")
        graph.add_conditional_edges("sensei_gate", self.route_after_sensei,
                                    {"design_countermeasure": "design_countermeasure",
                                     "five_whys": "five_whys"})
        graph.add_edge("design_countermeasure", "verify")
        graph.add_edge("verify", "standardize")
        graph.add_edge("standardize", END)

        if checkpointer is None:
            from langgraph.checkpoint.memory import MemorySaver

            checkpointer = MemorySaver()
        return graph.compile(checkpointer=checkpointer)

    def start_input(self, ticket_id: str) -> InvestigationState:
        """Initial state for an investigation spawned from an exception ticket."""
        for ticket in self.board.list_tickets():
            if ticket.id == ticket_id:
                self.runlog.record("investigation_started", ticket_id=ticket_id)
                return {
                    "ticket_id": ticket_id,
                    "summary": ticket.title,
                    "rule": _rule_from_title(ticket.title),
                }
        raise KeyError(f"No ticket with id {ticket_id!r} on the board.")

    # ------------------------------------------------------------------
    # Drafting helpers (where specialist agents will plug in)
    # ------------------------------------------------------------------

    def _draft(self, kind: str, fallback: str, context: InvestigationState) -> str:
        if self.llm is None:
            return fallback
        prompt = self.config.prompts.get(f"draft_{kind}", "").strip() or (
            f"Draft a {kind.replace('_', ' ')} for this Lean investigation. Context:\n"
            f"{_context_markdown(context)}\nRespond with the draft only — one short paragraph."
        )
        try:
            response = self.llm.invoke(prompt)
            return getattr(response, "content", str(response)).strip() or fallback
        except Exception:
            return fallback

    def _draft_fishbone(self, state: InvestigationState) -> Dict[str, List[str]]:
        drafted: Dict[str, List[str]] = {c: [] for c in FISHBONE_CATEGORIES}
        if self.llm is None:
            return drafted
        prompt = (
            f"Fishbone brainstorm for: {state.get('problem_statement', '')}\n"
            f"Observations: {state.get('observations', 'none')}\n"
            f"Categories: {', '.join(FISHBONE_CATEGORIES)}\n"
            "Respond ONLY with lines of 'Category: candidate cause' (max 2 per category)."
        )
        try:
            response = self.llm.invoke(prompt)
            for line in getattr(response, "content", str(response)).splitlines():
                if ":" in line:
                    category, cause = line.split(":", 1)
                    category = category.strip().lstrip("-* ")
                    if category in drafted:
                        drafted[category].append(cause.strip())
        except Exception:
            pass
        return drafted

    def _log(self, state: InvestigationState, stage: str, **extra: Any) -> None:
        self.runlog.record("investigation_stage", ticket_id=state.get("ticket_id"),
                           stage=stage, **extra)


# ----------------------------------------------------------------------
# Utilities
# ----------------------------------------------------------------------

def a3_markdown(state: InvestigationState) -> str:
    """Render the investigation state as an A3-style document."""
    fishbone_lines = []
    for category, causes in (state.get("fishbone") or {}).items():
        if causes:
            fishbone_lines.append(f"- **{category}:** " + "; ".join(causes))
    whys_lines = [f"{i + 1}. {why}" for i, why in enumerate(state.get("whys", []))]
    pareto = state.get("pareto") or {}
    pareto_lines = [f"| {rule} | {count} |" for rule, count in pareto.items()]

    parts = [
        f"# A3 — {state.get('summary', 'Investigation')}",
        "",
        "## 1. Problem statement",
        state.get("problem_statement", "_pending_"),
        "",
        "## 2. Data (Pareto of exception rules)",
        "| Rule | Count |", "|---|---|", *pareto_lines,
        f"\nObservations: {state.get('observations') or '_none recorded_'}",
        "",
        "## 3. Cause brainstorm (fishbone)",
        *(fishbone_lines or ["_none recorded_"]),
        "",
        "## 4. Causal chain (5 Whys)",
        *(whys_lines or ["_pending_"]),
        f"\n**Root cause:** {state.get('root_cause', '_pending_')}",
        "",
        "## 5. Countermeasure & pilot",
        f"**Countermeasure:** {state.get('countermeasure', '_pending_')}",
        f"**Pilot:** {state.get('pilot_plan', '_pending_')}",
        "",
        "## 6. Verification",
        f"**Verified:** {'yes' if state.get('verified') else 'no'} — "
        f"{state.get('verification_notes', '')}",
        "",
        f"_Sensei rounds: {state.get('sensei_rounds', 0)}"
        + (" (human override recorded)_" if state.get("sensei_override") else "_"),
    ]
    return "\n".join(parts)


def _accepted(answer: Any) -> bool:
    return str(answer).strip().lower() in ("", "ok", "okay", "accept", "yes", "y")


def _parse_labeled(text: str, *labels: str) -> List[str]:
    values = {label: "" for label in labels}
    for line in text.splitlines():
        for label in labels:
            prefix = f"{label}:"
            if line.strip().lower().startswith(prefix):
                values[label] = line.strip()[len(prefix):].strip()
    if not any(values.values()) and text.strip() and len(labels) >= 1:
        values[labels[0]] = text.strip()  # single unlabeled answer = first field
    return [values[label] for label in labels]


def _rule_from_title(title: str) -> str:
    # Exception tickets are titled "[SEVERITY] rule-name: summary"
    if "]" in title and ":" in title:
        return title.split("]", 1)[1].split(":", 1)[0].strip()
    return ""


def _context_markdown(state: InvestigationState) -> str:
    keys = ("summary", "problem_statement", "observations", "root_cause")
    return "\n".join(f"- {key}: {state[key]}" for key in keys if state.get(key))
