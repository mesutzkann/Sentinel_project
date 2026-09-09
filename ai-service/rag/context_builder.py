"""Turning ranked chunks into the block of text that goes in a prompt.

Retrieval ends with five chunks. A prompt needs one string, and what happens between those two
decides whether the model can use what was retrieved:

*Every chunk is labelled and numbered.* ``[S3] runbook · orders · Runbook — connection pool
exhaustion › Fix``. The label is what makes a citation possible — the agent is asked to name the
sources it used, and ``[S3]`` maps back to a chunk id, which maps back to a document the
evidence panel can link to. Without it a root cause built on a runbook is indistinguishable from
one the model invented.

*The budget is enforced here, not hoped for.* 3000 tokens, from docs/planning.md. The reasoning
model has an 8k window that also has to hold the system prompt, the incident, the evidence
collected from MCP tools and the answer. A retrieval that quietly returns 6000 tokens of context
does not fail — it pushes the incident out of the window, and the model answers a question it
can no longer see.

*Overlap is removed.* The chunker deliberately repeats whole blocks across adjacent chunks so
that a sentence near a boundary keeps its context, which is right for retrieval and wasteful
here: two chunks of one runbook can arrive with a third of their text identical, and the budget
pays for it twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rag.chunking import estimate_tokens
from rag.documents import RetrievedChunk, SourceType

# From docs/planning.md. See the module docstring for what it is protecting.
DEFAULT_TOKEN_BUDGET = 3000


@dataclass(frozen=True, slots=True)
class ContextSource:
    """One labelled chunk in the built context.

    Returned alongside the text rather than only rendered into it, because two different
    consumers need it: the prompt needs the label inline, and the investigation record needs the
    chunk id behind ``[S3]`` to store which knowledge the conclusion rests on.
    """

    ref: str
    chunk_id: str
    document_id: str
    title: str
    source_type: SourceType
    score: float
    rank: int
    service: str | None = None
    external_id: str | None = None
    path: str | None = None
    section: str | None = None

    @property
    def label(self) -> str:
        """The one-line provenance header a chunk is introduced by."""
        parts = [self.source_type.value]

        if self.service:
            parts.append(self.service)

        if self.external_id:
            parts.append(self.external_id)

        parts.append(self.section or self.title)

        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class Context:
    """The text to put in the prompt, and what went into it."""

    text: str
    sources: list[ContextSource] = field(default_factory=list)
    token_count: int = 0
    # Chunks that were retrieved and did not make it in — over budget, or wholly duplicated by
    # something already included. Reported because "the model was not shown it" and "the model
    # was shown it and ignored it" are different failures and look identical from the answer.
    dropped: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.sources


class ContextBuilder:
    """Assembles retrieved chunks into a bounded, labelled context block."""

    def __init__(
        self,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        header: str = "Knowledge base excerpts, most relevant first:",
    ) -> None:
        self._budget = token_budget
        self._header = header

    def build(self, chunks: list[RetrievedChunk]) -> Context:
        """Fit as many chunks as the budget allows, best first.

        Greedy in rank order, and it keeps going after the first chunk that does not fit rather
        than stopping there: chunks vary from 80 tokens to 400, and abandoning the rest of the
        list because the fourth one was long would drop a relevant 90-token known-error note to
        save a place nothing else could use.
        """
        if not chunks:
            return Context(text="", token_count=0)

        used = estimate_tokens(self._header)
        sources: list[ContextSource] = []
        blocks: list[str] = []
        dropped: list[str] = []
        seen: list[str] = []

        for chunk in chunks:
            content = _without_overlap(chunk.content, seen)

            if not content:
                dropped.append(chunk.chunk_id)
                continue

            source = _as_source(chunk, ref=f"S{len(sources) + 1}")
            block = f"[{source.ref}] {source.label}\n{content}"
            cost = estimate_tokens(block) + 1  # the blank line between blocks

            if used + cost > self._budget:
                dropped.append(chunk.chunk_id)
                continue

            used += cost
            sources.append(source)
            blocks.append(block)
            seen.append(content)

        if not blocks:
            return Context(text="", token_count=0, dropped=dropped)

        text = "\n\n".join([self._header, *blocks])

        return Context(
            text=text,
            sources=sources,
            # Recomputed from the finished string rather than accumulated: the running total is
            # a budget check, and reporting the sum of the parts as the size of the whole is how
            # an off-by-one in the joins becomes invisible.
            token_count=estimate_tokens(text),
            dropped=dropped,
        )


def _as_source(chunk: RetrievedChunk, ref: str) -> ContextSource:
    stored = chunk.chunk

    return ContextSource(
        ref=ref,
        chunk_id=stored.chunk_id,
        document_id=stored.document_id,
        title=stored.title,
        source_type=stored.source_type,
        score=chunk.score,
        rank=chunk.rank,
        service=stored.service,
        external_id=stored.external_id,
        path=stored.path,
        section=stored.section,
    )


def _without_overlap(content: str, seen: list[str]) -> str:
    """Drop the paragraphs of ``content`` that an already-included chunk carried.

    Paragraph-level rather than line-level. The chunker's overlap is whole blocks, so what
    repeats is a paragraph, a list or a code fence; comparing line by line would also strip a
    ``## Fix`` heading that legitimately appears in two different runbooks, and leave the text
    under it orphaned.
    """
    if not seen:
        return content.strip()

    kept = [
        paragraph
        for paragraph in content.split("\n\n")
        if paragraph.strip() and not any(paragraph.strip() in earlier for earlier in seen)
    ]

    return "\n\n".join(kept).strip()
