# The seed knowledge base

What the agent reads when it asks "has this happened before, and what did we do about it".
Ingested into `rag.documents` / `rag.document_chunks` by `POST /rag/ingest`, chunked by
`ai-service/rag/chunking.py`.

Files whose name begins with `_` are skipped, which is why this one is not in the index.

## Format

Every file is Markdown with a front-matter block:

```markdown
---
type: runbook
title: Connection pool exhaustion
service: orders
id: RB-001
scenario: DB_CONNECTION_POOL_EXHAUSTION
severity: high
date: 2026-03-14
---

# Connection pool exhaustion
...
```

`type` is required and must be one of the values in `rag.documents.SourceType`: `incident`,
`runbook`, `architecture`, `service_doc`, `code_doc`, `postmortem`, `deployment_doc`,
`known_error`. `title`, `service` and `id` map onto columns; every other key becomes chunk
metadata and can be filtered on — `{"service": "orders", "document_type": "postmortem"}`.

The block is flat `key: value`, not YAML. See `parse_document` in `ai-service/rag/ingest.py`
for why.

## What is here, and why

| Directory | Count | What it is for |
|---|---|---|
| `architecture/` | 3 | How the system fits together. Answers "what calls payments". |
| `services/` | 5 | One per sample service: endpoints, dependencies, known failure modes. |
| `runbooks/` | 8 | Diagnosis and fix for a failure class, written before the failure. |
| `incidents/` | 10 | Postmortems of failures that already happened. |
| `known-errors/` | 2 | Signatures with no fix yet — what to do when you see this. |

The incidents are the ones that matter most. Phase 9 measures whether a resolved incident makes
the *next* one faster, and that only means something if the corpus contains failures that
genuinely resemble each other without being identical: INC-00003 and INC-00009 are both "orders
got slow", with different causes and different evidence.
