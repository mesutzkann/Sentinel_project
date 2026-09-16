# Screenshots

Taken on 2026-09-16 against a running stack — the full estate under `--profile core --profile
samples --profile mcp`, the backend and AI service native, `qwen2.5:7b-instruct` behind the
agent. Nothing here is a mockup and no data was seeded for the picture: the incidents are the
ones `scripts/demo.py` raised, and `03-investigation-detail` is INC-00013, the run that concluded
`DB_CONNECTION_POOL_EXHAUSTION` on a scenario that was in fact `DB_CONNECTION_POOL_EXHAUSTION`.

Captured through headless Chrome over the DevTools protocol at 2× device scale, full page. The
script that did it is deliberately not in the repository: it is thirty lines of one-off CDP, and
carrying Playwright as a dependency to reproduce a picture would cost more than retaking it.

| file | page | what it shows |
|---|---|---|
| `01-dashboard.png` | `/` | The estate and its open incidents. |
| `02-investigations.png` | `/investigations` | Every run, with what it concluded beside how sure it was — a conclusion and a conclusion worth acting on are different outcomes. |
| `03-investigation-detail.png` | `/investigations/:id` | The long one. Root cause, what the confidence is made of, the critic's objection, the evidence-to-conclusion graph, the ranked hypotheses, and all 21 steps with the tool calls under each. |
| `04-incidents.png` | `/incidents` | Incidents, their state and which service they belong to. |
| `05-services.png` | `/services` | The five sample services and their open incident counts. |
| `06-evaluation.png` | `/evaluation` | Every benchmark the system is judged on, drawn from what the suite wrote — router against the keyword table it replaced, four retrievers against each other, 3B against 7B. |
| `07-knowledge.png` | `/knowledge` | The corpus the agent retrieves from. |
| `08-mcp-tools.png` | `/mcp` | 43 tools across 8 MCP servers, each with what it answers and whether it is read-only or destructive. |
| `09-models.png` | `/models` | The router, and what it makes of a question you type. |

**They go stale.** `06-evaluation` draws the last suite run, and a re-run changes the numbers on
it; the investigation pages are of particular runs and a fresh demo produces different ones. Any
figure quoted in the README should come from `datasets/evaluation/runs/`, which records the
machine it was measured on, rather than from a picture of a screen.
