Classify one question from an engineer about a microservice estate. Answer with the plan it
implies and nothing else.

Return a single JSON object matching this schema:

{{ schema }}

The intents, and the tools each one implies:

{{ intents }}

Rules:
- `intent` is the *question's* shape, not the answer's. "orders is slow" is PERFORMANCE_ANALYSIS
  even if the cause turns out to be the database; "is the database healthy" is DATABASE_HEALTH
  even if everything is fine.
- `target_service` is exactly one of {{ services }} when the question is about one of them, and
  null otherwise — including when it names two, or none, or something that is not a service.
- `requires_rag`, `requires_mcp` and `tools` follow from the intent. Use the table above exactly;
  do not invent a tool name and do not leave the list empty for an intent that has one.
- When nothing fits, GENERAL_QUESTION. A wrong intent sends the investigation to collect the
  wrong evidence, which is worse than collecting broadly.
- Reply with the JSON object alone: no explanation, no code fence.

Question: {{ query }}
