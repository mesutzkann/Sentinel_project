# ADR-0005: the cross-encoder runs in this process, as an optional dependency

**Status:** Accepted · Phase 6

## Context

Reranking needs `bge-reranker-v2-m3`. It is a cross-encoder: it takes a (query, chunk) pair and
returns one relevance score, which is why it is better than an embedding comparison and why it
cannot be precomputed.

[ADR-0004](0004-embeddings-through-ollama.md) put the embedding model in Ollama and said the
reranker would be different. It is: Ollama serves generation and embeddings, and a cross-encoder
is neither. `POST /api/embed` on a reranker model returns the encoder's pooled output, not a
relevance score, so there is no endpoint to call.

That leaves three ways to run it.

1. **`sentence-transformers` in the AI service process.** Roughly 2.5 GB of PyTorch wheels plus
   2.2 GB of weights, and about 2.5 GB resident when the model is loaded.
2. **A text-embeddings-inference container.** A separate process with its own memory, its own
   image to pull and its own place in compose — for one model, called from one place, in a
   stack that already has fourteen containers.
3. **An LLM-as-reranker prompt**, scoring pairs with the 3B model already loaded. No new
   dependency, and a 30-pair rerank becomes 30 generation calls: seconds, not milliseconds, for
   a judgement the cross-encoder was trained on and the instruct model was not.

## Decision

Option 1, with `sentence-transformers` declared as an optional extra rather than a dependency:

```bash
cd ai-service && pip install -e .[rerank]
```

`Reranker` is the interface. `CrossEncoderReranker` is the only implementation. The model is
loaded on first use, not at construction, and scoring runs in a worker thread under a lock.

Retrieval degrades when the extra is absent. `hybrid_rerank` returns the fused order, logs why,
and reports it: the result carries no `reranked` candidate list, and `GET /rag/stats` says
`rerank_available: false` with the reason. `evaluation/rag_eval.py` does the opposite — it
builds the retriever with `degrade=False` and omits the row entirely.

## Consequences

**A fresh checkout works, and is honest about being smaller.** `pip install -e .[dev]`, ingest,
search — three retrievers out of four, no gigabytes, and every surface that could mislead about
it says so instead. The one thing that must never happen is a benchmark table whose
`hybrid_rerank` row is quietly the `hybrid` row.

**The dependency is optional in the packaging sense, not in the results sense.** Phase 6 exists
to show that reranking helps; the extra is what the demo machine installs. What the option buys
is that CI, the unit suite and anyone reading the code do not pay for it.

**One process holds two models when it is installed.** The cross-encoder is ~600M parameters and
sits alongside whatever Ollama is holding. On the 16 GB development machine that is why
`RERANK_DEVICE` defaults to empty — sentence-transformers picks, and picks CPU when the GPU is
busy — and why scoring is serialised: two concurrent batches on one CPU model do not go twice as
fast, and both batches are resident at once.

**Latency was the thing this ADR got wrong, and the p50 column is what caught it.** The
prediction above — 300–600 ms for 30 pairs of 512 tokens — was written without measuring.
Measured, three times, on the same RTX 3060 laptop:

| | 30 pairs | why |
|---|---|---|
| CPU | **21.5 s** | `pip install torch` resolves `2.14.0+cpu`. There is no CUDA in the default wheel and nothing says so. |
| CUDA, float32 | 1.6–2.6 s | the GPU, at the precision the weights ship in |
| CUDA, float16 | **0.36–0.47 s** | Ampere's half-precision path, which is what the estimate was describing without knowing it |

End to end a `hybrid_rerank` search is **1.1–1.5 s p50** across three benchmark runs, of which
518 ms is the fused search; the spread is contention for the GPU, and the slow end is the run
that also had the 3B model resident. Two to three times the cost of the fusion alone, for R@3
0.956 against 0.910 and R@5 0.973 against 0.944 over the 120-query set.

Two consequences follow. The first is that `RERANK_DTYPE` resolves from the device — float16 on
CUDA, float32 elsewhere — because half precision on a CPU is emulated and *slower*, so the
precision cannot simply be pinned. The second is that recall had to be re-measured rather than
assumed: over the sixty-query set every metric came out identical between the CPU float32 run
and the GPU float16 one, and exactly one query differed at all, in its fourth result, between two
documents that were both irrelevant. Half precision costs nothing here that can be measured.

The estimate was wrong; the reason for the p50 and p95 columns next to recall@5 was not. They
are what turned a design note into a number, twice — first to say 21 s was not a latency an
investigation could pay, then to say a second is.

**A postscript on the recall numbers this ADR quoted.** The query set they came from was sixty
queries and is now a hundred and twenty, and two claims made here on the smaller set did not
survive: that reranking costs first place (its success@1 was 0.817 against fusion's 0.867, and
both are now 0.850), and that it is the retriever with nothing it cannot find (it has the fewest
misses, one, and that one is missed by every retriever). The decision is unchanged and the
argument for it is stronger — depth is where reranking pays, and the doubling made that the whole
of the effect rather than most of it.

**The seam that ADR-0004 kept open is now cheap.** With `sentence-transformers` in the
environment, `SentenceTransformerEmbeddingProvider` is four methods. It is still not written,
for the same reason it was not written then: Ollama serves bge-m3 and nothing needs a second way
to do it.
