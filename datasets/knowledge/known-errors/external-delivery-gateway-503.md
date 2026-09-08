---
type: known_error
title: Known error — delivery gateway returning 503
service: notifications
id: KE-002
scenario: EXTERNAL_DEPENDENCY_UNAVAILABLE
severity: medium
status: open
---

# Known error — delivery gateway returning 503

## Signature

Notifications logs `503 Service Unavailable` from an external host, with retry lines after each.
Orders and payments are completely clean. In the trace, the failing span is the **external**
delivery span — it has no server side, so the trace ends at the service boundary rather than
continuing into another service of ours.

## Why it is still open

The dependency is a third party with no availability commitment. Notifications currently has no
store-and-forward path, so an outage there is a notification that is never sent rather than one
that is sent late.

## What to do when you see it

1. Confirm the failure is outside the boundary. If the failing span terminates at the external
   host, nothing in the estate is broken and no service needs restarting.
2. Check the scope: only notifications should be affected. If orders or payments are also
   failing, this is not the cause.
3. Record the window. Notifications lost during the outage cannot be recovered today, and the
   affected users have to be identified from the delivery log.

## What not to do

Do not restart notifications. The service is healthy, its dependency is not, and a restart
discards the in-flight queue for no benefit.

## Permanent fix

A circuit breaker in front of the delivery gateway plus a store-and-forward queue, so an outage
delays notifications instead of dropping them. Until that lands, this stays a known error rather
than an incident.
