# Chaos Scenarios

The 15 failure scenarios SentinelAI is built to investigate. They are the backbone of the
project: they define `root_causes.category`, the agent evaluation dataset
(`datasets/evaluation/incidents/*.json`), and what the sample services must be able to do.

## Design rules

Every scenario obeys four rules, because the agent has to *find* the cause from evidence rather
than guess it:

1. **Distinct signature.** No two scenarios produce the same combination of log / metric / trace
   symptoms. Where two are naturally similar, one discriminating signal is designed in (see the
   *Discriminator* row — e.g. circuit-breaker failures are fast, pool-exhaustion failures are
   slow).
2. **Real failure, not a fake log line.** Enabling a scenario genuinely changes service
   behaviour. The telemetry is a side effect, exactly as in production.
3. **Idempotent enable/disable.** `POST /chaos/{code}/enable` and `/disable` can be called any
   number of times; `POST /chaos/reset` clears everything. Required so agent evaluation runs are
   deterministic.
4. **A remediable fix.** Each has a concrete fix reachable through a destructive MCP tool, so
   Phase 10 (human-in-the-loop remediation) has something real to execute and verify.

## Implementation status

Scenarios 1–5 have their behaviour implemented and verified end to end against the observability
stack. The remaining ten are declared — they appear in `GET /chaos` and can be enabled — but
enabling them does not yet change behaviour; that lands in the phases that need them.

| Implemented | 1, 2, 3, 4, 5 |
|---|---|
| Declared only | 6, 7, 8, 9, 10, 11, 12, 13, 14, 15 |

## Signal legend

`L` Loki logs · `M` Prometheus metrics · `T` Jaeger traces · `D` database-mcp · `G` git-mcp
· `C` source-code-mcp · `K` docker-mcp

---

## A. Database (4)

### 1. `DB_CONNECTION_POOL_EXHAUSTION`

| | |
|---|---|
| Service | orders |
| Trigger | Npgsql `MaxPoolSize` dropped 200 to 20 while load continues |
| **L** | Burst of `Npgsql.NpgsqlException: The connection pool has been exhausted` |
| **M** | `http_request_duration_p99` climbs to the timeout ceiling; error rate rises; throughput flat |
| **T** | Spans stall *before* the DB span — time is spent waiting to acquire a connection |
| **D** | `get_connection_count` shows active connections pinned at pool max |
| Discriminator | Latency is **high** and errors are timeouts (vs. scenario 13, where errors are instant) |
| Expected tools | `get_recent_errors`, `get_connection_count`, `get_response_time` |
| Fix | `update_env_and_restart` — restore `MaxPoolSize` |

### 2. `DB_SLOW_QUERY_MISSING_INDEX`

| | |
|---|---|
| Service | orders |
| Trigger | Query switched to a non-indexed predicate, forcing a sequential scan over a seeded large table |
| **L** | *Nothing.* No errors at all — this is the point |
| **M** | p95/p99 latency rises; error rate stays flat |
| **T** | One DB span dominates the trace |
| **D** | `get_slow_queries` ranks the offending statement top by total time; `describe_table` shows no matching index |
| Discriminator | Slow **without** errors, and the slowness sits inside a single DB span |
| Expected tools | `get_slow_queries`, `get_slowest_spans`, `describe_table` |
| Fix | `execute_write_query` — `CREATE INDEX` |

### 3. `DB_DEADLOCK`

| | |
|---|---|
| Service | payments |
| Trigger | Two transactions acquire the same two rows in opposite order |
| **L** | `40P01: deadlock detected` with the victim transaction's stack |
| **M** | Sawtooth error rate — spikes, recovers, spikes again |
| **T** | Failed traces cluster on one endpoint |
| **D** | `get_locks_and_deadlocks` shows blocked/blocking pairs |
| Discriminator | Errors are **intermittent and self-recovering**, never sustained |
| Expected tools | `get_recent_errors`, `get_locks_and_deadlocks` |
| Fix | `apply_patch` — consistent lock ordering |

### 4. `DB_N_PLUS_ONE_QUERY`

| | |
|---|---|
| Service | orders |
| Trigger | Order-detail endpoint drops its `Include`, issuing one query per line item |
| **L** | Nothing |
| **M** | Latency rises moderately; DB CPU rises sharply relative to request rate |
| **T** | One trace contains **50+ sibling DB spans**, each individually fast |
| **D** | `get_slow_queries` shows a trivial query with a huge *call count* |
| Discriminator | Many fast queries (vs. scenario 2's one slow query) |
| Expected tools | `get_trace_details`, `get_slow_queries`, `search_code` |
| Fix | `apply_patch` — restore eager loading |

---

## B. Code defects (3)

### 5. `NULL_REFERENCE_EXCEPTION`

| | |
|---|---|
| Service | payments |
| Trigger | Currency lookup returns null for an unmapped currency; the result is dereferenced |
| **L** | `System.NullReferenceException` burst, stack trace naming `PaymentProcessor.cs` and a line |
| **M** | Error rate steps up and stays up; latency unchanged |
| **T** | Failed spans on one operation only |
| **G** | A recent commit touched that file |
| **C** | `read_file` shows the missing null guard |
| Discriminator | **Fast** failures on a *subset* of requests, latency unaffected |
| Expected tools | `get_exception_statistics`, `read_file`, `get_recent_commits` |
| Fix | `apply_patch` — null guard |

### 6. `DIVIDE_BY_ZERO_EDGE_CASE`

| | |
|---|---|
| Service | orders |
| Trigger | Discount-per-item calculation divides by item count; quantity 0 is accepted |
| **L** | `System.DivideByZeroException`, low volume |
| **M** | Error rate rises only slightly — a few percent |
| Discriminator | **Rare** errors correlated with a specific input, not with load or time |
| Expected tools | `get_exception_statistics`, `search_code`, `read_file` |
| Fix | `apply_patch` — validate quantity |

### 7. `MEMORY_LEAK`

| | |
|---|---|
| Service | notifications |
| Trigger | Every delivered notification is appended to a `static List` that is never drained |
| **L** | Silent for a long time, then `OutOfMemoryException` |
| **M** | Working-set bytes climb **monotonically**; gen-2 GC count climbs; latency creeps up |
| **K** | `get_container_stats` confirms rising memory |
| Discriminator | The only scenario where a metric rises *monotonically over time* rather than stepping |
| Expected tools | `get_memory_usage`, `get_container_stats`, `search_code` |
| Fix | `restart_container` (mitigation) plus `apply_patch` (real fix) |

---

## C. Configuration (3)

### 8. `TIMEOUT_TOO_LOW`

| | |
|---|---|
| Service | gateway (victim: payments) |
| Trigger | `HttpClient.Timeout` 30s to 500ms while payments legitimately takes about 800ms |
| **L** | `TaskCanceledException` in **gateway**; payments logs show success |
| **M** | Gateway error rate high, payments error rate zero |
| **T** | Gateway span errors at 500ms while the payments child span **completes successfully** |
| Discriminator | Caller fails, callee succeeds — the clearest "look upstream" signal in the set |
| Expected tools | `get_recent_errors`, `get_trace_details`, `get_container_logs` |
| Fix | `update_env_and_restart` — restore the timeout |

### 9. `WRONG_CONNECTION_STRING`

| | |
|---|---|
| Service | notifications |
| Trigger | DB host env var points at a non-existent host |
| **L** | `Npgsql.NpgsqlException: Failed to connect` immediately and consistently |
| **M** | Error rate 100% for DB-backed endpoints; health endpoint degraded |
| **D** | `get_database_health` is fine — the database itself is healthy |
| Discriminator | **100%** failure of one service while the database is provably healthy |
| Expected tools | `get_container_logs`, `get_database_health`, `get_service_logs` |
| Fix | `update_env_and_restart` — correct the connection string |

### 10. `RETRY_STORM`

| | |
|---|---|
| Service | users (victim: orders) |
| Trigger | Retry policy set to 10 attempts with no backoff |
| **L** | Repeated identical request logs in orders |
| **M** | Request rate into orders jumps about 10x with no change in inbound traffic to users |
| **T** | Traces contain 10 near-identical consecutive child spans |
| Discriminator | Downstream load rises while **upstream load does not** — amplification |
| Expected tools | `get_request_rate`, `get_recent_traces`, `search_code` |
| Fix | `update_env_and_restart` — sane retry count plus exponential backoff |

---

## D. Dependency and cascade (3)

### 11. `DOWNSTREAM_LATENCY_CASCADE`

| | |
|---|---|
| Service | payments (victims: orders, gateway) |
| Trigger | 3s artificial delay in the payment authorisation path |
| **L** | Quiet in payments; timeouts appear at the edge |
| **M** | p99 rises in **three** services simultaneously |
| **T** | `get_slowest_spans` isolates payments as the single slow span; parents merely wait |
| Discriminator | Multi-service symptom with a single-service cause — tests whether the agent follows the trace instead of blaming the loudest service |
| Expected tools | `get_slowest_spans`, `get_service_dependencies`, `get_response_time` |
| Fix | `update_env_and_restart` — disable the delay |

### 12. `EXTERNAL_DEPENDENCY_UNAVAILABLE`

| | |
|---|---|
| Service | notifications |
| Trigger | The stub SMTP/webhook dependency returns `503` |
| **L** | `503 Service Unavailable` from an **external** host, plus retry lines |
| **M** | Only notifications is affected; orders and payments are clean |
| **T** | Failed spans terminate at an external span, not an internal one |
| Discriminator | The failing span is **outside** the service boundary |
| Expected tools | `get_recent_errors`, `get_failed_traces`, `get_service_dependencies` |
| Fix | Circuit breaker plus store-and-forward queue (`apply_patch`) |

### 13. `CIRCUIT_BREAKER_STUCK_OPEN`

| | |
|---|---|
| Service | payments |
| Trigger | The breaker opens after a burst and the half-open probe never succeeds |
| **L** | `Circuit breaker opened for payment-gateway`, then `BrokenCircuitException` |
| **M** | Error rate high **and latency very low** — requests fail without doing work |
| **T** | Spans end in single-digit milliseconds with an error status |
| Discriminator | **Fast failures.** Low latency plus high error rate is unique to this scenario and separates it from 1 and 11 |
| Expected tools | `search_logs`, `get_error_rate`, `get_response_time` |
| Fix | `restart_container` plus tuned breaker thresholds |

---

## E. Deployment and resources (2)

### 14. `BAD_DEPLOYMENT_REGRESSION`

| | |
|---|---|
| Service | payments |
| Trigger | A real commit in the sample repo introduces the defect; enabling the scenario marks that commit as "deployed now" |
| **L** | Errors begin **abruptly**, not gradually |
| **M** | Error rate steps from 0 to N at a single timestamp |
| **G** | `get_recent_commits` shows a commit whose timestamp matches error onset; `get_commit_diff` shows the change |
| Discriminator | The strongest **temporal correlation** signal — onset aligns with a commit, not with load |
| Expected tools | `get_recent_commits`, `get_commits_between`, `get_commit_diff` |
| Fix | `revert_commit` |

### 15. `CPU_SATURATION`

| | |
|---|---|
| Service | orders |
| Trigger | An expensive hashing loop runs on every request |
| **L** | Nothing |
| **M** | CPU pinned above 90%; latency rises with load; error rate flat until saturation |
| **T** | Time is spent in an internal compute span, **not** in DB or HTTP spans |
| **K** | `get_container_stats` confirms CPU; memory normal |
| Discriminator | Slow with **no** DB or network involvement — pure compute |
| Expected tools | `get_cpu_usage`, `get_container_stats`, `get_slowest_spans` |
| Fix | `apply_patch` — cache or remove the hot loop |

---

## Coverage matrix

A deliberate spread, so agent evaluation cannot be passed by one strategy:

| Dimension | Scenarios |
|---|---|
| Errors, no latency change | 5, 6, 9, 13 |
| Latency, no errors | 2, 4, 15 |
| Both | 1, 3, 8, 11, 14 |
| Neither at first (slow burn) | 7, 10, 12 |
| Cause in a **different** service than the symptom | 8, 10, 11 |
| Requires git evidence | 5, 14 |
| Requires source-code evidence | 4, 5, 6, 7, 15 |
| Requires database evidence | 1, 2, 3, 4, 9 |
| Requires trace evidence (logs and metrics insufficient) | 4, 8, 11, 12 |

Six scenarios (2, 4, 8, 10, 11, 15) produce **no error logs from the failing component**, so an
agent that only reads logs cannot solve them. That is intentional — it is what makes the
multi-signal investigation worth building.

## Chaos API

Exposed by every sample service through the shared `Sentinel.Samples.Common` middleware:

```
GET  /chaos                    -> scenarios owned by this service, with enabled state
POST /chaos/{code}/enable      -> idempotent; optional JSON body carries parameters
POST /chaos/{code}/disable     -> idempotent
POST /chaos/reset              -> disable everything (called before each eval run)
```

State is in-memory and per-process, so restarting a service is also a reset.

`enable` runs any setup the scenario needs before it responds, so the call can take a while the
first time — scenario 2 grows the orders table to two million rows on its first enable. It stays
idempotent: a second call returns immediately.

## Ownership

| Service | Scenarios |
|---|---|
| gateway | 8 |
| users | 10 |
| orders | 1, 2, 4, 6, 15 |
| payments | 3, 5, 11, 13, 14 |
| notifications | 7, 9, 12 |
