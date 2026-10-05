from pathlib import Path

from z0live.speculation_replay import (
    load_speculation_corpus,
    replay_speculation_corpus,
)


def test_speculation_fixture_corpus_has_180_passing_cases_and_zero_mutations():
    corpus = load_speculation_corpus(
        Path(__file__).parents[1] / "fixtures" / "speculation-v1.json"
    )
    assert len(corpus.cases) == 180
    result = replay_speculation_corpus(corpus)
    assert result["passed"], [
        case for case in result["cases"] if not case["passed"]
    ][:5]
    assert result["cases_passed"] == 180
    assert result["unauthorized_mutations"] == 0
    assert len(result["fixture_sha256"]) == 64
