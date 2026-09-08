---
type: postmortem
title: INC-00005 — notifications running out of memory overnight
service: notifications
id: INC-00005
scenario: MEMORY_LEAK
severity: high
date: 2026-05-11
duration_minutes: 41
resolution: restart_and_code_fix
---

# INC-00005 — notifications running out of memory overnight

## Summary

Notifications died with `OutOfMemoryException` at 03:12 after roughly 14 hours of uninterrupted
memory growth. Delivery stopped for 41 minutes. The cause was a static list that every delivered
notification was appended to and nothing ever drained.

## Timeline

| Time | Event |
|---|---|
| 13:00 (prev day) | Working set 180 MB. Normal. |
| 19:00 | 420 MB. Nothing looks wrong; nobody is looking. |
| 01:00 | 900 MB, latency creeping up as the GC works harder. |
| 03:12 | `OutOfMemoryException`. The container is killed and restarted. |
| 03:18 | Paged on the delivery backlog. |
| 03:35 | `get_memory_usage` over 24 hours shows a straight line upward. |
| 03:53 | Container restarted deliberately, backlog drained. |
| next day | Patch removing the static list deployed. |

## What we saw

`process_working_set_bytes` climbing **monotonically** — never falling back after a collection,
across 14 hours. Gen-2 collection count climbing alongside it. Latency creeping up slowly as the
GC spent more time for less return.

The logs said nothing at all until the very end, when they said everything at once.

## Root cause

A `static List<DeliveredNotification>` in the delivery path, appended to on every send and never
drained. It had been added for a debugging session and left in.

## What made it hard

The window. Over five minutes a leak and a warming cache look the same; the shape only becomes
unmistakable over hours. The first two dashboards anyone opened were five-minute windows and
showed nothing interesting.

The other trap was the restart. The container restarting at 03:12 released the memory, so by the
time anyone looked, the metric had reset to normal. The evidence for a leak is in the *slope
before* the restart, not in the value after it.

## Fix

Restarting the container mitigated it and bought about a day. The real fix removed the static
list; what it was collecting now goes to the database with a seven-day retention.

## Follow-up

Alert on working set growth over a six hour window rather than on an absolute threshold. An
absolute threshold fires when the service is nearly dead; a growth alert would have fired at
19:00.

See also: [Runbook — memory leak](../runbooks/memory-leak.md).
