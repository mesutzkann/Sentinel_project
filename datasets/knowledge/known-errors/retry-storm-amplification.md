---
type: known_error
title: Known error — retry amplification into orders
service: users
id: KE-001
scenario: RETRY_STORM
severity: medium
status: open
---

# Known error — retry amplification into orders

## Signature

Request rate into orders rises by roughly a factor of ten with **no change in traffic arriving at
users**. Orders' logs fill with repeated identical requests, seconds apart, carrying the same
trace id. Traces contain ten near-identical consecutive child spans.

Every metric users exports is healthy. The service generating the load is not the service that
looks unwell.

## Why it is still open

The retry policy is configured for 10 attempts with no backoff, and the value has to stay until
the ordering service's idempotency work lands — without idempotency, fewer retries mean dropped
orders during a blip.

## What to do when you see it

1. Confirm the ratio: inbound rate at users against outbound rate to orders. A rising ratio with
   flat inbound traffic is amplification, not load.
2. Check whether orders is failing for its own reason. Retries make an existing problem worse and
   do not create one; if orders is erroring, fix that first and the storm subsides.
3. If orders is being harmed, reduce `Retry__MaxAttempts` to 3 with exponential backoff as a
   temporary measure and accept the small number of dropped requests.

## What not to do

Do not scale orders to absorb it. Capacity added to absorb amplified load is capacity that makes
the amplification cheaper to sustain, and the ratio does not improve.

## Permanent fix

Exponential backoff with jitter and a retry budget, once idempotency keys are in place on order
creation. Tracked separately.
