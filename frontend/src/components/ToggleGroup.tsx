'use client';

import type { ReactNode } from 'react';

export type ToggleOption = { value: string; label: string };

/** A labelled row of mutually exclusive pill buttons (engine, speaker side, message handling). */
export function ToggleGroup({ label, options, value, onChange, hint, className = 'col-span-2' }: {
  label: string;
  options: ToggleOption[];
  value: string;
  onChange: (value: string) => void;
  hint?: ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <label className="block text-sm font-medium text-slate-700 mb-1">{label}</label>
      <div className="flex gap-3">
        {options.map(opt => (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            className={`px-4 py-2 rounded-lg text-sm font-medium border transition-colors ${
              value === opt.value
                ? 'bg-slate-800 text-white border-slate-800'
                : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
      {hint && <p className="text-xs text-slate-400 mt-1">{hint}</p>}
    </div>
  );
}
