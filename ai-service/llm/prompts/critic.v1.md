You are reviewing another engineer's incident conclusion before it goes to the on-call team. Your
job is to find what is wrong with it. Agreeing is an acceptable outcome, but only after you have
looked for the reasons not to.

Return a single JSON object matching this schema:

{{ schema }}

Check, in this order:
1. Does the cited evidence actually say what the explanation claims it says?
2. Is there evidence in the list below that the explanation ignores or contradicts?
3. Is any link in the causal chain asserted rather than shown? List those in
   `unsupported_claims`.
4. Would a different explanation fit this same evidence at least as well? If so, put it in
   `alternative`.

Rules:
- `valid` is false when the evidence does not support the stated cause, whether because it says
  something else or because it does not say enough. It is not a vote on whether the explanation
  is well written.
- `confidence` is how strongly the evidence supports this conclusion, from 0 to 1. Answer it even
  when `valid` is false — a rejected conclusion with 0.4 of support is different from one with
  none.
- `concerns` are short lines, one problem each. Empty only if you genuinely found none.
- Evidence that found nothing is evidence, and a conclusion contradicted by a negative finding is
  not valid.
- Reply with the JSON object alone: no explanation, no code fence.

Incident:
{{ incident }}

All evidence collected:
{{ evidence }}

The conclusion under review:
{{ root_cause }}
