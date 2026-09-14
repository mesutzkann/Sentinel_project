"""Where a finished investigation is kept so the next one can find it.

Two places, not one, and the second is the reason the first is not enough:

* **The corpus.** The Markdown file is written under the knowledge base directory next to the
  ten seed incidents. Without it, the postmortem lives only in the `rag` schema — and the next
  `POST /rag/ingest`, which mirrors the directory and prunes what is not in it, would delete it.
  A memory that a routine re-ingest erases is not a memory.
* **The index.** The document is ingested immediately, with ``prune=False``, so it is searchable
  in the same minute rather than at the next restart. An incident that recurs an hour later is
  the case this phase exists for.

The write is idempotent by content: a postmortem regenerated for the same incident overwrites
its own file and re-ingests only if the text actually changed, because the ingest pipeline skips
documents whose content hash it already has.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from agents.postmortem import Postmortem
from rag.ingest import IngestionPipeline

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Remembered:
    """What became of one postmortem."""

    incident_code: str
    path: Path
    written: bool
    indexed: bool
    chunks: int
    status: str
    error: str | None = None


class IncidentMemory:
    """Writes postmortems into the corpus and the index."""

    def __init__(self, pipeline: IngestionPipeline, knowledge_base_dir: Path) -> None:
        self._pipeline = pipeline
        self._root = knowledge_base_dir

    async def remember(self, postmortem: Postmortem) -> Remembered:
        """Store and index one postmortem. Never raises.

        An investigation that reached a conclusion has already produced its value; failing it
        because the knowledge base would not take the write-up would be trading the answer for
        the archive. Both failures are reported in the return value and logged, and the caller
        decides what to say about them.
        """
        path = self._root / postmortem.filename
        written = False

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(postmortem.markdown, encoding="utf-8")
            written = True
        except OSError as exc:
            logger.warning("Could not write %s: %s", path, exc)

            return Remembered(
                incident_code=postmortem.incident_code,
                path=path,
                written=False,
                indexed=False,
                chunks=0,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

        try:
            # prune=False, emphatically: this ingests one document into a corpus of thirty, and
            # the pruning form of this call would treat those thirty as no longer present.
            report = await self._pipeline.ingest([postmortem.document], prune=False)
        except Exception as exc:  # noqa: BLE001 - the embedding model being down is not a defect
            logger.warning("Could not index %s: %s", postmortem.incident_code, exc)

            return Remembered(
                incident_code=postmortem.incident_code,
                path=path,
                written=written,
                indexed=False,
                chunks=0,
                status="written_not_indexed",
                error=f"{type(exc).__name__}: {exc}",
            )

        outcome = report.documents[0] if report.documents else None
        status = outcome.status if outcome else "failed"

        return Remembered(
            incident_code=postmortem.incident_code,
            path=path,
            written=written,
            indexed=status in {"ingested", "replaced", "unchanged"},
            chunks=outcome.chunks if outcome else 0,
            status=status,
            error=outcome.error if outcome else "the ingest reported nothing",
        )
