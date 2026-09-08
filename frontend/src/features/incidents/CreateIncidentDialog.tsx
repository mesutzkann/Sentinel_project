import { useState, type SubmitEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { incidentsApi, queryKeys, servicesApi } from '../../api/endpoints';
import { describeError } from '../../api/client';
import type { IncidentSeverity } from '../../api/types';

const severities: IncidentSeverity[] = ['low', 'medium', 'high', 'critical'];

export function CreateIncidentDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();

  const services = useQuery({
    queryKey: queryKeys.services.all,
    queryFn: servicesApi.list,
  });

  const [serviceId, setServiceId] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [severity, setSeverity] = useState<IncidentSeverity>('medium');

  const create = useMutation({
    mutationFn: () =>
      incidentsApi.create({
        service_id: serviceId,
        title,
        description: description || null,
        severity,
      }),
    onSuccess: () => {
      // Invalidating the whole incidents tree rather than one filtered list: the new incident
      // may or may not match the filters currently on screen, and guessing wrong leaves a stale
      // table.
      void queryClient.invalidateQueries({ queryKey: queryKeys.incidents.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.services.all });
      onClose();
    },
  });

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="create-incident-title"
    >
      <form onSubmit={handleSubmit} className="panel w-full max-w-lg p-6">
        <h2 id="create-incident-title" className="mb-5 text-base font-semibold text-slate-100">
          New incident
        </h2>

        <div className="space-y-4">
          <div>
            <label htmlFor="service" className="label">
              Service
            </label>
            <select
              id="service"
              className="field"
              value={serviceId}
              onChange={(e) => setServiceId(e.target.value)}
              required
            >
              <option value="">Select a service</option>
              {services.data?.map((service) => (
                <option key={service.id} value={service.id}>
                  {service.display_name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="title" className="label">
              Title
            </label>
            <input
              id="title"
              className="field"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Payment authorisation failing for 12% of requests"
              required
            />
          </div>

          <div>
            <label htmlFor="description" className="label">
              Description
            </label>
            <textarea
              id="description"
              className="field min-h-24 resize-y"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What was observed, and when it started."
            />
          </div>

          <div>
            <span className="label">Severity</span>
            <div className="flex gap-2">
              {severities.map((option) => (
                <button
                  key={option}
                  type="button"
                  onClick={() => setSeverity(option)}
                  className={`flex-1 rounded-md border px-3 py-2 text-sm capitalize transition-colors ${
                    severity === option
                      ? 'border-accent bg-accent/10 text-accent'
                      : 'border-ink-700 text-slate-400 hover:bg-ink-800'
                  }`}
                >
                  {option}
                </button>
              ))}
            </div>
          </div>

          {create.error && (
            <p
              role="alert"
              className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm
                         text-state-bad"
            >
              {describeError(create.error)}
            </p>
          )}
        </div>

        <div className="mt-6 flex justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn-primary" disabled={create.isPending}>
            {create.isPending ? 'Creating...' : 'Create incident'}
          </button>
        </div>
      </form>
    </div>
  );
}
