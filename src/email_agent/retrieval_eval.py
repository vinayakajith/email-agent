"""Retrieval eval: recall@k and MRR per search mode over hand-labelled questions.

Golden file format (YAML):

    - question: "how much was my last HDFC credit card bill"
      expected:                       # any email matching ALL keys of one entry counts as relevant
        - {from_contains: hdfcbank, subject_contains: statement}
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from email_agent.db.pool import connection
from email_agent.search.hybrid import hybrid_search

MODES = {
    "bm25": {"mode": "bm25", "rerank": False},
    "vector": {"mode": "vector", "rerank": False},
    "hybrid (rrf)": {"mode": "hybrid", "rerank": False},
    "hybrid + rerank": {"mode": "hybrid", "rerank": True},
}


@dataclass
class ModeScore:
    recall: float
    mrr: float


def recall_at_k(ranked: list[int], relevant: set[int], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0


def reciprocal_rank(ranked: list[int], relevant: set[int]) -> float:
    return next((1.0 / i for i, doc in enumerate(ranked, start=1) if doc in relevant), 0.0)


async def resolve_expected(expected: list[dict]) -> set[int]:
    ids: set[int] = set()
    async with connection() as conn:
        for matcher in expected:
            clauses, params = [], []
            if v := matcher.get("from_contains"):
                clauses.append("(from_addr ILIKE %s OR from_name ILIKE %s)")
                params += [f"%{v}%"] * 2
            if v := matcher.get("subject_contains"):
                clauses.append("subject ILIKE %s")
                params.append(f"%{v}%")
            if v := matcher.get("email_id"):
                clauses.append("id = %s")
                params.append(v)
            if not clauses:
                continue
            rows = await (await conn.execute(f"SELECT id FROM emails WHERE {' AND '.join(clauses)}", params)).fetchall()
            ids |= {r["id"] for r in rows}
    return ids


async def run_eval(golden_path: Path, k: int = 10) -> tuple[dict[str, ModeScore], list[str]]:
    cases = yaml.safe_load(golden_path.read_text()) or []
    sums = {name: [0.0, 0.0] for name in MODES}
    used, warnings = 0, []
    for case in cases:
        relevant = await resolve_expected(case.get("expected", []))
        if not relevant:
            warnings.append(f"no email matches the expectations for: {case['question']!r}")
            continue
        used += 1
        for name, opts in MODES.items():
            hits = await hybrid_search(case["question"], k=k, **opts)
            ranked = [h.email_id for h in hits]
            sums[name][0] += recall_at_k(ranked, relevant, k)
            sums[name][1] += reciprocal_rank(ranked, relevant)
    scores = {name: ModeScore(r / used, m / used) if used else ModeScore(0, 0) for name, (r, m) in sums.items()}
    return scores, warnings
