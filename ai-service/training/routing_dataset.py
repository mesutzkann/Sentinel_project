"""Builds the router's training set: templates in, three JSONL splits out.

    python -m training.routing_dataset                    # deterministic, ~2 seconds
    python -m training.routing_dataset --paraphrase       # plus local-model variants, ~25 min
    python -m training.routing_dataset --report           # what is already on disk

**Every label comes from `routing/plans.py`.** A router's job is the intent and the service;
the flags and the tool list follow from the intent, and the table they follow from is the one the
planner actually acts on. Labelling by hand would mean a training set that could disagree with
the system it is training a component of — and the disagreement would show up as a model that
routes correctly and plans wrongly, which is a very hard thing to see.

**The split is stratified by intent and grouped by template.** Stratified because fifteen
intents at wildly different frequencies would make accuracy a measure of the sampling; grouped
because two fills of one phrasing are the same question with a different service in it, and
letting one land in train and its twin in test measures memorisation and calls it generalisation.

What comes out is *not* balanced across languages by construction — it is balanced across
intents, and the language mix falls out of the templates. That mix is reported rather than
enforced, because the honest question is how the router does on the questions this team asks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from routing.plans import decide
from routing.schema import Intent, RouteDecision
from training.templates import ALL_TEMPLATES, SERVICES, SLOTS, Template

# ai-service/training/routing_dataset.py -> ai-service -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = _REPO_ROOT / "datasets" / "routing"

# Fixed, so the same templates produce the same dataset on any machine. A training set that
# changed under a rerun would make every benchmark number incomparable with the last one.
SEED = 20260911

# Fills per (template, service) for templates that have something left to vary. Three, because a
# fourth mostly repeats a slot value and adds a near-duplicate the deduplicator then throws away.
DEFAULT_VARIANTS = 3

# Share of the deterministic examples that get a typo'd twin. People type badly under pressure,
# and a router that only works on clean input works in a demo. Kept low: past roughly a tenth the
# model starts learning the noise rather than the intent.
TYPO_SHARE = 0.10

SPLITS = (("train", 0.8), ("val", 0.1), ("test", 0.1))


@dataclass(frozen=True, slots=True)
class Example:
    """One training row, flat, with the label's fields at the top level.

    Flat rather than `{"query": ..., "label": {...}}` because the label *is* a
    :class:`routing.schema.RouteDecision`, and a row that can be handed straight to it is one
    fewer shape to keep in step.
    """

    id: str
    query: str
    language: str
    source: str
    template: str
    intent: str
    requires_rag: bool
    requires_mcp: bool
    tools: list[str]
    target_service: str | None

    def decision(self) -> RouteDecision:
        """The label as the router's own type, which is also how it is validated."""
        return RouteDecision(
            intent=Intent(self.intent),
            requires_rag=self.requires_rag,
            requires_mcp=self.requires_mcp,
            tools=self.tools,
            target_service=self.target_service,
        )


def label(template: Template, query: str, service: str | None, source: str, index: int) -> Example:
    decision = decide(template.intent, service)

    return Example(
        id=f"R{index:05d}",
        query=query,
        language=template.language,
        source=source,
        template=template.text,
        intent=decision.intent.value,
        requires_rag=decision.requires_rag,
        requires_mcp=decision.requires_mcp,
        tools=list(decision.tools),
        target_service=decision.target_service,
    )


# ---------------------------------------------------------------------- generation ----


# How a service gets named in a sentence. The label is the same either way, and a router that
# only recognised the bare token would fail on half of what gets typed.
#
# Only where the slot is not carrying a Turkish suffix: "{service}'ta" becomes "orders'ta", and
# substituting "orders servisi" into it would produce "orders servisi'ta", which is not Turkish.
SERVICE_FORMS: dict[str, tuple[str, ...]] = {
    "en": ("{name}", "the {name} service"),
    "tr": ("{name}", "{name} servisi"),
    "mixed": ("{name}", "{name} servisi"),
}


def name_service(text: str, service: str, language: str, rng: random.Random) -> str:
    suffixed = "{service}'" in text
    form = "{name}" if suffixed else rng.choice(SERVICE_FORMS.get(language, ("{name}",)))

    return text.replace("{service}", form.replace("{name}", service))


def fill(text: str, service: str | None, language: str, rng: random.Random) -> str:
    """Substitute the slots, choosing each value independently."""
    if service:
        filled = name_service(text, service, language, rng)
    else:
        filled = text.replace("{service}", "")

    for slot, values in SLOTS.items():
        token = "{" + slot + "}"

        while token in filled:
            filled = filled.replace(token, rng.choice(values), 1)

    return " ".join(filled.split())


def has_free_slots(text: str) -> bool:
    return any("{" + slot + "}" in text for slot in SLOTS)


def expand(variants: int) -> list[Example]:
    """Every template across every service it can name, with the free slots varied."""
    rng = random.Random(SEED)
    examples: list[Example] = []
    seen: set[str] = set()

    for template in ALL_TEMPLATES:
        services: list[str | None] = list(SERVICES) if template.names_service else [None]
        rounds = variants if has_free_slots(template.text) or template.names_service else 1

        for service in services:
            for _ in range(rounds):
                query = fill(template.text, service, template.language, rng)
                key = normalise(query)

                if key in seen:
                    continue

                seen.add(key)
                examples.append(label(template, query, service, "template", len(examples) + 1))

    return examples


# One deliberate slip per query, of the kinds a keyboard actually makes: a doubled letter, a
# dropped one, a transposition, or a Turkish character typed as its ASCII cousin. Not random
# character noise, which is a different problem and one nobody has.
_ASCII_FOLD = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def typo(query: str, rng: random.Random) -> str:
    words = query.split()
    candidates = [i for i, word in enumerate(words) if len(word) > 4]

    if not candidates:
        return query

    index = rng.choice(candidates)
    word = words[index]
    kind = rng.choice(("double", "drop", "swap", "fold"))
    position = rng.randrange(1, len(word) - 1)

    if kind == "double":
        words[index] = word[:position] + word[position] + word[position:]
    elif kind == "drop":
        words[index] = word[:position] + word[position + 1 :]
    elif kind == "swap":
        words[index] = word[:position] + word[position + 1] + word[position] + word[position + 2 :]
    else:
        words[index] = word.translate(_ASCII_FOLD)

    return " ".join(words)


def add_typos(examples: list[Example], share: float) -> list[Example]:
    """Typo'd twins of a sample, kept as extra rows rather than replacing the clean ones."""
    rng = random.Random(SEED + 1)
    sample = rng.sample(examples, k=int(len(examples) * share))
    out: list[Example] = []

    for example in sample:
        mangled = typo(example.query, rng)

        if mangled == example.query:
            continue

        out.append(
            Example(
                **{
                    **asdict(example),
                    "id": f"T{len(out) + 1:05d}",
                    "query": mangled,
                    "source": "typo",
                }
            )
        )

    return out


# ---------------------------------------------------------------------- paraphrase ----

PARAPHRASE_PROMPT = """You are helping build a training set for an incident-response assistant.

Rewrite this question {count} times, as different people would type it in a team chat. Keep the
language it is written in — Turkish stays Turkish, English stays English, and a question that
mixes them stays mixed. Keep every service name, number and error name exactly as they appear.

Do not change what is being asked for. "{query}" asks for {asking}; a rewrite that asks for
anything else is wrong and will mislabel the training set.

Return one rewrite per line, nothing else: no numbering, no quotes, no commentary.

Question: {query}"""

# What each intent is asking for, in a clause the paraphraser can hold on to. Without it a 3B
# rewrites "show me the logs" into "why is it broken", which is a different label.
ASKING: dict[Intent, str] = {
    Intent.FULL_INVESTIGATION: "an investigation into what is wrong",
    Intent.ERROR_ANALYSIS: "what errors are happening and how many",
    Intent.LOG_QUERY: "log lines, not a diagnosis",
    Intent.METRIC_QUERY: "a metric value",
    Intent.TRACE_QUERY: "a trace or spans",
    Intent.PERFORMANCE_ANALYSIS: "why something is slow",
    Intent.DATABASE_HEALTH: "the state of the database",
    Intent.DEPLOYMENT_CHECK: "what was deployed or changed",
    Intent.CODE_LOOKUP: "where something is implemented",
    Intent.CONFIG_LOOKUP: "what a setting is currently set to",
    Intent.SERVICE_TOPOLOGY: "which services call which",
    Intent.HISTORICAL_SIMILARITY: "whether this has happened before",
    Intent.KNOWLEDGE_QUESTION: "documentation or a procedure",
    Intent.REMEDIATION_QUESTION: "what to do about it",
    Intent.GENERAL_QUESTION: "something too vague to plan for",
}


# Where to spend the rewrites, from the Phase 8 test split rather than from a hunch: the tuned
# router scored 0.11 on mixed-language questions against 0.90 on English, 0.13 on
# PERFORMANCE_ANALYSIS and 0.22 on TRACE_QUERY. A seed's weight is the product of whatever it
# matches, so a mixed TRACE_QUERY question is twelve times as likely to be drawn as an English
# question about deployments. Uniform sampling spends three quarters of a 75-minute run on the
# intents that already score 0.90.
PARAPHRASE_WEIGHTS_BY_LANGUAGE: dict[str, float] = {"mixed": 4.0}
PARAPHRASE_WEIGHTS_BY_INTENT: dict[Intent, float] = {
    Intent.TRACE_QUERY: 3.0,
    Intent.PERFORMANCE_ANALYSIS: 3.0,
}

#: One line per *completed seed*, written as the run goes. See :func:`paraphrase`.
PARAPHRASE_CACHE = "paraphrases.jsonl"


def paraphrase_weight(example: Example) -> float:
    return PARAPHRASE_WEIGHTS_BY_LANGUAGE.get(
        example.language, 1.0
    ) * PARAPHRASE_WEIGHTS_BY_INTENT.get(Intent(example.intent), 1.0)


def choose_seeds(examples: list[Example], limit: int) -> list[Example]:
    """Weighted sampling without replacement, deterministic given :data:`SEED`.

    Efraimidis-Spirakis: give each row the key ``random() ** (1 / weight)`` and take the largest
    keys. Drawing with replacement instead would hand the same question to the model twice, and
    the second answer would be thrown away by the deduplicator after paying for it.
    """
    rng = random.Random(SEED + 2)
    keyed = sorted(
        (rng.random() ** (1.0 / paraphrase_weight(example)), example) for example in examples
    )

    return [example for _, example in keyed[-min(limit, len(examples)) :]]


async def paraphrase(
    examples: list[Example],
    per_query: int,
    model: str,
    limit: int,
    cache: Path | None = None,
) -> list[Example]:
    """Ask the local model to rewrite a sample of the queries.

    Rewrites carry the *seed's* label. That is the whole risk of this step: a paraphrase that
    drifts into another intent is a mislabelled row, and mislabelled rows are worse than no rows.
    The prompt pins what the question asks for, and anything that comes back empty, too long, or
    identical to its seed is dropped rather than kept and hoped over.

    **The run writes as it goes.** A seed's accepted rewrites are appended to ``cache`` as one
    JSON line the moment they are accepted, and a rerun skips every seed already in there. This
    step is an hour of local generation; holding all of it in memory until the end meant that
    closing a laptop lid, or a single Ctrl-C when the progress line looked stalled, cost the
    whole hour. Delete the cache file to start over, and note it is keyed by seed *and model* —
    rewrites from a different paraphraser are not reused.
    """
    from app.config import settings
    from llm.base import LlmMessage, LlmOptions
    from llm.ollama_provider import OllamaLlmProvider

    seeds = choose_seeds(examples, limit)
    done, out = read_paraphrase_cache(cache, model)

    if done:
        print(f"  resuming: {len(done)} seeds already done, {len(out)} rows", file=sys.stderr)

    provider = OllamaLlmProvider(settings().ollama_base_url, model, timeout_seconds=180)
    seen = {normalise(example.query) for example in examples}
    seen |= {normalise(example.query) for example in out}

    for position, seed in enumerate(seeds, start=1):
        if seed.id in done:
            continue

        prompt = PARAPHRASE_PROMPT.format(
            count=per_query,
            query=seed.query,
            asking=ASKING[Intent(seed.intent)],
        )

        try:
            completion = await provider.complete(
                [LlmMessage(role="user", content=prompt)],
                LlmOptions(temperature=0.8),
            )
        except Exception as exc:  # noqa: BLE001 - one bad call must not lose the run
            print(f"  {seed.id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        accepted: list[Example] = []

        for line in completion.text.splitlines():
            candidate = clean_paraphrase(line)
            key = normalise(candidate)

            if not candidate or key in seen or not keeps_the_question(seed, candidate):
                continue

            seen.add(key)
            accepted.append(
                Example(**{**asdict(seed), "query": candidate, "source": "paraphrase"})
            )

        out += accepted
        append_paraphrase_cache(cache, model, seed, accepted)

        if position % 25 == 0:
            print(
                f"  paraphrased {position}/{len(seeds)} seeds -> {len(out)} rows",
                file=sys.stderr,
            )

    # Numbered at the end rather than as they arrive, so a resumed run and a run that went
    # through in one go produce the same ids for the same rows.
    return [
        Example(**{**asdict(row), "id": f"P{number:05d}"})
        for number, row in enumerate(out, start=1)
    ]


def read_paraphrase_cache(cache: Path | None, model: str) -> tuple[set[str], list[Example]]:
    """The seeds already rewritten by this model, and the rows they produced."""
    if cache is None or not cache.exists():
        return set(), []

    done: set[str] = set()
    rows: list[Example] = []

    for line in cache.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue

        entry = json.loads(line)

        if entry.get("model") != model:
            continue

        done.add(entry["seed"])
        rows += [Example(**row) for row in entry["rows"]]

    return done, rows


def append_paraphrase_cache(
    cache: Path | None, model: str, seed: Example, rows: list[Example]
) -> None:
    """One line per seed, flushed. A seed that yielded nothing is still recorded as done.

    Recording the empty ones is the point of writing per seed rather than per row: without it a
    resumed run would ask the model again about every question whose rewrites all drifted, which
    is the slowest third of the run.
    """
    if cache is None:
        return

    cache.parent.mkdir(parents=True, exist_ok=True)
    entry = {"seed": seed.id, "model": model, "rows": [asdict(row) for row in rows]}

    with cache.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()


# Turkish that survives an ASCII keyboard: the letters, and the function words that appear in
# almost any Turkish question. Either is enough to call a line Turkish.
_TURKISH_LETTERS = frozenset("çğıöşüÇĞİÖŞÜ")
_TURKISH_WORDS = frozenset(
    {"mi", "mı", "mu", "mü", "ne", "neden", "nasıl", "nasil", "için", "icin", "var", "yok",
     "kaç", "kac", "bir", "son", "bu", "ve", "hangi", "göster", "goster"}
)


def keeps_the_question(seed: Example, candidate: str) -> bool:
    """Whether a rewrite still asks the seed's question, checked mechanically rather than hoped.

    A rewrite inherits its seed's label, so one that drifts is a mislabelled row — and the 7B
    does drift, measured: asked to rewrite "was orders touched before this started" it returned
    Turkish, and returned it about conditional payments rather than about orders. Two cheap
    checks catch both, and catching them here is cheaper than finding a wrong label later in a
    confusion matrix.
    """
    lowered = candidate.casefold()

    # The service is the half of the label a rewrite is most likely to lose.
    if seed.target_service and seed.target_service not in lowered:
        return False

    turkish = any(letter in candidate for letter in _TURKISH_LETTERS) or bool(
        _TURKISH_WORDS & set(lowered.split())
    )

    if seed.language == "en" and turkish:
        return False

    return not (seed.language == "tr" and not turkish)


_LEADING_NUMBER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def clean_paraphrase(line: str) -> str:
    """Strip the numbering and quoting a model adds however firmly it was asked not to."""
    candidate = _LEADING_NUMBER.sub("", line).strip().strip('"').strip("'").strip()

    # A rewrite that is one word is a fragment, and one that is three lines long is commentary.
    if len(candidate) < 8 or len(candidate) > 160 or candidate.endswith(":"):
        return ""

    return candidate


# ---------------------------------------------------------------------- splitting ----


def normalise(query: str) -> str:
    """For deduplication: case, punctuation and Turkish diacritics folded away.

    Folding the diacritics means "hata" and "hatâ" count as the same row, which is the point —
    two rows that differ only in how somebody typed an ı are not two training examples.
    """
    folded = unicodedata.normalize("NFKD", query.casefold().translate(_ASCII_FOLD))

    return re.sub(r"[^a-z0-9 ]+", " ", folded).strip()


def split(examples: list[Example]) -> dict[str, list[Example]]:
    """Stratified by intent, grouped by template, deterministic.

    The grouping is the part that matters. Every fill of one phrasing goes to the same split, so
    the test set contains phrasings the model has never seen rather than services it has never
    seen in a phrasing it knows by heart. Validation and test are filled *first*, because an
    intent with three phrasings has to give one to each of them and the remainder to training —
    filling training first would leave the thin intents with no test row at all.
    """
    rng = random.Random(SEED + 3)
    by_intent: dict[str, dict[str, list[Example]]] = defaultdict(lambda: defaultdict(list))

    for example in examples:
        by_intent[example.intent][example.template].append(example)

    out: dict[str, list[Example]] = {name: [] for name, _ in SPLITS}

    for position, intent in enumerate(sorted(by_intent)):
        groups = sorted(by_intent[intent].items())
        rng.shuffle(groups)
        total = sum(len(rows) for _, rows in groups)
        targets = {name: share * total for name, share in SPLITS}
        counts = dict.fromkeys(targets, 0)

        # Phrasings reserved before anything else: for every intent **and every language**, one
        # phrasing for test, plus one for validation in one language per intent, rotating.
        #
        # Reserving per intent alone was not enough. Phase 8 measured mixed-language accuracy at
        # 0.11 and the test split turned out to hold eleven mixed rows from two intents — a
        # number too thin to mean anything, hiding a model that had barely been trained on mixed
        # at all. A language the benchmark cannot report on is a language nobody fixes. The cost
        # is that test runs a little over its tenth, which is the right thing to spend it on.
        reserved: set[str] = set()
        languages = sorted({row.language for rows in by_intent[intent].values() for row in rows})

        for offset, language in enumerate(languages):
            floors = ["test", "val"] if offset == position % len(languages) else ["test"]
            candidates = [
                (template, rows) for template, rows in groups if rows[0].language == language
            ]

            for name, (template, rows) in zip(floors, candidates, strict=False):
                reserved.add(template)
                counts[name] += len(rows)
                out[name].extend(rows)

        # The rest goes wherever the shortfall is largest. Whole groups are indivisible, so the
        # shares land near the targets rather than on them — an intent with eleven phrasings
        # cannot be cut finer, and rounding by splitting one would put the same question on both
        # sides of the measurement.
        for template, rows in groups:
            if template in reserved:
                continue

            target = max(targets, key=lambda name: targets[name] - counts[name])
            counts[target] += len(rows)
            out[target].extend(rows)

    return out


# ---------------------------------------------------------------------- output ----


def write(splits: dict[str, list[Example]], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)

    for name, rows in splits.items():
        path = directory / f"{name}.jsonl"

        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


def load(directory: Path, name: str) -> list[Example]:
    path = directory / f"{name}.jsonl"

    if not path.exists():
        return []

    return [
        Example(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def report(splits: dict[str, list[Example]]) -> str:
    everything = [row for rows in splits.values() for row in rows]
    by_intent = Counter(row.intent for row in everything)
    by_language = Counter(row.language for row in everything)
    by_source = Counter(row.source for row in everything)

    lines = [
        "",
        f"{len(everything)} examples: "
        + ", ".join(f"{name} {len(rows)}" for name, rows in splits.items()),
        "",
        "| intent | total | train | val | test |",
        "|---|---|---|---|---|",
    ]

    for intent in sorted(by_intent):
        counts = {name: sum(1 for r in rows if r.intent == intent) for name, rows in splits.items()}
        lines.append(
            f"| {intent} | {by_intent[intent]} | "
            + " | ".join(str(counts[name]) for name, _ in SPLITS)
            + " |"
        )

    lines += [
        "",
        "language: " + ", ".join(f"{k} {v}" for k, v in sorted(by_language.items())),
        "source:   " + ", ".join(f"{k} {v}" for k, v in sorted(by_source.items())),
    ]

    return "\n".join(lines)


def validate(examples: list[Example]) -> None:
    """Every row has to be a label the router's own type accepts, and a query somebody could ask."""
    for example in examples:
        example.decision()

        if not example.query.strip():
            raise ValueError(f"{example.id} has an empty query")

        if "{" in example.query:
            raise ValueError(f"{example.id} has an unfilled slot: {example.query}")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--variants", type=int, default=DEFAULT_VARIANTS)
    parser.add_argument("--typo-share", type=float, default=TYPO_SHARE)
    parser.add_argument(
        "--paraphrase",
        action="store_true",
        help="also ask the local model to rewrite a sample of the queries",
    )
    parser.add_argument("--paraphrase-model", default="qwen2.5:7b-instruct")
    parser.add_argument("--paraphrase-seeds", type=int, default=420)
    parser.add_argument("--paraphrase-each", type=int, default=4)
    parser.add_argument(
        "--paraphrase-cache",
        type=Path,
        default=None,
        help=f"where rewrites are written as they arrive (default: <output>/{PARAPHRASE_CACHE}); "
        "a rerun skips the seeds already in it",
    )
    parser.add_argument(
        "--fresh-paraphrases",
        action="store_true",
        help="ignore the cache and ask the model about every seed again",
    )
    parser.add_argument("--report", action="store_true", help="report what is on disk and stop")
    args = parser.parse_args(argv)

    if args.report:
        splits = {name: load(args.output, name) for name, _ in SPLITS}

        if not any(splits.values()):
            print(f"Nothing in {args.output}", file=sys.stderr)
            return 1

        print(report(splits))
        return 0

    examples = expand(args.variants)
    print(f"{len(examples)} from templates", file=sys.stderr)

    if args.paraphrase:
        cache = args.paraphrase_cache or args.output / PARAPHRASE_CACHE

        if args.fresh_paraphrases and cache.exists():
            print(f"discarding {cache}", file=sys.stderr)
            cache.unlink()

        rewritten = await paraphrase(
            examples,
            args.paraphrase_each,
            args.paraphrase_model,
            args.paraphrase_seeds,
            cache=cache,
        )
        print(f"{len(rewritten)} from paraphrasing", file=sys.stderr)
        examples += rewritten

    typos = add_typos(examples, args.typo_share)
    print(f"{len(typos)} typo'd twins", file=sys.stderr)
    examples += typos

    validate(examples)
    splits = split(examples)
    write(splits, args.output)

    print(f"Wrote {args.output}", file=sys.stderr)
    print(report(splits))

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
