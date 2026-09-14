import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { PageHeader } from '../../components/Layout';
import { describeError } from '../../api/client';
import {
  knowledgeApi,
  knowledgeQueryKeys,
  type KnowledgeDocument,
  type KnowledgeStats,
  type SearchHit,
} from '../../api/knowledge';

/**
 * What the agent knows, and what it has learned.
 *
 * The page exists for one question an incident responder actually asks — "have we seen this
 * before, and where is it written down" — so it opens on the corpus rather than on a search box,
 * and every document can be read here rather than only cited in a timeline.
 *
 * **A document the agent wrote is marked as one.** Since Phase 9 a concluded investigation writes
 * its own postmortem into this corpus, at a confidence and without review. It sits next to ten
 * written by people, and a reader who cannot tell them apart is reading an unreviewed machine
 * conclusion as institutional knowledge.
 */
export function KnowledgePage() {
  const [type, setType] = useState('');
  const [service, setService] = useState('');
  const [open, setOpen] = useState<string | null>(null);

  const documents = useQuery({
    queryKey: knowledgeQueryKeys.documents(type, service),
    queryFn: () => knowledgeApi.documents({ sourceType: type, service }),
  });

  const stats = useQuery({
    queryKey: knowledgeQueryKeys.stats,
    queryFn: () => knowledgeApi.stats(),
  });

  return (
    <>
      <PageHeader
        title="Knowledge Base"
        description="Runbooks, architecture notes and past incidents — what the agent searches before it looks at the running system, and what it writes back when an investigation concludes."
      />

      <div className="space-y-6 p-8">
        {documents.error && (
          <div className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
            <p>{describeError(documents.error)}</p>
            <p className="mt-1 text-slate-400">
              The AI service is not answering. Start it with{' '}
              <span className="identifier">uvicorn app.main:app --port 8000</span>, and PostgreSQL
              with the <span className="identifier">core</span> compose profile.
            </p>
          </div>
        )}

        {stats.data && <CorpusStrip stats={stats.data} />}

        <Finder />

        <div className="panel p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-100">
              Documents{' '}
              {documents.data && (
                <span className="font-normal text-slate-500">({documents.data.total})</span>
              )}
            </h2>

            <div className="flex gap-2">
              <select
                className="field w-44 py-1 text-xs"
                aria-label="Filter by type"
                value={type}
                onChange={(event) => setType(event.target.value)}
              >
                <option value="">Every type</option>
                {Object.keys(stats.data?.documents_by_type ?? {}).map((name) => (
                  <option key={name} value={name}>
                    {label(name)}
                  </option>
                ))}
              </select>

              <select
                className="field w-40 py-1 text-xs"
                aria-label="Filter by service"
                value={service}
                onChange={(event) => setService(event.target.value)}
              >
                <option value="">Every service</option>
                {Object.keys(stats.data?.documents_by_service ?? {}).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {documents.isPending && <p className="text-sm text-slate-500">Reading the corpus...</p>}

          {documents.data?.total === 0 && (
            <p className="text-sm text-slate-400">
              Nothing here yet. Ingest the seed corpus with{' '}
              <span className="identifier text-slate-300">POST /rag/ingest</span>.
            </p>
          )}

          <ul className="divide-y divide-ink-800">
            {(documents.data?.documents ?? []).map((document) => (
              <DocumentRow
                key={document.document_id}
                document={document}
                open={open === document.document_id}
                onToggle={() =>
                  setOpen(open === document.document_id ? null : document.document_id)
                }
              />
            ))}
          </ul>
        </div>
      </div>
    </>
  );
}

function CorpusStrip({ stats }: { stats: KnowledgeStats }) {
  return (
    <div className="panel flex flex-wrap items-center gap-x-6 gap-y-2 p-4 text-sm">
      <div className="text-slate-300">
        <span className="font-semibold text-slate-100">{stats.documents}</span> documents,{' '}
        <span className="font-semibold text-slate-100">{stats.chunks}</span> chunks
      </div>

      <div className="flex flex-wrap gap-1.5">
        {Object.entries(stats.documents_by_type).map(([name, count]) => (
          <span
            key={name}
            className="rounded border border-ink-700 bg-ink-850 px-2 py-0.5 text-xs text-slate-400"
          >
            {label(name)} {count}
          </span>
        ))}
      </div>

      <div className="ml-auto flex items-center gap-3 text-xs text-slate-500">
        <span>
          <span
            className={`mr-1.5 inline-block h-1.5 w-1.5 rounded-full ${
              stats.embedding_available ? 'bg-state-ok' : 'bg-state-bad'
            }`}
          />
          <span className="identifier">{stats.embedding_model}</span>
        </span>
        <span>
          <span
            className={`mr-1.5 inline-block h-1.5 w-1.5 rounded-full ${
              stats.rerank_available ? 'bg-state-ok' : 'bg-state-warn'
            }`}
          />
          reranker
        </span>
      </div>
    </div>
  );
}

function Finder() {
  const [query, setQuery] = useState('');

  const search = useMutation({
    mutationFn: () => knowledgeApi.search(query.trim()),
  });

  const disabled = query.trim().length === 0 || search.isPending;

  return (
    <div className="panel p-4">
      <h2 className="mb-1 text-sm font-semibold text-slate-100">Search</h2>
      <p className="mb-3 text-xs text-slate-500">
        The same hybrid retrieval with reranking the agent uses when it asks whether this has
        happened before.
      </p>

      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (!disabled) search.mutate();
        }}
      >
        <input
          className="field flex-1"
          placeholder="connection pool exhausted"
          aria-label="Search the knowledge base"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <button type="submit" className="btn-primary" disabled={disabled}>
          {search.isPending ? 'Searching...' : 'Search'}
        </button>
      </form>

      {search.error && (
        <p className="mt-3 rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
          {describeError(search.error)}
        </p>
      )}

      {search.data && search.data.results.length === 0 && (
        <p className="mt-3 text-sm text-slate-400">
          Nothing in the knowledge base matched. For the agent that is a finding too: the absence
          of a precedent is evidence.
        </p>
      )}

      {search.data && search.data.results.length > 0 && (
        <ul className="mt-4 space-y-2">
          {search.data.results.map((hit) => (
            <Hit key={hit.chunk_id} hit={hit} />
          ))}
        </ul>
      )}
    </div>
  );
}

function Hit({ hit }: { hit: SearchHit }) {
  return (
    <li className="rounded border border-ink-700 bg-ink-850 px-3 py-2">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm font-medium text-slate-200">{hit.title}</span>
        <span className="shrink-0 text-xs tabular-nums text-slate-500">#{hit.rank}</span>
      </div>

      <div className="mt-0.5 text-[11px] uppercase tracking-wide text-slate-500">
        {label(hit.source_type)}
        {hit.service && <> · {hit.service}</>}
        {hit.section && <> · {hit.section}</>}
      </div>

      <p className="mt-1.5 line-clamp-3 text-xs leading-relaxed text-slate-400">{hit.content}</p>
    </li>
  );
}

function DocumentRow({
  document,
  open,
  onToggle,
}: {
  document: KnowledgeDocument;
  open: boolean;
  onToggle: () => void;
}) {
  const detail = useQuery({
    queryKey: knowledgeQueryKeys.document(document.document_id),
    queryFn: () => knowledgeApi.document(document.document_id),
    enabled: open,
  });

  return (
    <li className="py-2.5">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-baseline justify-between gap-3 text-left"
      >
        <span>
          <span className="text-sm text-slate-200">{document.title}</span>
          {document.generated && (
            <span
              className="ml-2 rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5
                         text-[10px] font-medium uppercase tracking-wide text-accent"
              title="Written by the investigation agent, not reviewed by a human"
            >
              agent-written
            </span>
          )}
        </span>

        <span className="shrink-0 text-xs text-slate-500">
          {label(document.source_type)}
          {document.service && <> · {document.service}</>} · {document.chunks} chunks
        </span>
      </button>

      {open && (
        <div className="mt-2 rounded border border-ink-700 bg-ink-950 p-3">
          {detail.isPending && <p className="text-xs text-slate-500">Loading...</p>}
          {detail.error && (
            <p className="text-xs text-state-bad">{describeError(detail.error)}</p>
          )}
          {detail.data && (
            <>
              <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-500">
                {detail.data.external_id && (
                  <span className="identifier text-slate-400">{detail.data.external_id}</span>
                )}
                {detail.data.path && <span className="identifier">{detail.data.path}</span>}
                {detail.data.ingested_at && (
                  <span>ingested {detail.data.ingested_at.slice(0, 10)}</span>
                )}
                {Object.entries(detail.data.metadata)
                  .filter(([key]) => key !== 'source')
                  .map(([key, value]) => (
                    <span key={key}>
                      {key}: <span className="text-slate-400">{value}</span>
                    </span>
                  ))}
              </div>

              <pre className="max-h-96 overflow-auto whitespace-pre-wrap text-xs leading-relaxed text-slate-300">
                {detail.data.content}
              </pre>
            </>
          )}
        </div>
      )}
    </li>
  );
}

const labels: Record<string, string> = {
  incident: 'Incident',
  runbook: 'Runbook',
  architecture: 'Architecture',
  service_doc: 'Service',
  code_doc: 'Code',
  postmortem: 'Postmortem',
  deployment_doc: 'Deployment',
  known_error: 'Known error',
};

function label(sourceType: string): string {
  return labels[sourceType] ?? sourceType;
}
