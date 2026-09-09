You are writing a short passage from an internal SRE knowledge base — the kind of runbook,
postmortem or service document an on-call engineer would find when searching for an answer.

Write the passage that would answer this question, as if it already existed in that knowledge
base. It does not have to be true of any particular system: it is a search probe, and what
matters is that it uses the vocabulary the real document would use.

Rules:
- **Write in English**, whatever language the question is in. The knowledge base is in English,
  and the point of this passage is to reach it.
- Three to five sentences. No headings, no bullet lists, no preamble, no quotation marks.
- Use the concrete terms the real document would: error class names, metric names, PostgreSQL
  error codes, configuration keys, the names of the observability tools. A passage that says
  "the service had a problem" probes nothing.
- Do not answer the question to the reader. Write the document, not a reply.
- If the question names a service, name it too.

Question:
{{ query }}
