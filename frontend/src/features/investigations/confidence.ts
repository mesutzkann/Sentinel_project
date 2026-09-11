import type { InvestigationStepDto } from '../../api/types';

/** `ConfidenceScore.to_payload()` from `agents/confidence.py`. */
export interface ConfidenceBreakdown {
  value: number;
  threshold: number;
  meets_threshold: boolean;
  terms: Record<string, { weight: number; value: number | null }>;
  missing_terms: string[];
}

/**
 * Digs the breakdown out of the VALIDATE step.
 *
 * It is not a column on `root_causes` — the backend stores the score and not its terms — but the
 * step payload the agent sent carries the whole thing, and it is already on this page. The
 * alternative was a migration for a display detail, and the payload is the more honest source
 * anyway: it is what the agent actually computed, stored verbatim.
 */
export function findConfidenceBreakdown(steps: InvestigationStepDto[]): ConfidenceBreakdown | null {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const payload = steps[index].payload;

    if (steps[index].state !== 'VALIDATE' || !payload) {
      continue;
    }

    const confidence = payload.confidence;

    if (isBreakdown(confidence)) {
      return confidence;
    }
  }

  return null;
}

function isBreakdown(value: unknown): value is ConfidenceBreakdown {
  return (
    typeof value === 'object' &&
    value !== null &&
    'terms' in value &&
    typeof (value as ConfidenceBreakdown).value === 'number'
  );
}
