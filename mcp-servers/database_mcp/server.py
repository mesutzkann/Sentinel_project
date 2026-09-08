"""database-mcp: what PostgreSQL itself can say about the failure.

The database is the only place several faults are visible at all. A slow query with no error
logs, a connection pool held at its ceiling, a deadlock between two transactions — none of these
leave a trace in the application unless the application happens to log them, and four of the
fifteen chaos scenarios are built on exactly that.

Everything here is read-only. Writes arrive in Phase 10 behind an approval token, and the
annotation on each tool is what the AI service's policy layer uses to enforce that.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import asyncpg

from _shared.config import settings
from _shared.results import empty, result, truncated
from _shared.server import build_server, read_only, serve

logger = logging.getLogger(__name__)

server = build_server(
    "database-mcp",
    instructions=(
        "PostgreSQL diagnostics: connection counts, slow queries from pg_stat_statements, "
        "locks and deadlocks, table structure, and read-only SQL. Each sample service owns its "
        "own schema (svc_orders, svc_payments, svc_users, svc_notifications), so a slow query "
        "or a lock can be attributed to a specific service rather than to 'the database'."
    ),
)

# Schemas the services own. Used to attribute a statement to a service.
_SERVICE_SCHEMAS = {
    "svc_users": "users",
    "svc_orders": "orders",
    "svc_payments": "payments",
    "svc_notifications": "notifications",
    "sentinel": "sentinel-backend",
}


class DatabaseError(RuntimeError):
    """The database could not answer."""


async def _connect() -> asyncpg.Connection:
    config = settings()

    try:
        return await asyncpg.connect(
            config.postgres_dsn,
            timeout=int(config.http_timeout_seconds),
            # Applies to every statement on this connection, so no tool can hang the agent.
            server_settings={"statement_timeout": str(config.query_timeout_ms)},
        )
    except (OSError, asyncpg.PostgresError) as exc:
        raise DatabaseError(f"Could not connect to PostgreSQL: {exc}") from exc


async def _fetch(query: str, *args: Any) -> list[dict[str, Any]]:
    connection = await _connect()

    try:
        rows = await connection.fetch(query, *args)
        return [dict(row) for row in rows]
    except asyncpg.PostgresError as exc:
        raise DatabaseError(f"Query failed: {exc}") from exc
    finally:
        await connection.close()


def _attribute(query: str) -> str | None:
    """Which service a statement belongs to, by the schema it touches."""
    for schema, service in _SERVICE_SCHEMAS.items():
        if schema in query:
            return service

    return None


# --------------------------------------------------------------------------- tools ----


@read_only(
    server,
    "Overall database health: version, uptime, size, connection usage, and whether any backend "
    "is currently blocked. A good first call to establish whether the database is a victim or "
    "a cause.",
)
async def get_database_health() -> dict[str, Any]:
    """Takes no arguments."""
    rows = await _fetch(
        """
        SELECT current_setting('server_version') AS version,
               pg_postmaster_start_time() AS started_at,
               pg_size_pretty(pg_database_size(current_database())) AS size,
               current_setting('max_connections')::int AS max_connections,
               (SELECT count(*) FROM pg_stat_activity) AS connections,
               (SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock') AS blocked,
               (SELECT count(*) FROM pg_stat_activity WHERE state = 'active') AS active
        """
    )

    health = rows[0]
    usage = health["connections"] / health["max_connections"]

    return {
        "version": health["version"],
        "started_at": health["started_at"].isoformat(),
        "database_size": health["size"],
        "connections": health["connections"],
        "max_connections": health["max_connections"],
        "connection_usage": round(usage, 3),
        "active_queries": health["active"],
        "blocked_backends": health["blocked"],
        "interpretation": (
            "The database itself is healthy. A service failing while this is true points at "
            "the service's own configuration — its pool size, or the host it is dialling — "
            "rather than at the database."
            if usage < 0.8 and health["blocked"] == 0
            else "The database is under pressure: connections are near the ceiling or backends "
            "are blocked waiting on locks."
        ),
    }


@read_only(
    server,
    "Connections grouped by the service that opened them. The distinguishing figure for pool "
    "exhaustion: a service pinned at exactly its pool maximum while the database has capacity "
    "to spare is exhausting its own pool, not the server's.",
)
async def get_connection_count() -> dict[str, Any]:
    """Takes no arguments."""
    rows = await _fetch(
        """
        SELECT COALESCE(application_name, '(unnamed)') AS client,
               state,
               count(*) AS connections
        FROM pg_stat_activity
        WHERE datname = current_database()
        GROUP BY 1, 2
        ORDER BY 3 DESC
        """
    )

    totals = await _fetch(
        "SELECT current_setting('max_connections')::int AS max, count(*) AS used "
        "FROM pg_stat_activity"
    )

    return {
        "max_connections": totals[0]["max"],
        "used": totals[0]["used"],
        "headroom": totals[0]["max"] - totals[0]["used"],
        "by_client": [
            {"client": r["client"], "state": r["state"], "connections": r["connections"]}
            for r in rows
        ],
        "note": (
            "These are server-side connections. A service can exhaust its own client-side pool "
            "while the server still has headroom — if requests are timing out but headroom "
            "here is large, the limit is the service's MaxPoolSize, not the database's."
        ),
    }


@read_only(
    server,
    "Statements ranked by total time spent, from pg_stat_statements. Two shapes matter: one "
    "statement with a high mean is a missing index; a trivial statement with a huge call count "
    "is a query issued per row.",
)
async def get_slow_queries(limit: int = 10, min_mean_ms: float = 0.0) -> dict[str, Any]:
    """Args:
    limit: How many statements to return.
    min_mean_ms: Ignore statements faster than this on average.
    """
    try:
        rows = await _fetch(
            """
            SELECT query,
                   calls,
                   round(total_exec_time::numeric, 2) AS total_ms,
                   round(mean_exec_time::numeric, 2) AS mean_ms,
                   round(max_exec_time::numeric, 2) AS max_ms,
                   rows
            FROM pg_stat_statements
            WHERE mean_exec_time >= $1
              AND query NOT ILIKE '%pg_stat_statements%'
            ORDER BY total_exec_time DESC
            LIMIT $2
            """,
            min_mean_ms,
            min(limit, settings().max_results),
        )
    except DatabaseError as exc:
        if "pg_stat_statements" in str(exc):
            return {
                "error": (
                    "pg_stat_statements is not available. It requires "
                    "shared_preload_libraries=pg_stat_statements, which the compose file sets."
                )
            }
        raise

    if not rows:
        return empty("since statistics were last reset", "pg_stat_statements")

    items = [
        {
            "query": " ".join(r["query"].split())[:400],
            "calls": r["calls"],
            "total_ms": float(r["total_ms"]),
            "mean_ms": float(r["mean_ms"]),
            "max_ms": float(r["max_ms"]),
            "rows": r["rows"],
            "service": _attribute(r["query"]),
            "shape": (
                "high call count, low mean — a query issued per row rather than once"
                if r["calls"] > 100 and float(r["mean_ms"]) < 5
                else "high mean — likely a scan where an index is missing"
                if float(r["mean_ms"]) > 50
                else None
            ),
        }
        for r in rows
    ]

    return result(found=len(items), window="since statistics were last reset", items=items)


@read_only(
    server,
    "Current lock waits and the deadlock count since startup. A deadlock is intermittent and "
    "self-recovering, so a nonzero count with no current waiter still means it happened.",
)
async def get_locks_and_deadlocks() -> dict[str, Any]:
    """Takes no arguments."""
    waits = await _fetch(
        """
        SELECT blocked.pid AS blocked_pid,
               blocked.query AS blocked_query,
               blocking.pid AS blocking_pid,
               blocking.query AS blocking_query,
               blocked.wait_event_type,
               blocked.wait_event
        FROM pg_stat_activity blocked
        JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocking_pid ON true
        JOIN pg_stat_activity blocking ON blocking.pid = blocking_pid
        WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
        """
    )

    stats = await _fetch(
        "SELECT deadlocks, stats_reset FROM pg_stat_database WHERE datname = current_database()"
    )

    deadlocks = stats[0]["deadlocks"] if stats else 0

    return {
        "deadlocks_since_reset": deadlocks,
        "stats_reset": stats[0]["stats_reset"].isoformat()
        if stats and stats[0]["stats_reset"]
        else None,
        "current_lock_waits": [
            {
                "blocked_pid": r["blocked_pid"],
                "blocked_query": " ".join((r["blocked_query"] or "").split())[:200],
                "blocking_pid": r["blocking_pid"],
                "blocking_query": " ".join((r["blocking_query"] or "").split())[:200],
                "wait_event": f"{r['wait_event_type']}:{r['wait_event']}",
            }
            for r in waits
        ],
        "interpretation": (
            f"{deadlocks} deadlock(s) have been resolved since statistics were reset. "
            "Deadlocks are detected and broken by PostgreSQL, so they appear as intermittent "
            "errors that recover on their own rather than as a sustained outage."
            if deadlocks
            else "No deadlocks recorded and nothing is currently blocked."
        ),
    }


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@read_only(
    server,
    "Columns, indexes and row estimate for one table. The indexes are the point: a slow query "
    "filtering on a column that appears in no index here is the missing-index diagnosis.",
)
async def describe_table(table: str, schema: str = "public") -> dict[str, Any]:
    """Args:
    table: Table name, without the schema.
    schema: Schema it lives in, e.g. `svc_orders`.
    """
    # Identifiers cannot be parameterised in SQL, so they are validated rather than escaped.
    if not _IDENTIFIER.match(table) or not _IDENTIFIER.match(schema):
        return {"error": "Table and schema must be plain identifiers."}

    columns = await _fetch(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
        ORDER BY ordinal_position
        """,
        schema,
        table,
    )

    if not columns:
        return {
            "error": f"No table {schema}.{table}.",
            "hint": "Schemas: svc_users, svc_orders, svc_payments, svc_notifications, sentinel.",
        }

    indexes = await _fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = $1 AND tablename = $2",
        schema,
        table,
    )

    estimate = await _fetch(
        """
        SELECT reltuples::bigint AS rows, pg_size_pretty(pg_total_relation_size(oid)) AS size
        FROM pg_class
        WHERE oid = to_regclass($1)
        """,
        f"{schema}.{table}",
    )

    indexed_columns = {
        column
        for index in indexes
        for column in re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', index["indexdef"])
    }

    return {
        "table": f"{schema}.{table}",
        "estimated_rows": estimate[0]["rows"] if estimate else None,
        "size": estimate[0]["size"] if estimate else None,
        "columns": [
            {
                "name": c["column_name"],
                "type": c["data_type"],
                "nullable": c["is_nullable"] == "YES",
                "default": c["column_default"],
                "indexed": c["column_name"] in indexed_columns,
            }
            for c in columns
        ],
        "indexes": [{"name": i["indexname"], "definition": i["indexdef"]} for i in indexes],
    }


# Only a single SELECT or a WITH that ends in one. Everything else is rejected before it
# reaches the database.
_SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|"
    r"reindex|cluster|call|do|set|reset|listen|notify|lock)\b",
    re.IGNORECASE,
)


@read_only(
    server,
    "Run a SELECT against the database. Read-only is enforced by the server, not by trust: the "
    "statement runs inside a READ ONLY transaction under a statement timeout, so a write is "
    "refused by PostgreSQL even if it gets past the parser.",
)
async def execute_readonly_query(sql: str, limit: int = 50) -> dict[str, Any]:
    """Args:
    sql: A single SELECT statement. Schema-qualify tables, e.g. `svc_orders.orders`.
    limit: Maximum rows to return.
    """
    statement = sql.strip().rstrip(";")

    if not _SELECT_ONLY.match(statement):
        return {"error": "Only SELECT (or WITH ... SELECT) statements are allowed.", "sql": sql}

    if ";" in statement:
        # A second statement is how a read gets turned into a write.
        return {"error": "Only one statement at a time.", "sql": sql}

    if _FORBIDDEN.search(statement):
        return {"error": "The statement contains a write or administrative keyword.", "sql": sql}

    connection = await _connect()

    try:
        # The real guarantee. The parser above is a fast rejection for obvious cases; this is
        # what makes a write impossible, including through a function the parser cannot see
        # into.
        async with connection.transaction(readonly=True):
            rows = await connection.fetch(statement)
    except asyncpg.PostgresError as exc:
        return {"error": f"PostgreSQL rejected the query: {exc}", "sql": sql}
    finally:
        await connection.close()

    if not rows:
        return {"sql": statement, "found": 0, "rows": [], "note": "The query returned no rows."}

    capped = min(limit, settings().max_results)
    shown, was_truncated = truncated([dict(r) for r in rows], capped)

    return {
        "sql": statement,
        "found": len(rows),
        "columns": list(rows[0].keys()),
        # Values are stringified because a result set can contain dates, UUIDs and numerics that
        # are not JSON types, and a serialisation failure here would lose the whole answer.
        "rows": [{k: str(v) for k, v in row.items()} for row in shown],
        "truncated": was_truncated,
    }


def main() -> None:
    serve(server, settings().port)


if __name__ == "__main__":
    main()
