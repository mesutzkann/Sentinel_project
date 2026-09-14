"""Precedent: which past incident this one is like, and how alike.

The number in "91% similar to INC-00032" is a cosine between two embeddings, and it is going in
front of a person at three in the morning. So the tests here are about what that sentence is
allowed to claim: that it names a real incident, that it is not the incident being investigated,
and that a threshold nothing crosses would be a feature that does not exist.
"""

from __future__ import annotations

import pytest

from rag.documents import RetrievedChunk, SourceType, StoredChunk
from rag.similarity import DEFAULT_THRESHOLD, IncidentSimilarity


class FakeEmbeddings:
    def __init__(self, vector: list[float] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._vector = vector if vector is not None else [0.1, 0.2, 0.3]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)

        return [self._vector for _ in texts]


class FakeStore:
    """Returns scored chunks and records the filter it was given."""

    def __init__(self, hits: list[RetrievedChunk]) -> None:
        self._hits = hits
        self.filters: list[dict | None] = []

    async def search(self, embedding, k, filters=None):  # type: ignore[no-untyped-def]
        self.filters.append(filters)

        return self._hits[:k]


def hit(
    external_id: str | None,
    score: float,
    *,
    document_id: str | None = None,
    title: str = "",
    service: str | None = "orders",
    source_type: str = SourceType.POSTMORTEM.value,
    content: str = "The pool was cut from 200 to 20 and requests queued behind it.",
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk=StoredChunk(
            chunk_id=f"c-{external_id}-{score}",
            document_id=document_id or f"d-{external_id}",
            content=content,
            chunk_index=0,
            title=title or f"{external_id} — something broke",
            source_type=source_type,
            service=service,
            external_id=external_id,
            path=f"incidents/{external_id}.md",
            section=None,
        ),
        score=score,
        rank=1,
        retriever="vector",
    )


def finder(hits: list[RetrievedChunk], **kwargs) -> tuple[IncidentSimilarity, FakeStore]:
    store = FakeStore(hits)

    return IncidentSimilarity(FakeEmbeddings(), store, **kwargs), store


@pytest.mark.asyncio
async def test_a_precedent_is_named_with_how_alike_it_is() -> None:
    similarity, _ = finder([hit("INC-00001", 0.71)])

    found = await similarity.find("orders is timing out")

    assert len(found) == 1
    assert found[0].percent == 71
    assert found[0].sentence().startswith("71% similar to INC-00001")


@pytest.mark.asyncio
async def test_anything_under_the_threshold_is_not_a_precedent() -> None:
    """A question with no history scored 0.448 against the seed corpus; that is not a match."""
    similarity, _ = finder([hit("INC-00010", 0.448), hit("INC-00004", 0.435)])

    assert await similarity.find("how do I add a new service to the mesh") == []


@pytest.mark.asyncio
async def test_the_investigation_does_not_match_its_own_write_up() -> None:
    """Since Phase 9 a concluded run writes itself into this corpus, at similarity near 1.00."""
    similarity, _ = finder([hit("INC-00142", 0.99), hit("INC-00001", 0.66)])

    found = await similarity.find("orders is timing out", exclude="INC-00142")

    assert [f.external_id for f in found] == ["INC-00001"]


@pytest.mark.asyncio
async def test_only_past_occurrences_count_as_precedent() -> None:
    """A runbook about this failure is a useful document and it is not a time it happened."""
    similarity, store = finder([hit("INC-00001", 0.7)])

    await similarity.find("orders is timing out")

    assert store.filters[0] == {"document_type": ["incident", "postmortem"]}


@pytest.mark.asyncio
async def test_one_incident_is_named_once_at_its_best_chunk() -> None:
    """A long postmortem has several chunks; three of them is not three precedents."""
    similarity, _ = finder(
        [
            hit("INC-00001", 0.64, document_id="d-1"),
            hit("INC-00001", 0.71, document_id="d-1"),
            hit("INC-00001", 0.66, document_id="d-1"),
        ]
    )

    found = await similarity.find("orders is timing out")

    assert len(found) == 1
    assert found[0].percent == 71


@pytest.mark.asyncio
async def test_an_unnamed_document_is_not_offered_as_a_precedent() -> None:
    """"83% similar to a document with no id" is not a sentence worth showing anybody."""
    similarity, _ = finder([hit(None, 0.83), hit("INC-00001", 0.65)])

    found = await similarity.find("orders is timing out")

    assert [f.external_id for f in found] == ["INC-00001"]


@pytest.mark.asyncio
async def test_the_service_on_fire_breaks_a_tie() -> None:
    """Two equally alike precedents are not equally useful."""
    similarity, _ = finder(
        [
            hit("INC-00010", 0.72, service="payments"),
            hit("INC-00001", 0.70, service="orders"),
        ]
    )

    found = await similarity.find("orders is timing out", service="orders")

    assert [f.external_id for f in found] == ["INC-00001", "INC-00010"]

    # Without a service, the ranking is the similarity and nothing else.
    unbiased = await similarity.find("orders is timing out")

    assert [f.external_id for f in unbiased] == ["INC-00010", "INC-00001"]


@pytest.mark.asyncio
async def test_an_empty_question_is_not_embedded() -> None:
    embeddings = FakeEmbeddings()
    similarity = IncidentSimilarity(embeddings, FakeStore([]))

    assert await similarity.find("   ") == []
    assert embeddings.calls == []


def test_the_threshold_is_the_measured_one_not_the_planning_documents() -> None:
    """docs/planning.md §6 says 0.85. Measured on bge-m3, no true precedent reaches it.

    The seed corpus's strongest real match is 0.750 and its weakest is 0.632; an unrelated
    question tops out at 0.448. A threshold of 0.85 would make this feature silent for ever,
    which is the failure mode that looks exactly like it working.
    """
    assert 0.45 < DEFAULT_THRESHOLD < 0.632
