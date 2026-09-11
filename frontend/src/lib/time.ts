const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * Relative time, coarse on purpose.
 *
 * During an incident the useful question is "how long has this been going on", not the exact
 * timestamp; the precise instant is available on the detail screen, where correlating with a
 * deployment actually requires it.
 */
export function formatRelative(iso: string): string {
  const elapsed = Date.now() - new Date(iso).getTime();

  if (elapsed < MINUTE) {
    return 'just now';
  }

  if (elapsed < HOUR) {
    const minutes = Math.floor(elapsed / MINUTE);
    return `${minutes}m ago`;
  }

  if (elapsed < DAY) {
    const hours = Math.floor(elapsed / HOUR);
    return `${hours}h ago`;
  }

  const days = Math.floor(elapsed / DAY);
  return days === 1 ? 'yesterday' : `${days}d ago`;
}

/** Absolute timestamp, for detail views where correlating against a commit needs the real instant. */
export function formatAbsolute(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'medium',
  });
}

/**
 * A span of milliseconds, at the precision the number deserves.
 *
 * Step durations run from a few hundred milliseconds for a tool call to minutes for an
 * investigation, and one format across that range is either noise at the top or a rounding error
 * at the bottom.
 */
export function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${ms}ms`;
  }

  if (ms < 60_000) {
    return `${(ms / 1000).toFixed(1)}s`;
  }

  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);

  return `${minutes}m ${seconds}s`;
}
