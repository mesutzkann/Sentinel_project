"""Routing by keyword, in two languages, with no model involved.

Rules are a poor classifier and a very good baseline. They are here for three jobs:

* Phase 7 needs *a* router so the agent can be built and the state machine exercised before any
  fine-tuning exists.
* Phase 8 needs something to beat. A tuned model that cannot outscore a keyword table is not
  worth 3.000 training examples, and without this the comparison would be against nothing.
* The shipped system needs a fallback. When the 1.5B returns JSON that will not parse after
  every retry, something still has to answer, and answering badly beats failing the run.

Two things it does deliberately badly, because pretending otherwise would flatter the baseline:

*It does not guess.* No keyword match means ``GENERAL_QUESTION``, which makes the planner
collect broadly. A rule table that reaches for the nearest intent produces a confident wrong
plan, and the agent then reasons over evidence that was never relevant.

*It reads Turkish and English the same way.* Both keyword sets map to the same intent, and no
attempt is made to detect the language first. The corpus is English and the questions are often
Turkish (see the ``cross_lingual`` split of the retrieval benchmark), so a router that only
worked in one of them would fail on the queries this project exists to handle.
"""

from __future__ import annotations

import re
import unicodedata

from routing.base import Router
from routing.schema import Intent, RouteDecision

# The five sample services. Matched as whole words so "orders" in "reorders" does not count.
KNOWN_SERVICES = ("gateway", "users", "orders", "payments", "notifications")

# Intent -> the terms that imply it, in the order they are tested. Order is the tie-breaker and
# it is not alphabetical: the specific intents come first, so "why is orders slow and erroring"
# lands on FULL_INVESTIGATION rather than on whichever single signal matched first.
_RULES: tuple[tuple[Intent, tuple[str, ...]], ...] = (
    (
        Intent.FULL_INVESTIGATION,
        (
            "investigate", "root cause", "what is wrong", "what's wrong", "find out why",
            "diagnose", "arastir", "araştır", "incele", "kok neden", "kök neden",
            "sorun ne", "neden bozuldu", "neyi var",
        ),
    ),
    (
        Intent.HISTORICAL_SIMILARITY,
        (
            "before", "previously", "past incident", "seen this", "happened again",
            "daha once", "daha önce", "gecmiste", "geçmişte", "benzer", "yasadik mi",
            "yaşadık mı",
        ),
    ),
    (
        Intent.DEPLOYMENT_CHECK,
        (
            "deploy", "deployment", "commit", "released", "release", "what changed",
            "rollback", "revert", "ne degisti", "ne değişti", "surum", "sürüm",
        ),
    ),
    (
        Intent.DATABASE_HEALTH,
        (
            "database", "deadlock", "connection pool", "slow quer", "lock", "index",
            "veritabani", "veritabanı", "kilitlenme", "baglanti havuzu", "bağlantı havuzu",
            "yavas sorgu", "yavaş sorgu",
        ),
    ),
    (
        Intent.SERVICE_TOPOLOGY,
        (
            "depends on", "dependency", "dependencies", "calls which", "which service calls",
            "topology", "bagimlilik", "bağımlılık", "hangi servis", "kim cagiriyor",
            "kim çağırıyor",
        ),
    ),
    (
        Intent.CONFIG_LOOKUP,
        (
            "configured", "configuration", "setting", "env var", "environment variable",
            "set to", "ayar", "yapilandirma", "yapılandırma", "kac olarak", "kaç olarak",
        ),
    ),
    (
        Intent.CODE_LOOKUP,
        (
            "code", "function", "class", "implemented", "where is", "source",
            "kod", "fonksiyon", "sinif", "sınıf", "nerede tanimli", "nerede tanımlı",
        ),
    ),
    (
        Intent.REMEDIATION_QUESTION,
        (
            "how do i fix", "how to fix", "remediate", "resolve this", "what should i do",
            "nasil duzeltir", "nasıl düzeltir", "ne yapmaliyim", "ne yapmalıyım", "cozum",
            "çözüm",
        ),
    ),
    (
        # Explicit documentation words only. The interrogative phrasings that used to live here
        # ("what is", "how do i") are in _DEFINITIONAL now, because as topic rules they matched
        # "what is the p99 latency of gateway" and turned a live metric question into a lookup.
        Intent.KNOWLEDGE_QUESTION,
        ("runbook", "document", "dokuman", "doküman", "prosedur", "prosedür"),
    ),
    (
        Intent.PERFORMANCE_ANALYSIS,
        (
            "slow", "latency", "p95", "p99", "response time", "timeout", "throughput",
            "yavas", "yavaş", "gecikme", "yanit suresi", "yanıt süresi", "zaman asimi",
            "zaman aşımı",
        ),
    ),
    (
        Intent.TRACE_QUERY,
        ("trace", "span", "jaeger", "iz", "izleme", "span'ler"),
    ),
    (
        Intent.METRIC_QUERY,
        (
            "metric", "cpu", "memory", "prometheus", "promql", "request rate", "gauge",
            "metrik", "bellek", "islemci", "işlemci",
        ),
    ),
    (
        Intent.LOG_QUERY,
        ("log", "logs", "loki", "logql", "loglar", "kayit", "kayıt"),
    ),
    (
        Intent.ERROR_ANALYSIS,
        (
            "error", "errors", "exception", "failing", "failure", "crash", "stack trace",
            "hata", "hatalar", "istisna", "coku", "çöktü", "patliyor", "patlıyor",
        ),
    ),
)

# Intent -> (requires_rag, requires_mcp, tools). The tools are the ones the scenario table in
# sample-services/chaos/scenarios.md lists under "Expected tools" for failures of that shape,
# which is what makes this table checkable against something rather than invented.
_PLANS: dict[Intent, tuple[bool, bool, tuple[str, ...]]] = {
    Intent.FULL_INVESTIGATION: (
        True,
        True,
        (
            "logs-mcp/get_recent_errors",
            "metrics-mcp/get_error_rate",
            "metrics-mcp/get_response_time",
            "traces-mcp/get_failed_traces",
            "git-mcp/get_recent_commits",
        ),
    ),
    Intent.ERROR_ANALYSIS: (
        True,
        True,
        ("logs-mcp/get_recent_errors", "logs-mcp/get_exception_statistics"),
    ),
    Intent.LOG_QUERY: (False, True, ("logs-mcp/get_service_logs", "logs-mcp/search_logs")),
    Intent.METRIC_QUERY: (False, True, ("metrics-mcp/get_service_metrics",)),
    Intent.TRACE_QUERY: (False, True, ("traces-mcp/get_recent_traces",)),
    Intent.PERFORMANCE_ANALYSIS: (
        True,
        True,
        ("metrics-mcp/get_response_time", "traces-mcp/get_slowest_spans"),
    ),
    Intent.DATABASE_HEALTH: (
        True,
        True,
        (
            "database-mcp/get_connection_count",
            "database-mcp/get_slow_queries",
            "database-mcp/get_locks_and_deadlocks",
        ),
    ),
    Intent.DEPLOYMENT_CHECK: (False, True, ("git-mcp/get_recent_commits",)),
    Intent.CODE_LOOKUP: (True, True, ("source-code-mcp/search_code", "source-code-mcp/read_file")),
    Intent.CONFIG_LOOKUP: (True, True, ("source-code-mcp/search_code",)),
    Intent.SERVICE_TOPOLOGY: (True, True, ("traces-mcp/get_service_dependencies",)),
    # No live signal: both are answered from the knowledge base alone.
    Intent.HISTORICAL_SIMILARITY: (True, False, ()),
    Intent.KNOWLEDGE_QUESTION: (True, False, ()),
    Intent.REMEDIATION_QUESTION: (True, False, ()),
    # Unknown shape, so collect broadly and let the evidence decide.
    Intent.GENERAL_QUESTION: (
        True,
        True,
        ("logs-mcp/get_recent_errors", "metrics-mcp/get_service_metrics"),
    ),
}


class RuleBasedRouter(Router):
    """Keyword matching over a normalised query."""

    @property
    def name(self) -> str:
        return "rule"

    async def route(self, query: str, *, service_hint: str | None = None) -> RouteDecision:
        normalised = _normalise(query)
        named = _named_services(normalised)
        intent = _classify(normalised, named)
        requires_rag, requires_mcp, tools = _PLANS[intent]

        return RouteDecision(
            intent=intent,
            requires_rag=requires_rag,
            requires_mcp=requires_mcp,
            tools=list(tools),
            target_service=_service(named, service_hint),
        )


# Turkish letters NFKD does not take apart. The rest — ş, ç, ğ, ö, ü — decompose to an ASCII
# letter plus a combining mark and are handled by stripping the marks; dotless ı and ı-less İ do
# not, so they are mapped explicitly. Without this, "araştır" normalises to "arastır" and does
# not match the rule written as "arastir", which is the whole point of normalising.
_TURKISH_FOLD = str.maketrans({"ı": "i", "İ": "i"})


def _normalise(query: str) -> str:
    """Lowercase, and fold Turkish down to ASCII so "araştır" matches "arastir".

    Deliberately one-directional: the rules are written unaccented and both spellings appear in
    them anyway, because a user typing on an English keyboard writes "arastir" and one on a
    Turkish keyboard writes "araştır", and neither should route differently from the other.
    """
    folded = query.translate(_TURKISH_FOLD).casefold()
    decomposed = unicodedata.normalize("NFKD", folded)

    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# Markers that make a question definitional or procedural rather than operational: it is asking
# what a thing *is* or how one *does* it, not what a running system is doing right now.
_DEFINITIONAL = (
    "what is a", "what is an", "what does", "what do you mean", "how do i", "how to",
    "explain", "definition", "nedir", "ne demek", "acikla", "nasil yapilir", "nasil",
)

_REMEDIAL = (
    "how do i fix", "how to fix", "remediate", "nasil duzeltir", "ne yapmaliyim", "cozum",
)


def _classify(normalised: str, named: list[str]) -> Intent:
    """Topic rules, behind one gate.

    The gate is a single sentence: **a question that is explicitly definitional or procedural and
    names no service is a documentation question.** Without it "how do i diagnose a deadlock"
    matches ``diagnose`` and launches a full investigation to answer a runbook question, and
    "what is a connection pool" queries live connection counts to answer a definition.

    Naming a service is what turns the same phrasing back into an operational question: "what is
    the p99 latency of gateway" wants a number off Prometheus, not a paragraph about percentiles.
    That is a crude discriminator and it is the honest limit of a keyword table — it is precisely
    the kind of distinction the Phase 8 model should make better, and the reason this baseline is
    worth measuring against rather than assumed to be beaten.
    """
    if not named:
        if any(term in normalised for term in _REMEDIAL):
            return Intent.REMEDIATION_QUESTION

        if any(term in normalised for term in _DEFINITIONAL):
            return Intent.KNOWLEDGE_QUESTION

    for intent, terms in _RULES:
        if any(term in normalised for term in terms):
            return intent

    return Intent.GENERAL_QUESTION


def _named_services(normalised: str) -> list[str]:
    """The known services the question names, as whole words."""
    return [s for s in KNOWN_SERVICES if re.search(rf"\b{s}\b", normalised)]


def _service(named: list[str], hint: str | None) -> str | None:
    """The service the question names, falling back to the incident's own service.

    A question naming two services resolves to neither: "why does orders time out calling
    payments" is a cascade, and picking one of them silently would point every collector at half
    the problem. The hint is not used to break that tie either — the incident was filed against
    one of them, and preferring it is how a cascade gets diagnosed as a local failure.
    """
    if len(named) == 1:
        return named[0]

    if named:
        return None

    return hint
