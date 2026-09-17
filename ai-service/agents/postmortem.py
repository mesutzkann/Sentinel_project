"""Turning a finished investigation into something the next one can find.

This is the whole of Phase 9's claim: an incident that has been investigated becomes a document
in the knowledge base, so the next incident that looks like it retrieves it as history rather
than starting from nothing. The ten seed incidents in `datasets/knowledge/incidents/` are what
that looks like when a human writes it; this writes the same shape from a run.

**The model writes the prose and none of the facts.** The root cause, the evidence, the
recommended actions, the confidence and the timeline all come from the investigation and are
rendered straight into the document. What the model is asked for is the summary, the description
of the signals, the lessons and the prevention list — the parts that are judgement about what
happened rather than records of it. That division is the same one `routing/plans.py` draws for
the router: a model that could invent a timeline would produce a document indistinguishable from
a real one and wrong, and it would be indexed next to nine that are right.

**A run with no conclusion produces no postmortem.** An investigation that ended in NEEDS_HUMAN
has, by construction, a conclusion nobody stood behind; writing it into the knowledge base would
make the next investigation retrieve an uncertainty as a precedent. :func:`PostmortemWriter.write`
returns ``None`` and says why.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agents.context import InvestigationContext
from agents.nodes.reasoning import DEFAULT_MAX_ATTEMPTS, render_evidence, render_incident
from llm.base import LlmMessage, LlmOptions, LocalLlmProvider
from llm.prompts import PromptRegistry, registry
from llm.structured import (
    StructuredAttempt,
    StructuredOutputError,
    generate_structured,
    json_schema_hint,
)
from rag.documents import Document, SourceType
from reporting.predictions import ModelPurpose, PredictionRecord, PredictionReporter

logger = logging.getLogger(__name__)

# Lessons and prevention items. Four is the point past which a postmortem stops being read: the
# seed corpus averages three of each, and a list of nine is a list nobody acts on.
MAX_LIST_ITEMS = 4

#: Where a generated postmortem is written, relative to the knowledge base root. Its own
#: directory rather than `incidents/`, so a corpus that mixes written and generated documents can
#: always say which is which — the front matter says so too, but a directory is visible in a
#: `ls` and in a diff.
GENERATED_DIR = "postmortems"


class PostmortemDraft(BaseModel):
    """The parts of a postmortem that are judgement rather than record.

    **No character limits on the prose fields**, and not as an oversight. `minLength` and
    `maxLength` become repetition rules in llama.cpp's grammar, and a schema carrying two of them
    at this size is rejected outright — "Failed to initialize samplers: failed to parse grammar",
    a 400 from Ollama that says nothing about which field caused it. Each field passes alone;
    together they do not. Length is asked for in the prompt instead, where it belongs: the rest
    of `agents/schemas.py` constrains lists and never strings, for the same reason.
    """

    summary: str = Field(
        description=(
            "Two to four sentences: what broke, what users saw, what turned out to be wrong. "
            "Lead with the symptom — it is what the next person searches for."
        ),
    )

    what_we_saw: str = Field(
        description=(
            "One paragraph on the signals, in the words they appear in. Quote the decisive log "
            "line or error code when the evidence carries one."
        ),
    )

    lessons: list[str] = Field(
        min_length=1,
        max_length=MAX_LIST_ITEMS,
        description="What this incident taught about this system, not about incident response.",
    )

    prevention: list[str] = Field(
        default_factory=list,
        max_length=MAX_LIST_ITEMS,
        description="Concrete, checkable steps: a limit to set, an alert to add, a path to fix.",
    )

    @field_validator("lessons", "prevention", mode="after")
    @classmethod
    def _unnumber(cls, items: list[str]) -> list[str]:
        """Strip the numbering a model adds however firmly the prompt asks it not to.

        These are rendered into a Markdown list, so a model's own "1." arrives as "- 1. ..." on
        the page. Measured on the 3B: it numbers every item, every time. Repairing it here rather
        than rejecting the answer costs nothing and keeps a usable document; the same trade the
        routing dataset's `clean_paraphrase` makes.
        """
        return [_LIST_NUMBER.sub("", item).strip() for item in items if item.strip()]


@dataclass(frozen=True, slots=True)
class Postmortem:
    """A written-up investigation, ready to be stored and ingested."""

    incident_code: str
    title: str
    markdown: str
    structured: dict[str, Any]
    document: Document
    filename: str

    #: What the model cost to write it, for `model_predictions`.
    usage: dict[str, int] = field(default_factory=dict)


class PostmortemWriter:
    """Writes the postmortem for one finished investigation."""

    prompt_name = "postmortem"
    default_prompt_version = "v1"

    def __init__(
        self,
        provider: LocalLlmProvider,
        *,
        prompt_version: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        prompts: PromptRegistry | None = None,
        reporter: PredictionReporter | None = None,
    ) -> None:
        self._provider = provider
        self._prompt_version = prompt_version or self.default_prompt_version
        self._max_attempts = max_attempts
        self._prompts = prompts or registry()
        self._reporter = reporter

    @property
    def prompt_id(self) -> str:
        return f"{self.prompt_name}.{self._prompt_version}"

    async def write(
        self,
        ctx: InvestigationContext,
        *,
        duration_ms: int | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> Postmortem | None:
        """The postmortem for this run, or ``None`` when there is nothing to remember.

        Raises nothing. A knowledge base that gained a document is better than one that did not,
        and neither is worth failing an investigation that has already produced its answer — so a
        model that will not produce the object is logged and the run ends as it would have.
        """
        if ctx.root_cause is None:
            logger.info(
                "%s reached no root cause; no postmortem written", ctx.incident_code
            )

            return None

        rendered = self._prompts.get(self.prompt_name, self._prompt_version).render(
            schema=json_schema_hint(PostmortemDraft),
            incident=render_incident(ctx),
            root_cause=_render_root_cause(ctx),
            evidence=render_evidence(ctx),
            recommendations=_render_recommendations(ctx),
        )

        try:
            result = await generate_structured(
                provider=self._provider,
                schema=PostmortemDraft,
                messages=[LlmMessage(role="user", content=rendered)],
                options=LlmOptions(),
                max_attempts=self._max_attempts,
            )
        except StructuredOutputError as exc:
            logger.warning(
                "%s: the model produced no usable postmortem after %d attempt(s)",
                ctx.incident_code,
                len(exc.attempts),
            )
            await self._report(
                ctx,
                exc.attempts,
                valid_json=False,
                output=exc.attempts[-1].completion.text if exc.attempts else None,
            )

            return None

        await self._report(
            ctx,
            result.attempts,
            valid_json=True,
            output=result.value.model_dump_json(),
        )

        return build(
            ctx,
            result.value,
            duration_ms=duration_ms,
            events=events,
            usage={
                "prompt_tokens": result.total_prompt_tokens,
                "completion_tokens": result.total_completion_tokens,
                "latency_ms": result.total_latency_ms,
            },
        )

    async def _report(
        self,
        ctx: InvestigationContext,
        attempts: list[StructuredAttempt],
        *,
        valid_json: bool,
        output: str | None,
    ) -> None:
        """Record the write-up call under POSTMORTEM.

        Its own purpose rather than REASONING, because this call is not part of the
        investigation: it happens after the run has ended and its cost is what the memory in
        Phase 9 charges per incident. Folding it into reasoning would make every run look more
        expensive to think through than it was.
        """
        if self._reporter is None or not attempts:
            return

        await self._reporter.record(
            PredictionRecord.from_attempts(
                attempts,
                purpose=ModelPurpose.POSTMORTEM,
                valid_json=valid_json,
                output=output,
                investigation_id=ctx.investigation_id,
            )
        )


def build(
    ctx: InvestigationContext,
    draft: PostmortemDraft,
    *,
    duration_ms: int | None = None,
    events: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
    now: datetime | None = None,
) -> Postmortem:
    """Assemble the document. Separated from the model call so it can be tested without one."""
    root_cause = ctx.root_cause
    assert root_cause is not None  # noqa: S101 - guarded by the caller; a run without one is None

    title = f"{ctx.incident_code} — {root_cause.title}"
    date = (now or datetime.now(UTC)).date().isoformat()
    service = ctx.target_service

    front_matter = {
        "type": SourceType.POSTMORTEM.value,
        "title": title,
        "service": service,
        "id": ctx.incident_code,
        "scenario": root_cause.category,
        "date": date,
        "confidence": f"{root_cause.confidence:.2f}",
        "investigation_minutes": _minutes(duration_ms),
        # Said in the document rather than only in a directory name. A reader who finds this by
        # search rather than by browsing still has to be able to tell it from one a human wrote.
        "source": "generated",
    }

    # Two forms of the same document, and the difference matters. `markdown` is the file: front
    # matter and body, which is what gets written to the corpus. `content` is what the loader
    # returns for that file — the body alone — and it is what gets embedded. Handing the store
    # the file instead would give the same document two content hashes, one from this path and
    # one from the next corpus walk, and it would be re-embedded for no reason on every ingest.
    body = _render_body(ctx, draft, title, events=events).strip()
    markdown = _render_front_matter(front_matter) + body + "\n"
    filename = f"{GENERATED_DIR}/{_slug(ctx.incident_code, root_cause.title)}.md"

    document = Document(
        source_type=SourceType.POSTMORTEM,
        title=title,
        content=body,
        service=service,
        external_id=ctx.incident_code,
        path=filename,
        metadata={
            key: str(value)
            for key, value in front_matter.items()
            if key not in {"type", "title", "service", "id"} and value is not None
        },
    )

    return Postmortem(
        incident_code=ctx.incident_code,
        title=title,
        markdown=markdown,
        structured={
            **draft.model_dump(),
            "root_cause": root_cause.title,
            "category": root_cause.category,
            "confidence": root_cause.confidence,
            "evidence_count": len(ctx.evidence),
            "recommendations": [r.action_code for r in ctx.recommendations],
        },
        document=document,
        filename=filename,
        usage=usage or {},
    )


# ------------------------------------------------------------------------ rendering ----


def _render_front_matter(front_matter: dict[str, Any]) -> str:
    """The fenced `key: value` block the corpus loader reads. Keys with no value are left out."""
    lines = ["---"]
    lines += [f"{key}: {value}" for key, value in front_matter.items() if value is not None]
    lines += ["---", ""]

    return "\n".join(lines)


def _render_body(
    ctx: InvestigationContext,
    draft: PostmortemDraft,
    title: str,
    *,
    events: list[dict[str, Any]] | None,
) -> str:
    """The document, in the shape the seed corpus uses.

    Shape matters more than it looks. The chunker splits on headings, so the sections here are
    what a retrieval hit is a hit *on*: "## What we saw" holds the symptoms somebody will search
    with, and burying them inside a longer section would make the match land on a chunk that
    also carries the conclusion — which reads, to the next investigation, as the answer arriving
    before the question.
    """
    root_cause = ctx.root_cause
    assert root_cause is not None  # noqa: S101 - same guard as build()

    lines = [f"# {title}", "", "## Summary", "", draft.summary.strip()]
    lines += ["", "## What we saw", "", draft.what_we_saw.strip()]

    timeline = _render_timeline(events)

    if timeline:
        lines += ["", "## Timeline", "", *timeline]

    lines += ["", "## Root cause", "", root_cause.explanation.strip()]

    if root_cause.validator_output:
        verdict = root_cause.validator_output.get("verdict")

        if verdict:
            lines += ["", f"The critic's verdict on this conclusion: {verdict}."]

    lines += ["", "## Evidence", ""]
    lines += _render_evidence_list(ctx)

    if ctx.recommendations:
        lines += ["", "## Recommended fix", ""]
        lines += [
            f"- **{r.action_code}** — {r.description}"
            + (f" (`{r.tool_name}`)" if r.tool_name else "")
            for r in ctx.recommendations
        ]

    lines += ["", "## Lessons", ""]
    lines += [f"- {lesson}" for lesson in draft.lessons]

    if draft.prevention:
        lines += ["", "## Prevention", ""]
        lines += [f"- {item}" for item in draft.prevention]

    lines += [
        "",
        "---",
        "",
        f"Written by the SentinelAI investigation agent from investigation "
        f"`{ctx.investigation_id}`, at confidence {root_cause.confidence:.2f} over "
        f"{len(ctx.evidence)} piece(s) of evidence. Not reviewed by a human.",
        "",
    ]

    return "\n".join(lines)


def _render_evidence_list(ctx: InvestigationContext) -> list[str]:
    """Every fact, with its index, so a claim in this document can be traced to the run.

    All of it, not the top few: the numbers are the same indices the hypotheses cited and the
    confidence score counted, and a document that renumbered them would break the only link
    between a conclusion and the bytes behind it.
    """
    if not ctx.evidence:
        return ["(the investigation collected no evidence)"]

    return [
        f"- `[{index}]` ({item.source}, weight {item.weight:.2f})"
        + (f" via `{item.tool}`" if item.tool else "")
        + f" — {item.summary}"
        for index, item in enumerate(ctx.evidence)
    ]


def _render_timeline(events: list[dict[str, Any]] | None) -> list[str]:
    """The states the run passed through, with the times they were recorded.

    From the emitted events rather than from the model. This is the section a model would be
    most willing to invent and least able to get right, and it is the one a reader trusts most.
    """
    if not events:
        return []

    rows = [
        f"| {_clock(event.get('timestamp'))} | {event.get('state', '')} "
        f"| {event.get('message', '')} |"
        for event in events
        if event.get("type") == "step_completed"
    ]

    if not rows:
        return []

    return ["| Time | State | What happened |", "|---|---|---|", *rows]


def _render_root_cause(ctx: InvestigationContext) -> str:
    root_cause = ctx.root_cause

    if root_cause is None:
        return "(none)"

    category = root_cause.category or "uncategorised"

    return (
        f"{root_cause.title} [{category}], confidence {root_cause.confidence:.2f}\n"
        f"{root_cause.explanation}"
    )


def _render_recommendations(ctx: InvestigationContext) -> str:
    if not ctx.recommendations:
        return "(none proposed)"

    return "\n".join(f"- {r.action_code}: {r.description}" for r in ctx.recommendations)


# -------------------------------------------------------------------------- helpers ----


def _minutes(duration_ms: int | None) -> int | None:
    if duration_ms is None:
        return None

    # Rounded up: an investigation that took forty seconds took a minute, and a postmortem that
    # says it took zero reads as a document with a broken field.
    return max(1, round(duration_ms / 60_000))


def _clock(timestamp: str | None) -> str:
    """``HH:MM`` from an ISO timestamp, or the timestamp if it is not one."""
    if not timestamp:
        return ""

    try:
        return datetime.fromisoformat(timestamp).strftime("%H:%M")
    except ValueError:
        return timestamp


#: Leading "1.", "2)", "-" or "*" on a list item the model wrote as Markdown.
_LIST_NUMBER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slug(incident_code: str, title: str) -> str:
    """``INC-00042-connection-pool-exhausted``, matching the seed corpus's filenames."""
    slug = _SLUG_STRIP.sub("-", title.casefold()).strip("-")

    return f"{incident_code}-{slug[:60].rstrip('-')}" if slug else incident_code
