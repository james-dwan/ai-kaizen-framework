"""Smoke tests for the AI Jidoka Framework core loop."""

from __future__ import annotations

from typing import List, TypedDict

import pytest

from kaizen import (
    AbnormalityRule,
    KaizenConfig,
    KaizenGraphBuilder,
    KanbanTicket,
    LocalKanbanBoard,
    ReflectionAgent,
    RunLog,
    Severity,
)
from kaizen.kaizen_graph import KaizenState


def make_config(tmp_path, rules=None, stop_on="high", sandbox=False) -> KaizenConfig:
    config = KaizenConfig.default()
    config.data["process"]["name"] = "test-process"
    config.data["rules"] = rules or []
    config.data["jidoka"]["stop_on_severity"] = stop_on
    config.data["sandbox"] = sandbox
    config.data["kanban"]["board_path"] = str(tmp_path / "board.json")
    return config


class State(KaizenState, TypedDict, total=False):
    value: int
    doubled: int


def double(state):
    return {"doubled": state["value"] * 2}


def build(tmp_path, config):
    builder = KaizenGraphBuilder(State, config, runlog=RunLog(str(tmp_path / "log.jsonl")))
    builder.add_node("double", double)
    builder.set_entry_point("double")
    builder.set_finish_point("double")
    return builder.compile()


def test_clean_run_completes(tmp_path):
    graph = build(tmp_path, make_config(tmp_path))
    result = graph.invoke({"value": 3})
    assert result["doubled"] == 6
    assert result["kaizen_stopped"] is False
    assert result["kaizen_exceptions"] == []


def test_rule_triggers_stop_and_ticket(tmp_path):
    rules = [{
        "name": "too-big",
        "condition": "state.get('doubled', 0) > 5",
        "severity": "high",
        "sqdip_category": "quality",
        "description": "Doubled value exceeded the limit",
    }]
    config = make_config(tmp_path, rules=rules)
    graph = build(tmp_path, config)
    result = graph.invoke({"value": 10})
    assert result["kaizen_stopped"] is True
    assert "too-big" in result["kaizen_stop_reason"]
    board = LocalKanbanBoard(config.data["kanban"]["board_path"])
    tickets = board.list_tickets(bucket="Exceptions")
    assert len(tickets) == 1
    assert "5 Whys" in tickets[0].description


def test_low_severity_does_not_stop(tmp_path):
    rules = [{"name": "warn", "condition": "True", "severity": "low"}]
    graph = build(tmp_path, make_config(tmp_path, rules=rules))
    result = graph.invoke({"value": 1})
    assert result["kaizen_stopped"] is False
    assert len(result["kaizen_exceptions"]) == 1


def test_uncaught_error_becomes_high_severity_stop(tmp_path):
    config = make_config(tmp_path)
    builder = KaizenGraphBuilder(State, config, runlog=RunLog(str(tmp_path / "log.jsonl")))
    builder.add_node("boom", lambda state: 1 / 0)
    builder.set_entry_point("boom")
    builder.set_finish_point("boom")
    result = builder.compile().invoke({"value": 1})
    assert result["kaizen_stopped"] is True
    assert result["kaizen_exceptions"][0]["rule"] == "uncaught-exception"
    assert result["kaizen_exceptions"][0]["severity"] == Severity.HIGH.value


def test_sandbox_creates_no_tickets(tmp_path):
    rules = [{"name": "always", "condition": "True", "severity": "high"}]
    config = make_config(tmp_path, rules=rules, sandbox=True)
    graph = build(tmp_path, config)
    result = graph.invoke({"value": 1})
    assert result["kaizen_stopped"] is True  # the stop still happens in sandbox
    assert LocalKanbanBoard(config.data["kanban"]["board_path"]).list_tickets() == []


def test_config_save_bumps_version_and_archives(tmp_path):
    path = tmp_path / "config.yaml"
    config = KaizenConfig.default()
    config.save(str(path))
    assert config.data["version"] == 1
    config.save()
    assert config.data["version"] == 2
    archived = list((tmp_path / "config_history").glob("*.yaml"))
    assert len(archived) == 1


def test_reflection_computes_sqdip_and_posts(tmp_path):
    rules = [{"name": "always", "condition": "True", "severity": "low"}]
    config = make_config(tmp_path, rules=rules)
    runlog = RunLog(str(tmp_path / "log.jsonl"))
    builder = KaizenGraphBuilder(State, config, runlog=runlog)
    builder.add_node("double", double)
    builder.set_entry_point("double")
    builder.set_finish_point("double")
    graph = builder.compile()
    graph.invoke({"value": 1})
    graph.invoke({"value": 2})

    board = LocalKanbanBoard(config.data["kanban"]["board_path"])
    agent = ReflectionAgent(config, runlog, board=board, reports_dir=str(tmp_path / "reports"))
    summary = agent.daily_reflection()
    assert summary.sqdip.runs_started == 2
    assert summary.sqdip.productivity_runs_completed == 2
    assert summary.sqdip.quality_exception_rate == 100.0
    assert summary.ticket_id is not None
    assert (tmp_path / "reports" / f"kaizen-{summary.day.isoformat()}.md").exists()


def test_callable_rule_condition(tmp_path):
    rule = AbnormalityRule(name="fn", condition=lambda s: s["value"] < 0)
    assert rule.check({"value": -1}) is True
    assert rule.check({"value": 1}) is False


# -- Sensei Agent ----------------------------------------------------------

from kaizen import FiveWhysAnalysis, SenseiAgent  # noqa: E402


def test_sensei_questions_blame_and_weak_countermeasures(tmp_path):
    sensei = SenseiAgent(make_config(tmp_path))
    analysis = FiveWhysAnalysis(
        problem="Invoice was wrong",
        whys=["The consultant was careless with the timesheet"],
        root_cause="Human error",
        countermeasure="Remind everyone to be more careful",
    )
    review = sensei.review(analysis)
    assert review.ready is False
    text = " ".join(review.questions)
    assert "process" in text          # blame -> process question
    assert "poka-yoke" in text
    assert "Reminders and training fade" in text


def test_sensei_accepts_solid_analysis(tmp_path):
    sensei = SenseiAgent(make_config(tmp_path))
    analysis = FiveWhysAnalysis(
        problem="Invoice INV-2026-07 overstated Contoso hours by 12.5 on 2026-07-15",
        whys=[
            "The timesheet export duplicated the final week",
            "The export job ran twice on the cutoff day",
            "The scheduler retries on timeout without checking for a prior success",
            "The job has no idempotency key",
            "The integration was built before the retry policy was introduced",
        ],
        root_cause="Export job is not idempotent under the current retry policy",
        countermeasure="Add an idempotency key per period; verify by replaying July with forced retries",
    )
    review = sensei.review(analysis)
    assert review.ready is True
    assert "ready to act on" in review.to_markdown()


def test_sensei_coaches_open_tickets(tmp_path):
    rules = [{"name": "always", "condition": "True", "severity": "high",
              "description": "Something abnormal happened in the run"}]
    config = make_config(tmp_path, rules=rules)
    graph = build(tmp_path, config)
    graph.invoke({"value": 1})

    board = LocalKanbanBoard(config.data["kanban"]["board_path"])
    sensei = SenseiAgent(config)
    assert sensei.coach_open_exceptions(board) == 1
    ticket = board.list_tickets(bucket="Exceptions")[0]
    assert "**Sensei" in ticket.description
    # Second pass is idempotent — already-coached tickets are skipped.
    assert sensei.coach_open_exceptions(board) == 0
