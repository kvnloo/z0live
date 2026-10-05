from pathlib import Path

from z0live.fixtures import load_corpus
from z0live.replay import replay_corpus


def test_core_fixture_corpus_has_20_passing_cases():
    corpus = load_corpus(
        Path(__file__).parents[1]
        / "fixtures"
        / "core-v1.json"
    )
    assert len(corpus.cases) == 20
    result = replay_corpus(corpus)
    assert result.passed, [
        c for c in result.cases
        if not c.passed
    ]
    assert len(result.fixture_sha256) == 64


def test_stress_fixture_corpus_has_120_passing_cases():
    corpus = load_corpus(
        Path(__file__).parents[1]
        / "fixtures"
        / "stress-v1.json"
    )
    assert len(corpus.cases) == 120
    result = replay_corpus(corpus)
    assert result.passed, [
        c for c in result.cases
        if not c.passed
    ]
    assert len(result.fixture_sha256) == 64
