import asyncio

from app.retrieval.models import EvidenceCandidate
from app.retrieval.reranker import BoundedRerankerExecutor, CrossEncoderReranker


def _candidate(chunk_id: str, text: str) -> EvidenceCandidate:
    return EvidenceCandidate(
        chunk_id=chunk_id,
        text=text,
        source_snapshot_id=f"snapshot-{chunk_id}",
        score=1.0,
        rank=1,
    )


def test_cross_encoder_reranker_reorders_by_scores() -> None:
    class FakeModel:
        def predict(self, pairs):
            return [len(pair[1]) for pair in pairs]

    reranker = CrossEncoderReranker(loader=lambda name: FakeModel())
    candidates = [
        _candidate("short", "短"),
        _candidate("long", "这是一段较长的合同依据文本"),
    ]
    ranked = reranker.rerank("待审核条款", candidates, top_k=2)

    assert [item.chunk_id for item in ranked] == ["long", "short"]
    assert [item.rank for item in ranked] == [1, 2]


def test_bounded_reranker_executor_queues_concurrent_calls() -> None:
    executor = BoundedRerankerExecutor(max_waiters=4)

    async def main():
        async def call(value):
            return await executor.run(lambda value=value: value)

        return await asyncio.gather(call(1), call(2), call(3))

    assert asyncio.run(main()) == [1, 2, 3]
