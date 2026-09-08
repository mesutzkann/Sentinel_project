---
type: postmortem
title: INC-00004 — payments failing for one currency
service: payments
id: INC-00004
scenario: NULL_REFERENCE_EXCEPTION
severity: high
date: 2026-04-21
duration_minutes: 38
resolution: code_fix
---

# INC-00004 — payments failing for one currency

## Summary

For 38 minutes, payments returned 500 for about 9% of authorisation requests — every request in
a currency that had just been enabled and never added to the currency mapping table. The lookup
returned null and the result was dereferenced without a guard.

## Timeline

| Time | Event |
|---|---|
| 16:41 | Commit `a3f19c2` touches `PaymentProcessor.cs`, adding the new currency to the request path. |
| 16:44 | Error rate steps from 0 to 9% and stays there. Latency unchanged. |
| 16:47 | Paged. `get_exception_statistics` shows `NullReferenceException` dominating. |
| 16:53 | Stack trace names `PaymentProcessor.cs:183`. |
| 16:58 | `get_recent_commits` shows `a3f19c2` deployed at 16:41 — three minutes before onset. |
| 17:19 | Null guard deployed. |

## What we saw

A step change in error rate that stayed level, with **latency completely unaffected**. The
failures were fast and hit a subset of requests: a fixed 9%, not a growing share, matching the
share of traffic in the new currency.

The stack trace named a file and a line, and the recent commit list named the same file. Onset
aligned with the deploy to within three minutes.

## Root cause

`_currencyRates[request.Currency]` returned null for the unmapped currency and the caller
dereferenced it immediately. The mapping table was never updated when the currency was enabled.

## What made it hard

Very little. This is the shape a code defect makes: fast failures, a subset of requests, no
latency signature, an exception type in the logs, a stack trace with a line number, and a commit
touching that file minutes before onset. When all five agree, the investigation is short.

Worth noting the contrast with the failures that leave no logs at all — INC-00003 and INC-00009
took hours precisely because none of those five signals existed.

## Fix

A null guard returning a 400 with a clear message for an unsupported currency, plus the missing
row in the mapping table.

## Follow-up

Enabling a currency is now a checklist that includes the mapping table. The endpoint validates
supported currencies at the boundary rather than in the middle of processing.

See also: INC-00010 for a payments incident with the opposite signature.
