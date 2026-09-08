import type { IncidentSeverity, IncidentStatus } from '../api/types';

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
