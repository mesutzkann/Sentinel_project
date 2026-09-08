"""Cutting a document into pieces that survive being read on their own.

The corpus is Markdown — runbooks, architecture notes, postmortems — so the chunker splits on
the structure the text already has (headings, paragraphs, code fences, tables) rather than on a
fixed character count. A fixed-width cut through a runbook reliably lands in the middle of the
one command the reader needed, and neither BM25 nor an embedding can recover what was cut off.

Two behaviours are worth knowing about before reading the code:

*Chunks are prefixed with their heading path.* "Restore MaxPoolSize to 200" retrieves badly on
its own; "Runbook: connection pool exhaustion · Fix" in front of it retrieves well, and the
person reading the evidence panel can see where the sentence came from. The breadcrumb is part
of the stored content because it has to be seen by the embedding and the lexical index, not
just by the UI.

*Overlap is whole blocks, not a sliding character window.* The point of overlap is that a
sentence near a boundary still has its context in both chunks; half a sentence in both achieves
nothing and reads as corruption in the evidence panel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag.documents import Chunk, Document

# bge-m3 tokenises with XLM-RoBERTa's SentencePiece model, which is not available without
# pulling in the tokenizer library. Characters per token there run about 4 for English prose and
# closer to 3 for Turkish, whose agglutinative morphology splits into more pieces. 3.4 is the
# conservative end of that range: it over-estimates, so chunks come out slightly under target
# rather than slightly over, and under target is the harmless direction — the model has an 8192
# token window and the budget being protected is the prompt's, not the encoder's.
_CHARS_PER_TOKEN = 3.4

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_OR_TABLE = re.compile(r"^\s*([-*+]\s|\d+[.)]\s|\||>)")

# Sentence boundaries for splitting a paragraph that is too long to be a chunk on its own.
# Deliberately crude: it only runs on paragraphs of 400+ tokens, where a wrong split costs a
# clause and no more.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÇĞİÖŞÜ0-9])")


def estimate_tokens(text: str) -> int:
    """Approximate token count for a piece of text.

    An estimate, and named like one. Exact counts would need the model's tokenizer loaded in
    this process; what the chunker actually needs is a stable, slightly pessimistic ruler, and
    the cost of being wrong by 10% is a chunk that is 10% smaller than it could have been.
    """
    if not text:
        return 0

    return max(1, round(len(text) / _CHARS_PER_TOKEN))


@dataclass(frozen=True, slots=True)
class _Block:
    """One structural unit of the document: a paragraph, a code fence, a list, a heading."""

    text: str
    section: str | None
    is_heading: bool = False
    heading_level: int = 0
    is_code: bool = False

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


class MarkdownChunker:
    """Splits Markdown into retrieval-sized chunks along its own structure.

    Defaults come from docs/planning.md: 400 tokens with 60 of overlap. At bge-m3's density that
    is roughly a screen of text — long enough to hold a complete diagnostic step, short enough
    that five of them fit in a prompt alongside logs, metrics and traces.
    """

    def __init__(
        self,
        target_tokens: int = 400,
        overlap_tokens: int = 60,
        prepend_context: bool = True,
    ) -> None:
        if overlap_tokens >= target_tokens:
            raise ValueError("overlap_tokens must be smaller than target_tokens")

        self._target = target_tokens
        self._overlap = overlap_tokens
        self._prepend_context = prepend_context

    def chunk(self, document: Document) -> list[Chunk]:
        """Chunks in document order, indexed from zero.

        An empty or whitespace-only document produces no chunks rather than one empty one. A
        document with nothing in it is a loader problem, and an empty chunk in the index is a
        result that can be retrieved and cannot be read.
        """
        blocks = self._blocks(document.content)

        if not blocks:
            return []

        chunks: list[Chunk] = []
        current: list[_Block] = []
        current_tokens = 0

        for block in self._split_oversized(blocks):
            starts_new_section = (
                block.is_heading
                and block.heading_level <= 2
                # Only worth breaking on if there is enough in the current chunk to be useful on
                # its own; otherwise a document of short sections yields a chunk per heading.
                and current_tokens >= self._target // 2
            )
            would_overflow = current and current_tokens + block.tokens > self._target

            if starts_new_section or would_overflow:
                chunks.append(self._emit(document, current, len(chunks)))
                current = self._carry_over(current)
                current_tokens = sum(b.tokens for b in current)

            current.append(block)
            current_tokens += block.tokens

        if current:
            chunks.append(self._emit(document, current, len(chunks)))

        # A trailing chunk made only of carried-over overlap repeats text that is already in the
        # previous chunk and adds nothing retrievable. It happens when a document ends just past
        # a boundary.
        if len(chunks) > 1 and self._is_redundant(chunks[-1], chunks[-2]):
            chunks.pop()

        return chunks

    # ------------------------------------------------------------------ parsing ----

    def _blocks(self, content: str) -> list[_Block]:
        """Markdown to blocks, carrying the heading path each block sits under."""
        blocks: list[_Block] = []
        path: list[str] = []
        pending: list[str] = []
        fence: str | None = None
        code: list[str] = []

        def flush_paragraph() -> None:
            nonlocal pending
            if pending:
                blocks.append(_Block("\n".join(pending), _section(path)))
                pending = []

        for line in content.splitlines():
            fence_match = _FENCE.match(line)

            if fence is not None:
                code.append(line)
                if fence_match and fence_match.group(1) == fence:
                    blocks.append(_Block("\n".join(code), _section(path), is_code=True))
                    fence, code = None, []
                continue

            if fence_match:
                flush_paragraph()
                fence = fence_match.group(1)
                code = [line]
                continue

            heading = _HEADING.match(line)

            if heading:
                flush_paragraph()
                level = len(heading.group(1))
                title = heading.group(2).strip()

                # Truncate the path to this level, then extend: an h3 under an h1 keeps the h1.
                del path[level - 1 :]
                path.append(title)

                # The heading's own section includes itself, so a chunk that starts at "## Fix"
                # is labelled "Runbook > Fix" rather than with its parent. The label is what the
                # evidence panel shows and what a metadata filter sees; naming the section above
                # the one the chunk is actually in would be wrong in both.
                blocks.append(
                    _Block(line, _section(path), is_heading=True, heading_level=level)
                )
                continue

            if not line.strip():
                flush_paragraph()
                continue

            # A list or table row starts its own paragraph run only when prose precedes it, so a
            # bulleted list stays one block instead of becoming one block per bullet.
            if _LIST_OR_TABLE.match(line) and pending and not _LIST_OR_TABLE.match(pending[-1]):
                flush_paragraph()

            pending.append(line)

        # An unterminated fence is malformed Markdown, not a reason to drop the text.
        if code:
            blocks.append(_Block("\n".join(code), _section(path), is_code=True))

        flush_paragraph()

        return blocks

    def _split_oversized(self, blocks: list[_Block]) -> list[_Block]:
        """Break any single block that is larger than a whole chunk.

        Rare — it takes a 1400-character paragraph or a long code fence — but a block that
        cannot fit would otherwise silently produce a chunk over budget.
        """
        result: list[_Block] = []

        for block in blocks:
            if block.tokens <= self._target:
                result.append(block)
                continue

            pieces = block.text.splitlines() if block.is_code else _sentences(block.text)
            joiner = "\n" if block.is_code else " "
            buffer: list[str] = []

            for piece in pieces:
                candidate = joiner.join([*buffer, piece])

                if buffer and estimate_tokens(candidate) > self._target:
                    result.append(
                        _Block(joiner.join(buffer), block.section, is_code=block.is_code)
                    )
                    buffer = [piece]
                else:
                    buffer.append(piece)

            if buffer:
                result.append(_Block(joiner.join(buffer), block.section, is_code=block.is_code))

        return result

    # ------------------------------------------------------------------ assembly ----

    def _carry_over(self, blocks: list[_Block]) -> list[_Block]:
        """The tail of a finished chunk that the next one repeats.

        Whole blocks from the end, newest first, until the overlap budget is spent. Code fences
        are never carried: repeating a command in two chunks makes it look like it should be run
        twice, and the fence is exactly the kind of self-contained block that needs no context.
        """
        carried: list[_Block] = []
        budget = self._overlap

        for block in reversed(blocks):
            if block.is_code or block.tokens > budget:
                break

            carried.insert(0, block)
            budget -= block.tokens

        return carried

    def _emit(self, document: Document, blocks: list[_Block], index: int) -> Chunk:
        body = "\n\n".join(b.text for b in blocks).strip()

        # The section of the first block, not the last: a chunk that starts under "Diagnosis"
        # and runs into "Fix" is labelled by where it starts, which is where its first sentence
        # belongs.
        section = next((b.section for b in blocks if b.section), None)

        # A chunk that begins with the heading it sits under already carries its context.
        heading_first = blocks[0].is_heading

        if self._prepend_context and not heading_first:
            breadcrumb = f"{document.title} · {section}" if section else document.title
            body = f"{breadcrumb}\n\n{body}"

        return Chunk(
            content=body,
            chunk_index=index,
            token_count=estimate_tokens(body),
            section=section,
            metadata=document.chunk_metadata(),
        )

    @staticmethod
    def _is_redundant(chunk: Chunk, previous: Chunk) -> bool:
        """Whether a chunk says nothing its predecessor did not."""
        return chunk.content.strip() in previous.content


def _section(path: list[str]) -> str | None:
    return " > ".join(path) if path else None


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]

    # A wall of text with no sentence punctuation — a long table row, minified content — has to
    # be broken somewhere; words are the next boundary down.
    return parts if len(parts) > 1 else text.split()
