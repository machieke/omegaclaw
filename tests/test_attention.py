import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import attention  # noqa: E402


def _premises_chain(n: int):
    items = []
    for i in range(n):
        items.append(f"((--> n{i} n{i+1}) (stv 1.0 0.9))")
    return items


def test_conductance_monotonicity():
    base = attention.conductance_score(0.2, 0.2, 0.2, 0.5)
    higher_r = attention.conductance_score(0.8, 0.2, 0.2, 0.5)
    higher_h = attention.conductance_score(0.2, 0.8, 0.2, 0.5)
    higher_ig = attention.conductance_score(0.2, 0.2, 0.8, 0.5)
    higher_cost = attention.conductance_score(0.2, 0.2, 0.2, 0.9)

    assert higher_r > base
    assert higher_h > base
    assert higher_ig > base
    assert higher_cost < base


def test_scheduler_is_deterministic_for_same_input():
    payload = {
        "engine": "nal",
        "premises": _premises_chain(5),
        "budgets": {
            "candidate_budget": 32,
            "inference_budget": 3,
            "revision_budget": 2,
            "attention_time_budget_ms": 200,
        },
    }

    attention.reset_state()
    first = attention.run_attention_cycle(payload)

    attention.reset_state()
    second = attention.run_attention_cycle(payload)

    assert first["inference_queue"] == second["inference_queue"]
    assert first["revision_queue"] == second["revision_queue"]
    assert first["all_candidates"] == second["all_candidates"]


def test_dual_queues_are_disjoint_and_respect_budgets():
    payload = {
        "engine": "nal",
        "premises": _premises_chain(8),
        "budgets": {
            "candidate_budget": 64,
            "inference_budget": 3,
            "revision_budget": 2,
            "attention_time_budget_ms": 200,
        },
    }

    attention.reset_state()
    result = attention.run_attention_cycle(payload)

    inf = result["inference_queue"]
    rev = result["revision_queue"]
    inf_ids = {item["pair_id"] for item in inf}
    rev_ids = {item["pair_id"] for item in rev}

    assert len(inf) <= 3
    assert len(rev) <= 2
    assert inf_ids.isdisjoint(rev_ids)


def test_contradictions_route_to_revision_queue():
    payload = {
        "engine": "nal",
        "premises": [
            "((--> cat mammal) (stv 1.0 0.9))",
            "((--> cat mammal) (stv 0.0 0.9))",
            "((--> dog mammal) (stv 1.0 0.9))",
            "((--> mammal animal) (stv 1.0 0.9))",
        ],
        "budgets": {
            "candidate_budget": 32,
            "inference_budget": 2,
            "revision_budget": 1,
            "attention_time_budget_ms": 200,
        },
    }

    attention.reset_state()
    result = attention.run_attention_cycle(payload)

    assert result["revision_queue"]
    top_rev = result["revision_queue"][0]
    assert top_rev["contradiction"] > 0.0
    assert top_rev["pair_id"] == "p1|p2"


def test_candidate_and_hebbian_caps_are_enforced():
    payload = {
        "engine": "nal",
        "premises": _premises_chain(12),
        "budgets": {
            "candidate_budget": 5,
            "inference_budget": 5,
            "revision_budget": 5,
            "max_hebbian_edges": 2,
            "attention_time_budget_ms": 200,
        },
    }

    attention.reset_state()
    result = attention.run_attention_cycle(payload)

    assert result["metrics"]["candidates_generated"] <= 5
    assert result["metrics"]["hebbian_edges"] <= 2


def test_eval_plan_returns_metta_tuple():
    payload = {
        "engine": "nal",
        "premises": _premises_chain(4),
        "budgets": {"inference_budget": 2, "revision_budget": 1},
    }
    attention.reset_state()
    plan = attention.bounded_metta_eval_plan(payload)
    assert plan.startswith("(")
    assert plan.endswith(")")


def test_runtime_wrapper_respects_disabled_flag():
    payload = {"engine": "nal", "premises": _premises_chain(3)}
    plan = attention.bounded_metta_eval_plan_runtime(
        payload,
        False,
        16,
        2,
        1,
        50,
        0.4,
        0.25,
        0.25,
        0.2,
        0.0,
        0.05,
        0.001,
        64,
        0.5,
        1.0,
        3.0,
        1.0,
        0.15,
        4.0,
        1,
        1,
    )
    assert plan == "()"
