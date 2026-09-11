# ADR-0007: The critic may only overturn a conclusion for a reason it can name

- **Status:** Accepted
- **Date:** 2026-09-11
- **Deciders:** project owner

## Context

`VALIDATE` is the gate between a root cause and a recommendation: a rejected conclusion goes back
for another round, and a second rejection ends the investigation at `NEEDS_HUMAN` with nothing to
act on. It is the last thing standing between the agent and a result.

Measured over the five implemented chaos scenarios (`python -m evaluation.reasoning_eval`), it
was also the thing stopping every run. The 3B reached a correct root cause in three of five
cases; **none of the five finished.** The critic rejected ten conclusions, six of which were
already correct, and it did so while filling in a confidence of 0.8 on the same verdict — it was
saying "the evidence supports this well" and "this is invalid" in one object. The 7B behaved the
same way and took two and a half times as long, so this was never about model size.

Reading the rejections, three causes, all of them ours rather than the model's:

1. **The prompt asked for fault-finding and got it.** "Your job is to find what is wrong with
   it", followed by four questions each phrased as a way to disagree. A 3B follows the dominant
   instruction.
2. **Nothing separated a wrong diagnosis from an overreaching sentence.** Every causal chain has
   a link that is inferred rather than shown, so "is any link asserted rather than shown" is
   always yes — and a model that has just written down a concern will not answer "valid" next.
3. **The verdict came first.** `valid` was the first field of the schema, and constrained
   decoding fills fields in order, so the model decided and then justified.

## Decision

Four changes, three to how the critic is asked and one to how its answer is used.

1. **`critic.v2.md` asks one question** — does the evidence support this cause better than any
   other explanation of the same evidence — and lists the grounds for rejecting as a closed set:
   nothing collected supports it, something collected contradicts it, or a named alternative
   explains the same facts better. It says explicitly that concerns and unsupported claims are
   expected on a conclusion it accepts, and that what they lower is the confidence.
2. **`CriticVerdict` puts the evidence before the verdict.** `supporting_evidence` and
   `contradicting_evidence` are generated first, so `valid` has something to follow from, and
   both lists reach the screen where a human can check the critic's reading.
3. **A veto that names none of its own grounds is overruled in code.** The objection is kept —
   as a concern on the record, and as the critic's confidence, which is a quarter of the score —
   but it no longer ends the round.
4. **`hypothesis_margin` measures the gap to a *different* claim.** A 3B proposes the same
   explanation twice in different words; measuring the winner against its own paraphrase scored a
   correct, critic-accepted conclusion at 0.667 and stopped it. Sameness is judged by the cited
   evidence, which is what the arithmetic can see.

The 0.70 confidence threshold is untouched, and that is the point: an objection the critic cannot
articulate still lowers the score, and the score still stops the run.

## Consequences

**Positive** — measured on the same five scenarios, same model, before and after:

| | before | after |
|---|---|---|
| correct root cause | 3/5 | 3/5 |
| investigations that finished | 0/5 | 2/5 |
| rejections (of which false) | 10 (6) | 1 (1) |
| recommendations produced | 0 | 6 |
| model calls per investigation | 6.0 | 4.0 |
| mean wall clock | 87 s | 74 s |

Fewer rejections means fewer rounds, so the agent is a third cheaper while producing more. Read
the call count rather than the clock: a second pass with `--repeat 2` measured 91 s on a busier
machine and the same 4.0 calls, and produced identical conclusions and identical confidences to
the first — temperature is 0 and the decoding is constrained, so these runs reproduce.

The two gates still hold where they should. The two runs that concluded wrongly scored 0.66 and
0.33 and stopped for a human, and so did a correct conclusion the critic doubted at 0.62 — an
overruled veto is still a quarter of the score, and here it was enough to stop the run on its
own.

**Negative**

- The critic can no longer block a conclusion on a judgement it cannot put into a field. If the
  right answer is "this feels wrong and I cannot say why", this system does not have it any more.
- A model that names a spurious `alternative` still vetoes. That is the weakest of the three
  grounds and the one most likely to need revisiting.
- The sameness test behind the margin is crude: identical citation sets. Two hypotheses where one
  cites a subset of the other count as different claims, and telling those apart would need the
  meaning of the sentences, which is the one thing not to put in an arithmetic term.
- These are five fixtures and one model. They are the five scenarios that exist, and the numbers
  should be re-measured whenever either changes.

## Alternatives considered

- **Lower the 0.70 threshold.** It would finish more runs and it is tuning the gate to pass the
  test. The threshold is what stops the agent acting on a 0.6 conclusion, which is the failure
  this whole phase is built to avoid.
- **Allow a third round instead of stopping at the second rejection.** More model time to reach
  the same `NEEDS_HUMAN`: the disagreement was never about phrasing.
- **A second model as arbiter.** Doubles the cost of the slowest step to ask another 3B the same
  badly-posed question.
- **Leave it and demo `NEEDS_HUMAN`.** Honest, and it makes the demo a system that investigates
  well and concludes nothing.
