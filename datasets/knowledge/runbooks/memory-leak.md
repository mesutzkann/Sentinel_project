---
type: runbook
title: Runbook — memory leak
service: notifications
id: RB-005
scenario: MEMORY_LEAK
severity: high
---

# Runbook — memory leak

## Symptom

`process_working_set_bytes` climbs **monotonically** — it rises, and it never comes back down,
across hours and across GC cycles. Gen-2 collection count climbs with it. Latency creeps up
slowly as the GC works harder for less. Logs are silent for a long time and then produce
`OutOfMemoryException` all at once.

It is the only failure in this estate where a metric rises steadily over time rather than
stepping to a new level. That shape identifies it on its own.

## Confirm it

1. `get_memory_usage` over a window of hours, not minutes. Over five minutes a leak and a warm
   cache look identical; over three hours they do not.
2. `get_container_stats` — the container's memory agrees with the process metric, which rules out
   the metric itself being wrong.
3. `search_code` for a `static` collection that is appended to and never drained. That is the
   usual shape, and a static field holding request-scoped data is the usual cause.

Working set rising while GC heap stays flat points at unmanaged memory or unclosed handles
instead. Both climb together for a managed leak.

## Rule out the lookalikes

| Also looks like | Distinguished by |
|---|---|
| Cache warming | Levels off. A leak does not. |
| Load increase | Request rate rises too. Here it is flat. |
| CPU saturation | CPU is the pinned resource there, and memory is normal. |

## Fix

Two steps, in this order:

1. **Mitigate.** `restart_container notifications` — it releases the memory and buys hours.
2. **Fix.** Remove the unbounded retention. If the collection is a cache, bound it; if it is a
   log of delivered notifications, it belongs in the database with a retention policy, not in a
   process.

Restarting alone means being back here at the same rate. Ship the patch.

## Verify

Working set flat over an hour under the same load, and gen-2 collections no longer climbing.
This is one of the few fixes that cannot be verified in two minutes.

## Prevent

Alert on working set growth over a six hour window rather than on an absolute threshold. An
absolute threshold fires when the service is nearly dead; a growth rate fires while there is
still time.
