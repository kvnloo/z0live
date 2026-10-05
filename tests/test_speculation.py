from z0live.speculation import (
    SpeculationAction,
    SpeculationGate,
    SpeculationMeasurement,
    evaluate_speculation,
)


def test_partial_never_grants_mutation_authority():
    gate = SpeculationGate(min_chars=4)
    decision = gate.partial("t1", "run the tests")
    assert decision.action == SpeculationAction.PREWARM
    assert decision.mutation_allowed is False

    final = gate.final("t1", "run the tests please")
    assert final[-1].action == SpeculationAction.AUTHORITY
    assert final[-1].mutation_allowed is True


def test_divergent_final_cancels_prepared_work_before_authority():
    gate = SpeculationGate(min_chars=4)
    gate.partial("t1", "run the tests")
    actions = gate.final("t1", "actually open the docs")
    assert [x.action for x in actions] == [
        SpeculationAction.CANCEL,
        SpeculationAction.AUTHORITY,
    ]
    assert actions[0].mutation_allowed is False


def test_speculation_gate_requires_measured_gain_and_zero_mutations():
    good = [
        SpeculationMeasurement("a", 100, 300, 220, True, 0, 200),
        SpeculationMeasurement("b", 100, 320, 250, True, 0, 220),
        SpeculationMeasurement("c", 100, 300, 230, False, 0, 180),
    ]
    out = evaluate_speculation(good)
    assert out["p50_useful_gain_ms"] == 75
    assert out["speculative_mutations"] == 0
    assert out["pass"] is True

    bad = [
        SpeculationMeasurement("a", 100, 300, 220, True, 1, 200),
    ]
    assert evaluate_speculation(bad)["pass"] is False
