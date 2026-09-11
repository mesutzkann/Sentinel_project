import { useEffect, useState } from 'react';
import { HubConnectionBuilder, HubConnectionState, LogLevel } from '@microsoft/signalr';
import type { HubConnection } from '@microsoft/signalr';
import { useQueryClient } from '@tanstack/react-query';
import { api, tokenStorage } from '../../api/client';
import { queryKeys } from '../../api/endpoints';
import type {
  EvidenceDto,
  HypothesisDto,
  InvestigationDetailDto,
  InvestigationDto,
  InvestigationStepDto,
  RootCauseDto,
} from '../../api/types';

/**
 * The live half of an investigation.
 *
 * A run is tens of seconds to minutes of model and tool calls, and the timeline filling in while
 * it happens is the most visible thing this project does — it is why ADR-0002 chose callbacks
 * over a synchronous call, and this hook is the far end of that decision.
 *
 * **Pushed events are merged into the cached detail rather than triggering a refetch.** Around
 * forty events arrive per investigation; refetching the whole thing on each one would ask the
 * backend for the same forty steps forty times. The merges are all idempotent — matched by
 * sequence or by id — because delivery is at-least-once and because a reconnect replays nothing:
 * what the hook does on reconnect is invalidate once and let the query fill the gap.
 */
export type StreamStatus = 'connecting' | 'live' | 'reconnecting' | 'offline';

export function useInvestigationStream(investigationId: string, enabled: boolean): StreamStatus {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<StreamStatus>('connecting');

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const key = queryKeys.investigations.detail(investigationId);

    const patch = (apply: (current: InvestigationDetailDto) => InvestigationDetailDto) => {
      queryClient.setQueryData<InvestigationDetailDto>(key, (current) =>
        current ? apply(current) : current,
      );
    };

    const connection = new HubConnectionBuilder()
      .withUrl(hubUrl(), { accessTokenFactory: () => tokenStorage.get() ?? '' })
      .withAutomaticReconnect()
      .configureLogging(LogLevel.Warning)
      .build();

    connection.on('StepUpdated', (_id: string, step: InvestigationStepDto) => {
      patch((current) => ({
        ...current,
        steps: upsertBy(current.steps, step, (s) => s.sequence).sort(
          (a, b) => a.sequence - b.sequence,
        ),
        investigation: { ...current.investigation, step_count: current.investigation.step_count + 1 },
      }));
    });

    connection.on('EvidenceAdded', (_id: string, evidence: EvidenceDto) => {
      patch((current) => ({
        ...current,
        evidence: upsertBy(current.evidence, evidence, (e) => e.id),
        investigation: {
          ...current.investigation,
          evidence_count: current.investigation.evidence_count + 1,
        },
      }));
    });

    connection.on('HypothesisUpdated', (_id: string, hypothesis: HypothesisDto) => {
      patch((current) => ({
        ...current,
        hypotheses: upsertBy(current.hypotheses, hypothesis, (h) => h.id),
      }));
    });

    connection.on('RootCauseFound', (_id: string, rootCause: RootCauseDto) => {
      patch((current) => ({ ...current, root_cause: rootCause }));
    });

    connection.on('InvestigationCompleted', (_id: string, investigation: InvestigationDto) => {
      patch((current) => ({ ...current, investigation }));

      // The one event that refetches. Recommendations have no push of their own — they are
      // written from the terminal event, which is also when the backend reconciles whatever the
      // stream lost — so the finished investigation is worth reading once in full.
      void queryClient.invalidateQueries({ queryKey: key });
    });

    connection.onreconnecting(() => setStatus('reconnecting'));

    connection.onreconnected(() => {
      setStatus('live');
      void connection.invoke('Subscribe', investigationId);

      // Whatever arrived while the socket was down was not buffered for us. The rows are in the
      // database either way, so one read closes the gap.
      void queryClient.invalidateQueries({ queryKey: key });
    });

    connection.onclose(() => setStatus('offline'));

    let cancelled = false;

    connection
      .start()
      .then(() => {
        if (cancelled) {
          return;
        }

        setStatus('live');
        return connection.invoke('Subscribe', investigationId);
      })
      .catch(() => {
        // A hub that will not connect is not a failed investigation: the agent keeps running and
        // the page keeps polling. Saying so on the screen is the whole of the handling.
        if (!cancelled) {
          setStatus('offline');
        }
      });

    return () => {
      cancelled = true;
      void stop(connection);
    };
  }, [investigationId, enabled, queryClient]);

  // Derived rather than stored: a hook that is not connecting is offline, and writing that into
  // state from the effect would be a second render to say something already known here.
  return enabled ? status : 'offline';
}

/** The hub lives on the backend, at the same origin the REST client uses. */
function hubUrl(): string {
  const base = (api.defaults.baseURL ?? '').replace(/\/$/, '');

  return `${base}/hubs/investigations`;
}

function upsertBy<T>(items: T[], incoming: T, key: (item: T) => string | number): T[] {
  const index = items.findIndex((item) => key(item) === key(incoming));

  if (index === -1) {
    return [...items, incoming];
  }

  const next = [...items];
  next[index] = incoming;

  return next;
}

async function stop(connection: HubConnection): Promise<void> {
  if (connection.state === HubConnectionState.Disconnected) {
    return;
  }

  try {
    await connection.stop();
  } catch {
    // Tearing down a socket that is already closing is not worth a console error on every
    // navigation away from the page.
  }
}
