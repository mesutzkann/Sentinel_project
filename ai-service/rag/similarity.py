"""Has this happened before, and how alike is it?

docs/planning.md §6 asks for one sentence of evidence: *"INC-00032 ile %91 benzerlik"* — the
incident's own words embedded and compared against past incidents and postmortems, with anything
above a threshold carried into the investigation as a fact.

**This is not the same question retrieval answers.** `SEARCH_HISTORY` asks "what do we know about
this failure" and answers with the best chunks from anywhere in the knowledge base, ranked by a
fused score that is not a similarity and not comparable across retrievers. This asks a narrower
one — "which past *incident* is this one like" — and answers with a cosine distance, because the
number goes in front of a person. "91% similar to INC-00032" is a claim; "rank 2 of 5, score
0.0164" is not something anybody can act on.

**An investigation must not match its own postmortem.** Since this phase a concluded
investigation writes itself into the same corpus, so the second run against one incident would
otherwise retrieve the first run's write-up at a similarity near 1.00 and report the incident as
a precedent for itself. ``exclude`` is not an optimisation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rag.documents import SourceType
from rag.embeddings import EmbeddingProvider
from rag.store import VectorStore

logger = logging.getLogger(__name__)

# Above this, two incidents are alike enough to say so. **Measured, not assumed.** Eight
# investigation-shaped questions against the ten seed incidents on bge-m3:
#
#   "orders is timing out, find out why"            -> INC-00001  0.632
#   "payments is returning 500s intermittently"     -> INC-00006  0.646
#   "notifications memory keeps climbing"           -> INC-00005  0.674
#   "gateway requests fail after about a second"    -> INC-00002  0.684
#   "orders queries got slow this afternoon"        -> INC-00009  0.715
#   "payments latency is up and orders times out"   -> INC-00010  0.750
#   "how do I add a new service to the mesh"        -> best match 0.448   (no precedent, correctly)
#
# So the true precedents live between 0.63 and 0.75 and a question with no precedent tops out
# below 0.45. **docs/planning.md §6 says 0.85, and at 0.85 this feature would never once fire** —
# that number is the cosine between near-identical texts, not between an incident and somebody's
# description of it a year later. 0.62 sits under every true match and well above the unrelated
# question.
#
# The honest caveat: a vague question ("why is the database so slow") also scores 0.61-0.65,
# because it genuinely does resemble several past incidents. The threshold separates precedent
# from irrelevance, not specific from vague.
DEFAULT_THRESHOLD = 0.62

#: Chunks to pull before folding to documents. A long postmortem has several chunks and the
#: strongest one is what the incident is being compared against, so the fold keeps the best per
#: document and this number has to be comfortably above the number of documents wanted.
DEFAULT_CHUNKS = 12

#: What counts as a precedent. A runbook describing the same failure is a useful document and it
#: is not a past occurrence, and the distinction is what makes the sentence true.
PRECEDENT_TYPES = (SourceType.INCIDENT.value, SourceType.POSTMORTEM.value)


@dataclass(frozen=True, slots=True)
class SimilarIncident:
    """One past incident, and how alike this one is."""

    external_id: str
    title: str
    similarity: float
    document_id: str
    source_type: str
    service: str | None = None
    path: str | None = None
    excerpt: str = ""

    @property
    def percent(self) -> int:
        return round(self.similarity * 100)

    def sentence(self) -> str:
        """What goes into evidence, and onto a screen."""
        return f"{self.percent}% similar to {self.external_id}: {self.title}"


class IncidentSimilarity:
    """Finds the past incidents this one resembles."""

    def __init__(
        self,
        embeddings: EmbeddingProvider,
        store: VectorStore,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        chunks: int = DEFAULT_CHUNKS,
    ) -> None:
        self._embeddings = embeddings
        self._store = store
        self._threshold = threshold
        self._chunks = chunks

    @property
    def threshold(self) -> float:
        return self._threshold

    async def find(
        self,
        summary: str,
        *,
        exclude: str | None = None,
        limit: int = 3,
        service: str | None = None,
    ) -> list[SimilarIncident]:
        """Past incidents above the threshold, most alike first.

        ``service`` narrows the search when one is known; it does not exclude the rest of the
        corpus, because a latency cascade in payments is precedent for one in orders and a filter
        that hid it would be filtering out the most useful kind of match there is.
        """
        if not summary.strip():
            return []

        vectors = await self._embeddings.embed([summary])

        if not vectors:
            return []

        hits = await self._store.search(
            vectors[0],
            self._chunks,
            {"document_type": list(PRECEDENT_TYPES)},
        )

        best: dict[str, SimilarIncident] = {}

        for hit in hits:
            chunk = hit.chunk
            external_id = chunk.external_id

            # A precedent has to be nameable. "83% similar to a document with no id" is not a
            # sentence worth putting in front of somebody at three in the morning.
            if not external_id or external_id == exclude:
                continue

            if hit.score < self._threshold:
                continue

            if external_id in best and best[external_id].similarity >= hit.score:
                continue

            best[external_id] = SimilarIncident(
                external_id=external_id,
                title=chunk.title,
                similarity=hit.score,
                document_id=chunk.document_id,
                source_type=chunk.source_type,
                service=chunk.service,
                path=chunk.path,
                excerpt=" ".join(chunk.content.split())[:200],
            )

        ranked = sorted(best.values(), key=lambda found: found.similarity, reverse=True)

        if service:
            # Same service first, similarity second: two equally alike precedents are not equally
            # useful when one of them is about the service that is currently on fire.
            ranked.sort(key=lambda found: (found.service != service, -found.similarity))

        return ranked[:limit]
