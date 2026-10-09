from pathlib import Path

from email_agent.retrieval_eval import recall_at_k, reciprocal_rank, run_eval


def test_metrics():
    assert recall_at_k([1, 2, 3], {3, 9}, k=3) == 0.5
    assert recall_at_k([1, 2, 3], {3}, k=2) == 0.0
    assert reciprocal_rank([5, 7, 3], {3}) == 1 / 3
    assert reciprocal_rank([5], {3}) == 0.0


async def test_example_golden_set_runs_on_fixture_mailbox(ingested):
    example = Path(__file__).parents[1] / "evals" / "retrieval_golden.example.yaml"
    scores, warnings = await run_eval(ingested.id, example, k=5)
    assert warnings == []
    assert set(scores) == {"bm25", "vector", "hybrid (rrf)", "hybrid + rerank"}
    assert scores["hybrid + rerank"].recall == 1.0
