import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '../../components/Layout';
import { queryKeys, servicesApi } from '../../api/endpoints';
import { describeError } from '../../api/client';

export function ServicesPage() {
  const { data, isPending, error } = useQuery({
    queryKey: queryKeys.services.all,
    queryFn: servicesApi.list,
  });

  return (
    <>
      <PageHeader
        title="Services"
        description="The monitored estate. Service names are the key that joins logs, metrics and traces."
      />

      <div className="p-8">
        {isPending && <p className="text-sm text-slate-500">Loading services...</p>}

        {error && (
          <p className="rounded border border-state-bad/30 bg-state-bad/10 px-3 py-2 text-sm text-state-bad">
            {describeError(error)}
          </p>
        )}

        {data && (
          <div className="panel overflow-hidden">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-ink-800 bg-ink-850">
                <tr className="text-xs uppercase tracking-wide text-slate-500">
                  <th className="px-4 py-3 font-medium">Service</th>
                  <th className="px-4 py-3 font-medium">Name</th>
                  <th className="px-4 py-3 font-medium">Repository</th>
                  <th className="px-4 py-3 text-right font-medium">Open incidents</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-800">
                {data.map((service) => (
                  <tr key={service.id} className="hover:bg-ink-850/50">
                    <td className="px-4 py-3 text-slate-200">{service.display_name}</td>
                    <td className="px-4 py-3">
                      <span className="identifier text-slate-400">{service.name}</span>
                    </td>
                    <td className="px-4 py-3">
                      <span className="identifier text-slate-500">{service.repo_path ?? '-'}</span>
                    </td>
                    <td className="px-4 py-3 text-right">
                      {service.open_incident_count > 0 ? (
                        <span className="font-medium text-state-warn">
                          {service.open_incident_count}
                        </span>
                      ) : (
                        <span className="text-slate-600">0</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            {data.length === 0 && (
              <p className="px-4 py-8 text-center text-sm text-slate-500">
                No services registered. The five sample services are seeded at backend startup.
              </p>
            )}
          </div>
        )}
      </div>
    </>
  );
}
