"""The ``/rag`` endpoints: ingest the knowledge base, and search it.

``/rag/search`` takes a retriever by name. That is not a debugging convenience — it is how the
Phase 6 evaluation runs the same query set through all four retrievers, and how anyone reading
the project can see for themselves that hybrid retrieval beats either half rather than being
told so.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings, settings
from rag.chunking import MarkdownChunker
from rag.context_builder import ContextBuilder
from rag.documents import SourceType
from rag.embeddings import EmbeddingProvider, EmbeddingUnavailableError, OllamaEmbeddingProvider
from rag.filters import KNOWN_KEYS, FilterError, normalize
from rag.ingest import CorpusError, IngestionPipeline
from rag.lexical import Bm25Index
from rag.rerank import CrossEncoderReranker
from rag.retrievers import (
    Bm25Retriever,
    HybridRerankRetriever,
    HybridRetriever,
    Retriever,
    VectorRetriever,
)
from rag.store import PgVectorStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rag", tags=["rag"])

RetrieverName = Literal["bm25", "vector", "hybrid", "hybrid_rerank"]


class RagService:
    """Everything retrieval needs, assembled once for the process.

    The lexical index is in memory, so it has to be filled from the store before the first
    search. That happens on demand rather than at startup: the AI service has to be able to
    start when PostgreSQL is not up yet — it is one process in a compose stack, not the last one
    — and a service that refuses to boot because a dependency is slow is harder to operate than
    one whose first search is slower.
    """

    def __init__(self, config: Settings) -> None:
        self._config = config
        self.store = PgVectorStore.from_url(config.database_url)
        self.embeddings: EmbeddingProvider = OllamaEmbeddingProvider(
            base_url=config.ollama_base_url,
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
        )
        self.lexical = Bm25Index()
        self.chunker = MarkdownChunker(
            target_tokens=config.chunk_target_tokens,
            overlap_tokens=config.chunk_overlap_tokens,
        )
        self.pipeline = IngestionPipeline(
            chunker=self.chunker,
            embeddings=self.embeddings,
            store=self.store,
            lexical=self.lexical,
        )

        self.reranker = CrossEncoderReranker(
            model=config.rerank_model,
            device=config.rerank_device_or_auto,
            max_length=config.rerank_max_length,
            batch_size=config.rerank_batch_size,
        )
        self.context_builder = ContextBuilder(token_budget=config.context_token_budget)

        bm25 = Bm25Retriever(self.lexical)
        vector = VectorRetriever(self.embeddings, self.store)
        hybrid = HybridRetriever(bm25, vector, candidates=config.retrieval_candidates)

        self.retrievers: dict[str, Retriever] = {
            "bm25": bm25,
            "vector": vector,
            "hybrid": hybrid,
            # Degrades to the fused order when the cross-encoder is not installed. A search is a
            # request from a person or an agent that has something else to do; the evaluation
            # builds its own with ``degrade=False``, because a benchmark row that quietly
            # reports fused numbers under the reranker's name is worse than no row.
            "hybrid_rerank": HybridRerankRetriever(
                hybrid, self.reranker, candidates=config.retrieval_candidates
            ),
        }

        self._index_loaded = False

    async def ensure_index(self) -> None:
        """Fill the lexical index from the store, once."""
        if self._index_loaded:
            return

        await self.pipeline.rebuild_lexical_index()
        self._index_loaded = True

    def invalidate_index(self) -> None:
        """After an ingest the pipeline has already rebuilt it; this just records that."""
        self._index_loaded = True


# One per process. The engine holds a connection pool and the lexical index holds the corpus;
# building either per request would be pointless work, and building the index per request would
# be a full table scan per search.
_service: RagService | None = None


def get_service(config: Annotated[Settings, Depends(settings)]) -> RagService:
    global _service  # noqa: PLW0603 - process-wide, intentionally

    if _service is None:
        _service = RagService(config)

    return _service


# ---------------------------------------------------------------------- contracts ----


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=50)
    retriever: RetrieverName = "hybrid"
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Metadata equality filters, ANDed. A list value matches any of its entries. "
            f"Commonly used keys: {', '.join(KNOWN_KEYS)}."
        ),
    )
    investigation_id: str | None = Field(
        default=None,
        description="Recorded on the retrieval log, so a search can be traced to what asked it.",
    )
    build_context: bool = Field(
        default=False,
        description=(
            "Also return the results assembled into a labelled, token-bounded block ready to "
            "put in a prompt. What the investigation agent asks for; off by default because a "
            "caller that renders the hits itself would be paying for a second copy of them."
        ),
    )


class SearchHit(BaseModel):
    chunk_id: str
    document_id: str
    title: str
    source_type: SourceType
    content: str
    score: float
    rank: int
    chunk_index: int
    service: str | None = None
    external_id: str | None = None
    path: str | None = None
    section: str | None = None


class ContextSourceModel(BaseModel):
    ref: str
    chunk_id: str
    document_id: str
    title: str
    source_type: SourceType
    label: str
    rank: int
    score: float
    service: str | None = None
    external_id: str | None = None
    path: str | None = None
    section: str | None = None


class ContextModel(BaseModel):
    text: str
    token_count: int
    sources: list[ContextSourceModel]
    dropped: list[str]


class SearchResponse(BaseModel):
    query: str
    retriever: str
    filters: dict[str, Any]
    total: int
    latency_ms: dict[str, int]
    # Per-retriever candidate lists. Empty for a single-retriever search, which is the honest
    # representation: there was nothing to fuse. A `hybrid_rerank` search that could not load
    # the cross-encoder reports `rerank_skipped` instead of `reranked`.
    candidates: dict[str, list[str]]
    results: list[SearchHit]
    context: ContextModel | None = None


class IngestRequest(BaseModel):
    force: bool = Field(
        default=False,
        description="Re-embed every document even if its content hash is unchanged.",
    )
    prune: bool = Field(
        default=True,
        description="Remove indexed documents whose file is no longer in the corpus.",
    )


class DocumentResult(BaseModel):
    key: str
    title: str
    status: str
    chunks: int
    error: str | None = None


class IngestResponse(BaseModel):
    ingested: int
    replaced: int
    unchanged: int
    removed: int
    failed: int
    chunks_written: int
    duration_ms: int
    documents: list[DocumentResult]


class StatsResponse(BaseModel):
    documents: int
    chunks: int
    documents_by_type: dict[str, int]
    documents_by_service: dict[str, int]
    lexical_index_size: int
    embedding_model: str
    embedding_available: bool
    rerank_model: str
    rerank_available: bool
    rerank_unavailable_reason: str | None = None


# ---------------------------------------------------------------------- endpoints ----


@router.post("/search", response_model=SearchResponse)
async def search(
    request: SearchRequest,
    service: Annotated[RagService, Depends(get_service)],
) -> SearchResponse:
    """Retrieve chunks for a query.

    An empty result is a 200 with no hits, not a 404. "The knowledge base has nothing about
    this" is an answer the agent has to be able to reason about, and Phase 7 treats it as
    evidence — the absence of a precedent is itself a finding.
    """
    try:
        filters = normalize(request.filters)
    except FilterError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    retriever = service.retrievers[request.retriever]

    try:
        await service.ensure_index()
        result = await retriever.retrieve(request.query, request.k, filters)
    except EmbeddingUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except Exception as exc:
        logger.exception("Retrieval failed for query %r", request.query)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Retrieval failed: {exc}",
        ) from exc

    await service.store.log_retrieval(
        query=result.query,
        retriever=result.retriever,
        filters=result.filters,
        candidates=result.candidates,
        latency_ms=result.stage_latency_ms,
        investigation_id=request.investigation_id,
    )

    context = service.context_builder.build(result.chunks) if request.build_context else None

    return SearchResponse(
        query=result.query,
        retriever=result.retriever,
        filters=result.filters,
        total=len(result.chunks),
        latency_ms=result.stage_latency_ms,
        candidates=result.candidates,
        context=(
            ContextModel(
                text=context.text,
                token_count=context.token_count,
                sources=[
                    ContextSourceModel(
                        ref=s.ref,
                        chunk_id=s.chunk_id,
                        document_id=s.document_id,
                        title=s.title,
                        source_type=s.source_type,
                        label=s.label,
                        rank=s.rank,
                        score=round(s.score, 6),
                        service=s.service,
                        external_id=s.external_id,
                        path=s.path,
                        section=s.section,
                    )
                    for s in context.sources
                ],
                dropped=context.dropped,
            )
            if context is not None
            else None
        ),
        results=[
            SearchHit(
                chunk_id=hit.chunk.chunk_id,
                document_id=hit.chunk.document_id,
                title=hit.chunk.title,
                source_type=hit.chunk.source_type,
                content=hit.chunk.content,
                score=round(hit.score, 6),
                rank=hit.rank,
                chunk_index=hit.chunk.chunk_index,
                service=hit.chunk.service,
                external_id=hit.chunk.external_id,
                path=hit.chunk.path,
                section=hit.chunk.section,
            )
            for hit in result.chunks
        ],
    )


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    request: IngestRequest,
    service: Annotated[RagService, Depends(get_service)],
    config: Annotated[Settings, Depends(settings)],
) -> IngestResponse:
    """Ingest the seed corpus from disk.

    Idempotent: a document whose content has not changed is not re-embedded, so running this
    twice costs one directory walk and a set of hash comparisons. That is what makes it safe to
    call on every start.
    """
    try:
        report = await service.pipeline.ingest_corpus(
            config.knowledge_base_dir, force=request.force, prune=request.prune
        )
    except CorpusError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except EmbeddingUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    service.invalidate_index()

    return IngestResponse(
        ingested=report.count("ingested"),
        replaced=report.count("replaced"),
        unchanged=report.count("unchanged"),
        removed=report.count("removed"),
        failed=report.count("failed"),
        chunks_written=report.chunks_written,
        duration_ms=report.duration_ms,
        documents=[
            DocumentResult(
                key=d.key, title=d.title, status=d.status, chunks=d.chunks, error=d.error
            )
            for d in report.documents
        ],
    )


@router.get("/stats", response_model=StatsResponse)
async def stats(
    service: Annotated[RagService, Depends(get_service)],
) -> StatsResponse:
    """What is in the knowledge base, and whether retrieval can currently run.

    Reports the embedding model's availability rather than assuming it: a populated index with
    no reachable embedding model still answers lexical searches and cannot answer dense ones,
    and that is worth being able to see before a search returns half of what it should.
    """
    try:
        store_stats = await service.store.stats()
        await service.ensure_index()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not read the rag schema: {exc}",
        ) from exc

    return StatsResponse(
        documents=store_stats.documents,
        chunks=store_stats.chunks,
        documents_by_type=store_stats.documents_by_type,
        documents_by_service=store_stats.documents_by_service,
        lexical_index_size=service.lexical.size,
        embedding_model=service.embeddings.model,
        embedding_available=await service.embeddings.is_available(),
        rerank_model=service.reranker.model,
        rerank_available=await service.reranker.is_available(),
        rerank_unavailable_reason=service.reranker.unavailable_reason,
    )
