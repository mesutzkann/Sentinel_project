import type {
  EvidenceSource,
  IncidentSeverity,
  IncidentStatus,
  InvestigationStatus,
} from '../api/types';

const severityStyles: Record<IncidentSeverity, string> = {
  low: 'bg-severity-low/15 text-severity-low border-severity-low/30',
  medium: 'bg-severity-medium/15 text-severity-medium border-severity-medium/30',
  high: 'bg-severity-high/15 text-severity-high border-severity-high/30',
  critical: 'bg-severity-critical/20 text-severity-critical border-severity-critical/40',
};

export function SeverityBadge({ severity }: { severity: IncidentSeverity }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[11px]
                  font-semibold uppercase tracking-wide ${severityStyles[severity]}`}
    >
      {severity}
    </span>
  );
}

const statusLabels: Record<IncidentStatus, string> = {
  open: 'Open',
  investigating: 'Investigating',
  awaiting_approval: 'Awaiting approval',
  resolving: 'Resolving',
  resolved: 'Resolved',
  closed: 'Closed',
};

/**
 * Statuses are grouped by what they mean for the viewer rather than given six distinct colours:
 * something needs you (amber), something is happening (blue), nothing to do (grey).
 */
const statusStyles: Record<IncidentStatus, string> = {
  open: 'bg-state-warn/15 text-state-warn border-state-warn/30',
  investigating: 'bg-accent/15 text-accent border-accent/30',
  awaiting_approval: 'bg-state-warn/15 text-state-warn border-state-warn/30',
  resolving: 'bg-accent/15 text-accent border-accent/30',
  resolved: 'bg-state-ok/15 text-state-ok border-state-ok/30',
  closed: 'bg-ink-700 text-slate-400 border-ink-600',
};

export function StatusBadge({ status }: { status: IncidentStatus }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[11px]
                  font-medium ${statusStyles[status]}`}
    >
      {statusLabels[status]}
    </span>
  );
}

const investigationLabels: Record<InvestigationStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

const investigationStyles: Record<InvestigationStatus, string> = {
  queued: 'bg-ink-700 text-slate-400 border-ink-600',
  running: 'bg-accent/15 text-accent border-accent/30',
  completed: 'bg-state-ok/15 text-state-ok border-state-ok/30',
  failed: 'bg-state-bad/15 text-state-bad border-state-bad/30',
  cancelled: 'bg-ink-700 text-slate-400 border-ink-600',
};

/**
 * A completed investigation that would not stand behind its conclusion is still completed — the
 * agent finished, and what it concluded is not something it will act on. That distinction lives
 * in `needsHuman`, because the status column has no word for it and inventing one in the backend
 * would have meant a sixth enum value that only this badge understands.
 */
export function InvestigationStatusBadge({
  status,
  needsHuman = false,
}: {
  status: InvestigationStatus;
  needsHuman?: boolean;
}) {
  const stopped = status === 'completed' && needsHuman;

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 text-[11px]
                  font-medium ${
                    stopped
                      ? 'bg-state-warn/15 text-state-warn border-state-warn/30'
                      : investigationStyles[status]
                  }`}
    >
      {status === 'running' && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" aria-hidden />
      )}
      {stopped ? 'Needs a human' : investigationLabels[status]}
    </span>
  );
}

/** Where a fact came from. Signals of the same kind share a colour so a panel scans by source. */
const sourceStyles: Record<EvidenceSource, string> = {
  logs: 'bg-severity-medium/15 text-severity-medium border-severity-medium/30',
  metrics: 'bg-accent/15 text-accent border-accent/30',
  traces: 'bg-severity-low/15 text-severity-low border-severity-low/30',
  git: 'bg-state-ok/15 text-state-ok border-state-ok/30',
  database: 'bg-severity-high/15 text-severity-high border-severity-high/30',
  source_code: 'bg-state-ok/15 text-state-ok border-state-ok/30',
  docker: 'bg-ink-700 text-slate-300 border-ink-600',
  historical_incident: 'bg-accent-muted/40 text-slate-300 border-accent/20',
  rag_document: 'bg-accent-muted/40 text-slate-300 border-accent/20',
};

export function SourceBadge({ source }: { source: EvidenceSource }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px]
                  uppercase tracking-tight ${sourceStyles[source]}`}
    >
      {source.replace('_', ' ')}
    </span>
  );
}
