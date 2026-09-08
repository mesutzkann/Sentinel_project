import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { PageHeader } from '../../components/Layout';
import { describeError } from '../../api/client';
import {
  mcpApi,
  mcpQueryKeys,
  type JsonSchema,
  type McpServerSummary,
  type McpToolSummary,
} from '../../api/mcp';

/**
 * What the agent can see, and a way to try it by hand.
 *
 * The runner is not a debugging convenience. From Phase 7 an investigation is a sequence of
 * these calls, and being able to run one and read exactly what came back is how a wrong answer
 * gets traced to the tool that produced it rather than blamed on the model.
 */
export function McpToolsPage() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<McpToolSummary | null>(null);

  const { data, isPending, error } = useQuery({
    queryKey: mcpQueryKeys.tools,
    queryFn: () => mcpApi.tools(),
  });

  const refresh = useMutation({
    mutationFn: () => mcpApi.tools(true),
    onSuccess: (fresh) => queryClient.setQueryData(mcpQueryKeys.tools, fresh),
  });

  const grouped = useMemo(() => groupByServer(data?.tools ?? []), [data]);

  return (
    <>
      <PageHeader
        title="MCP Tools"
        description="Everything the agent can read the running system through. Each server is a container; the agent reaches nothing except through these."
      />

      <div className="space-y-6 p-8">
        {isPending && <p className="text-sm text-slate-500">Discovering tools...</p>}

        {error && (
          <div className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
            <p>{describeError(error)}</p>
            <p className="mt-1 text-slate-400">
              The AI service is not answering. Start it with{' '}
              <span className="identifier">uvicorn app.main:app --port 8000</span>, and the
              servers with the <span className="identifier">mcp</span> compose profile.
            </p>
          </div>
        )}

        {data && (
          <>
            <ServerStrip
              servers={data.servers}
              totalTools={data.total_tools}
              onRefresh={() => refresh.mutate()}
              refreshing={refresh.isPending}
            />

            <div className="grid grid-cols-1 gap-6 xl:grid-cols-[1fr_1fr]">
              <ToolCatalogue
                grouped={grouped}
                selected={selected}
                onSelect={setSelected}
              />
              <ToolRunner tool={selected} />
            </div>
          </>
        )}
      </div>
    </>
  );
}

function ServerStrip({
  servers,
  totalTools,
  onRefresh,
  refreshing,
}: {
  servers: McpServerSummary[];
  totalTools: number;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const down = servers.filter((s) => !s.reachable);

  return (
    <div className="panel p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="text-sm text-slate-300">
          <span className="font-semibold text-slate-100">{totalTools}</span> tools across{' '}
          <span className="font-semibold text-slate-100">
            {servers.filter((s) => s.reachable).length}
          </span>{' '}
          of {servers.length} servers
        </div>

        <button
          type="button"
          onClick={onRefresh}
          disabled={refreshing}
          className="rounded border border-ink-700 px-2.5 py-1 text-xs text-slate-300
                     hover:border-accent/40 hover:text-accent disabled:opacity-50"
        >
          {refreshing ? 'Rediscovering...' : 'Rediscover'}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-6">
        {servers.map((server) => (
          <div
            key={server.name}
            className={`rounded border px-3 py-2 ${
              server.reachable
                ? 'border-state-ok/30 bg-state-ok/5'
                : 'border-state-bad/30 bg-state-bad/5'
            }`}
            title={server.error ?? server.url}
          >
            <div className="identifier text-xs text-slate-300">{server.name}</div>
            <div
              className={`mt-0.5 text-[11px] ${
                server.reachable ? 'text-state-ok' : 'text-state-bad'
              }`}
            >
              {server.reachable ? `${server.tool_count} tools` : 'unreachable'}
            </div>
          </div>
        ))}
      </div>

      {/* A server being down is why a tool is missing. Saying so here stops that looking like
          the agent having lost a capability for no reason. */}
      {down.length > 0 && (
        <ul className="mt-3 space-y-1 border-t border-ink-800 pt-3">
          {down.map((server) => (
            <li key={server.name} className="text-xs text-slate-500">
              <span className="identifier text-slate-400">{server.name}</span> — {server.error}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ToolCatalogue({
  grouped,
  selected,
  onSelect,
}: {
  grouped: [string, McpToolSummary[]][];
  selected: McpToolSummary | null;
  onSelect: (tool: McpToolSummary) => void;
}) {
  return (
    <div className="space-y-4">
      {grouped.map(([server, tools]) => (
        <div key={server} className="panel overflow-hidden">
          <div className="border-b border-ink-800 bg-ink-850 px-4 py-2">
            <span className="identifier text-xs text-slate-300">{server}</span>
            <span className="ml-2 text-[11px] text-slate-500">{tools.length} tools</span>
          </div>

          <ul className="divide-y divide-ink-800">
            {tools.map((tool) => (
              <li key={tool.qualified_name}>
                <button
                  type="button"
                  onClick={() => onSelect(tool)}
                  className={`block w-full px-4 py-3 text-left transition-colors ${
                    selected?.qualified_name === tool.qualified_name
                      ? 'bg-accent/10'
                      : 'hover:bg-ink-850/50'
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <span className="identifier text-sm text-slate-200">{tool.name}</span>
                    <AccessBadge tool={tool} />
                  </div>
                  <p className="mt-1 text-xs leading-relaxed text-slate-500">
                    {tool.description}
                  </p>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

/**
 * The classification the policy layer acts on, shown rather than implied. In Phase 4 every tool
 * is read-only; from Phase 10 a destructive badge means a human has to approve the call.
 */
function AccessBadge({ tool }: { tool: McpToolSummary }) {
  if (tool.destructive) {
    return (
      <span className="rounded border border-state-bad/40 bg-state-bad/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-state-bad">
        destructive
      </span>
    );
  }

  return (
    <span className="rounded border border-ink-600 bg-ink-700 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-400">
      read-only
    </span>
  );
}

function ToolRunner({ tool }: { tool: McpToolSummary | null }) {
  const [argumentsJson, setArgumentsJson] = useState('{}');
  const [parseError, setParseError] = useState<string | null>(null);
  const [lastTool, setLastTool] = useState<string | null>(null);

  const call = useMutation({
    mutationFn: (args: Record<string, unknown>) => mcpApi.call(tool!.qualified_name, args),
  });

  // Selecting a different tool reseeds the editor from its schema, so the arguments always
  // belong to the tool on screen rather than to the previous one.
  if (tool && tool.qualified_name !== lastTool) {
    setLastTool(tool.qualified_name);
    setArgumentsJson(seedArguments(tool.input_schema));
    setParseError(null);
    call.reset();
  }

  if (!tool) {
    return (
      <div className="panel flex items-center justify-center p-8 text-sm text-slate-500">
        Select a tool to see its arguments and run it.
      </div>
    );
  }

  const run = () => {
    let parsed: Record<string, unknown>;

    try {
      parsed = JSON.parse(argumentsJson);
    } catch (err) {
      setParseError(err instanceof Error ? err.message : 'Not valid JSON.');
      return;
    }

    setParseError(null);
    call.mutate(parsed);
  };

  return (
    <div className="panel space-y-4 p-4">
      <div>
        <div className="identifier text-sm text-slate-200">{tool.qualified_name}</div>
        <p className="mt-1 text-xs leading-relaxed text-slate-500">{tool.description}</p>
      </div>

      <SchemaHint schema={tool.input_schema} />

      <div>
        <label
          htmlFor="mcp-arguments"
          className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500"
        >
          Arguments
        </label>
        <textarea
          id="mcp-arguments"
          value={argumentsJson}
          onChange={(e) => setArgumentsJson(e.target.value)}
          spellCheck={false}
          rows={6}
          className="w-full rounded border border-ink-700 bg-ink-900 px-3 py-2 font-mono
                     text-xs text-slate-200 focus:border-accent/50 focus:outline-none"
        />
        {parseError && <p className="mt-1 text-xs text-state-bad">{parseError}</p>}
      </div>

      <button
        type="button"
        onClick={run}
        disabled={call.isPending}
        className="rounded bg-accent/15 px-3 py-1.5 text-sm font-medium text-accent
                   ring-1 ring-accent/30 hover:bg-accent/25 disabled:opacity-50"
      >
        {call.isPending ? 'Running...' : 'Run tool'}
      </button>

      {call.error && (
        <p className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
          {describeError(call.error)}
        </p>
      )}

      {call.data && <CallResult result={call.data} />}
    </div>
  );
}

function CallResult({
  result,
}: {
  result: { success: boolean; latency_ms: number; content: unknown; error: string | null };
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 text-xs">
        <span
          className={`rounded border px-1.5 py-0.5 font-semibold uppercase tracking-wide ${
            result.success
              ? 'border-state-ok/30 bg-state-ok/15 text-state-ok'
              : 'border-state-bad/30 bg-state-bad/15 text-state-bad'
          }`}
        >
          {result.success ? 'ok' : 'failed'}
        </span>
        <span className="text-slate-500">{result.latency_ms} ms</span>
      </div>

      {/* A refused or failed call is shown as a result rather than thrown away: from Phase 7
          the agent has to reason about it, and so does whoever is reading this page. */}
      {result.error && (
        <p className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-xs text-state-bad">
          {result.error}
        </p>
      )}

      {result.content !== null && result.content !== undefined && (
        <pre className="max-h-96 overflow-auto rounded border border-ink-800 bg-ink-900 p-3 font-mono text-[11px] leading-relaxed text-slate-300">
          {JSON.stringify(result.content, null, 2)}
        </pre>
      )}
    </div>
  );
}

function SchemaHint({ schema }: { schema: JsonSchema }) {
  const properties = Object.entries(schema.properties ?? {});

  if (properties.length === 0) {
    return <p className="text-xs text-slate-500">Takes no arguments.</p>;
  }

  const required = new Set(schema.required ?? []);

  return (
    <ul className="space-y-1 text-xs">
      {properties.map(([name, spec]) => (
        <li key={name} className="flex gap-2">
          <span className="identifier shrink-0 text-slate-300">{name}</span>
          <span className="shrink-0 text-slate-600">{describeType(spec.type)}</span>
          {required.has(name) && <span className="shrink-0 text-state-warn">required</span>}
          {spec.description && <span className="text-slate-500">{spec.description}</span>}
        </li>
      ))}
    </ul>
  );
}

function describeType(type: string | string[] | undefined): string {
  if (Array.isArray(type)) {
    // ["string", "null"] is how an optional argument is spelled; the null adds nothing here.
    return type.filter((t) => t !== 'null').join(' | ') || 'any';
  }

  return type ?? 'any';
}

/**
 * Pre-fills the editor with the required arguments and any defaults, so running a tool is one
 * click rather than a trip to the schema.
 */
function seedArguments(schema: JsonSchema): string {
  const properties = schema.properties ?? {};
  const required = new Set(schema.required ?? []);
  const seed: Record<string, unknown> = {};

  for (const [name, spec] of Object.entries(properties)) {
    if (spec.default !== undefined && spec.default !== null) {
      seed[name] = spec.default;
    } else if (required.has(name)) {
      seed[name] = placeholderFor(name, spec.type);
    }
  }

  return JSON.stringify(seed, null, 2);
}

function placeholderFor(name: string, type: string | string[] | undefined): unknown {
  const resolved = Array.isArray(type) ? type.find((t) => t !== 'null') : type;

  if (resolved === 'integer' || resolved === 'number') return 15;
  if (resolved === 'boolean') return false;

  // `service` is the argument most tools take, and a real service name makes the seeded call
  // runnable rather than a template to fill in.
  return name === 'service' ? 'orders' : '';
}

function groupByServer(tools: McpToolSummary[]): [string, McpToolSummary[]][] {
  const grouped = new Map<string, McpToolSummary[]>();

  for (const tool of tools) {
    const existing = grouped.get(tool.server);
    if (existing) {
      existing.push(tool);
    } else {
      grouped.set(tool.server, [tool]);
    }
  }

  return [...grouped.entries()].sort(([a], [b]) => a.localeCompare(b));
}
