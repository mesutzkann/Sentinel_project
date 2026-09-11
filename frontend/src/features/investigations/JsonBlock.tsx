import { useState } from 'react';

/**
 * The raw document behind a summary, shown only when asked for.
 *
 * Every conclusion on this screen is a sentence a model wrote, and the reason to trust any of it
 * is that the bytes a tool returned are one click away. Collapsed by default because thirteen
 * expanded payloads is not a timeline any more.
 */
export function JsonBlock({
  value,
  label = 'raw',
}: {
  value: unknown;
  label?: string;
}) {
  const [open, setOpen] = useState(false);

  if (value === null || value === undefined) {
    return null;
  }

  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="text-[11px] font-medium text-slate-500 hover:text-slate-300"
      >
        {open ? '▾' : '▸'} {label}
      </button>

      {open && (
        <pre
          className="mt-1 max-h-64 overflow-auto rounded border border-ink-800 bg-ink-950 p-2
                     font-mono text-[11px] leading-relaxed text-slate-400"
        >
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </div>
  );
}
