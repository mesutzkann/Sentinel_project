# ADR-0004: bge-m3 is served by Ollama, not loaded in-process

**Status:** Accepted · Phase 5

## Context

Hybrid retrieval needs a dense embedding of every chunk and of every query.
[docs/planning.md](../planning.md) chose the model — BAAI/bge-m3, for its multilingual vector
space and its 1024-dimension dense output — and named `sentence-transformers` as the library
that would run it, alongside `bge-reranker-v2-m3` in Phase 6.

Running it in-process means PyTorch. On the machine this project is developed on that is roughly
2.5 GB of wheels plus a 2.2 GB model, and about the same again in resident memory when the
encoder is loaded, in a 16 GB box that is already running a 3B language model, PostgreSQL, six
MCP containers, five sample services and the observability stack.

Ollama is already running, already holds the reasoning model, and already serves embeddings for
bge-m3 over `POST /api/embed`. It is the same weights and the same 1024 dimensions.

## Decision

`EmbeddingProvider` is the interface. `OllamaEmbeddingProvider` is the only implementation, and
it is configured with `EMBEDDING_MODEL=bge-m3`.

## Consequences

**One model runtime, not two.** Ollama already decides what fits in 6 GB of VRAM and what
spills to CPU. A second framework loading a second model into the same GPU makes that a
negotiation between two processes that cannot see each other, and the failure mode is an
out-of-memory error in whichever one asked second.

**One extra dependency to install, and it is a `pull`.** `ollama pull bge-m3` is 1.2 GB and the
README says so. The alternative was 4.7 GB of Python packages and weights.

**No `sentence-transformers` in this phase.** Phase 6 needs `bge-reranker-v2-m3`, a cross-encoder
that Ollama does not serve, so the transformers dependency arrives then — for the thing that
actually requires it, in the phase whose benchmark justifies it, rather than a phase early for a
model that had another way to run.

**The interface keeps the option open.** A `SentenceTransformerEmbeddingProvider` is one class
implementing four methods. It is not written, because unused code that nothing exercises is a
liability rather than an option, and the seam is what makes it cheap to add if the reranker work
in Phase 6 puts the library in the environment anyway.

**Availability is reported, not assumed.** `GET /rag/stats` says whether the embedding model is
reachable. A populated index with no reachable model still answers lexical searches and cannot
answer dense ones, and that difference is invisible from a search result alone — the hybrid
retriever degrades to its lexical half rather than failing.
