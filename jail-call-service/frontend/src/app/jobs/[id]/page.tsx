'use client';

import { useState, useEffect, useRef } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';

const API = '/api';

type CallSummary = {
  index: number;
  filename: string;
  status: string;
  duration_seconds?: number;
  has_transcript: boolean;
  has_summary: boolean;
  repaired: boolean;
  error?: string;
  inmate_name?: string;
  call_datetime_str?: string;
  outside_number_fmt?: string;
  facility?: string;
  call_outcome?: string;
};

type JobDetail = {
  id: string;
  case_name: string;
  input_folder: string;
  stage: string;
  total_calls: number;
  done_calls: number;
  error_calls: number;
  created_at: string;
  started_at?: string;
  completed_at?: string;
  has_zip: boolean;
  error?: string;
  calls: CallSummary[];
};

const STATUS_COLORS: Record<string, string> = {
  pending: 'text-slate-400',
  repairing: 'text-blue-500',
  converting: 'text-blue-600',
  transcribing: 'text-violet-600',
  summarizing: 'text-amber-600',
  generating_pdf: 'text-orange-600',
  done: 'text-green-600',
  error: 'text-red-500',
};

const STATUS_LABELS: Record<string, string> = {
  pending: 'Pending',
  repairing: 'Repairing header…',
  converting: 'Converting…',
  transcribing: 'Transcribing…',
  summarizing: 'Summarizing…',
  generating_pdf: 'Generating PDF…',
  done: 'Done',
  error: 'Error',
};

const STAGE_STEPS = [
  { key: 'converting', label: 'Convert' },
  { key: 'transcribing', label: 'Transcribe' },
  { key: 'summarizing', label: 'Summarize' },
  { key: 'generating', label: 'Generate' },
  { key: 'packaging', label: 'Package' },
  { key: 'done', label: 'Done' },
];

function formatDuration(s?: number): string {
  if (!s) return '–';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

function StageIndicator({ stage }: { stage: string }) {
  const stageOrder = STAGE_STEPS.map(s => s.key);
  const currentIdx = stageOrder.indexOf(stage);

  return (
    <div className="flex items-center gap-0">
      {STAGE_STEPS.map((step, idx) => {
        const isDone = idx < currentIdx || stage === 'done';
        const isActive = step.key === stage;
        return (
          <div key={step.key} className="flex items-center">
            <div className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              isDone ? 'bg-green-100 text-green-700' :
              isActive ? 'bg-slate-800 text-white' :
              'bg-slate-100 text-slate-400'
            }`}>
              {isDone && <span>✓</span>}
              {step.label}
            </div>
            {idx < STAGE_STEPS.length - 1 && (
              <div className={`w-6 h-0.5 ${isDone ? 'bg-green-300' : 'bg-slate-200'}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function JobDetailPage() {
  const params = useParams();
  const router = useRouter();
  const jobId = params.id as string;

  const [job, setJob] = useState<JobDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [packaging, setPackaging] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);

  const loadJob = async () => {
    try {
      const res = await fetch(`${API}/jobs/${jobId}`);
      if (!res.ok) { router.push('/'); return; }
      setJob(await res.json());
    } catch {}
    setLoading(false);
  };

  const connectSSE = () => {
    if (eventSourceRef.current) eventSourceRef.current.close();
    const es = new EventSource(`${API}/jobs/${jobId}/events`);
    es.onmessage = async (e) => {
      const event = JSON.parse(e.data);
      if (event.type === 'done' || event.type === 'error') {
        es.close();
        await loadJob();
      } else if (event.type !== 'ping') {
        await loadJob();
      }
    };
    es.onerror = () => es.close();
    eventSourceRef.current = es;
  };

  useEffect(() => {
    loadJob();
    return () => { eventSourceRef.current?.close(); };
  }, [jobId]);

  const handleStart = async () => {
    setStarting(true);
    try {
      const res = await fetch(`${API}/jobs/${jobId}/start`, { method: 'POST' });
      if (res.ok) {
        connectSSE();
        await loadJob();
      }
    } catch {}
    setStarting(false);
  };

  const handlePackage = async () => {
    setPackaging(true);
    try {
      const res = await fetch(`${API}/jobs/${jobId}/package`, { method: 'POST' });
      if (res.ok) {
        connectSSE();
        await loadJob();
      }
    } catch {}
    setPackaging(false);
  };

  if (loading) {
    return <div className="flex items-center justify-center h-64 text-slate-400">Loading…</div>;
  }

  if (!job) {
    return <div className="flex items-center justify-center h-64 text-red-500">Job not found.</div>;
  }

  const isRunning = !['created', 'done', 'error'].includes(job.stage);
  const pct = job.total_calls > 0 ? Math.round((job.done_calls / job.total_calls) * 100) : 0;

  return (
    <div className="max-w-6xl mx-auto px-4 py-8">
      {/* Breadcrumb */}
      <div className="mb-6 flex items-center gap-2 text-sm text-slate-500">
        <Link href="/" className="hover:text-slate-900 transition-colors">Jobs</Link>
        <span>/</span>
        <span className="text-slate-900 font-medium">{job.case_name}</span>
      </div>

      {/* Header */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-xl font-bold text-slate-900">{job.case_name}</h1>
            <p className="text-sm text-slate-400 font-mono mt-1">{job.input_folder}</p>
          </div>
          <div className="flex gap-3 flex-shrink-0">
            {job.stage === 'created' && (
              <button
                onClick={handleStart}
                disabled={starting}
                className="px-4 py-2 bg-slate-800 text-white text-sm font-medium rounded-lg hover:bg-slate-700 disabled:opacity-50 transition-colors"
              >
                {starting ? 'Starting…' : 'Start Processing'}
              </button>
            )}
            {job.stage === 'done' && (
              <>
                <button
                  onClick={handlePackage}
                  disabled={packaging}
                  className="px-4 py-2 bg-white border border-slate-300 text-slate-700 text-sm font-medium rounded-lg hover:bg-slate-50 disabled:opacity-50 transition-colors"
                >
                  {packaging ? 'Packaging…' : 'Re-package'}
                </button>
                <Link
                  href={`/jobs/${jobId}/review`}
                  className="px-4 py-2 bg-violet-600 text-white text-sm font-medium rounded-lg hover:bg-violet-700 transition-colors"
                >
                  Review Transcripts
                </Link>
                {job.has_zip && (
                  <a
                    href={`/api/jobs/${jobId}/download`}
                    className="px-4 py-2 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 transition-colors"
                  >
                    Download Zip
                  </a>
                )}
              </>
            )}
          </div>
        </div>

        {/* Stage indicator */}
        <div className="mt-5">
          <StageIndicator stage={job.stage} />
        </div>

        {/* Progress bar */}
        {isRunning && (
          <div className="mt-4">
            <div className="flex justify-between text-xs text-slate-500 mb-1">
              <span>{job.done_calls} / {job.total_calls} calls done</span>
              <span>{pct}%</span>
            </div>
            <div className="h-2 bg-slate-100 rounded-full overflow-hidden">
              <div
                className="h-full bg-slate-700 rounded-full transition-all duration-500"
                style={{ width: `${pct}%` }}
              />
            </div>
          </div>
        )}

        {job.error && (
          <div className="mt-4 bg-red-50 border border-red-200 rounded-lg px-4 py-3 text-sm text-red-700">
            {job.error}
          </div>
        )}

        {/* Stats */}
        <div className="mt-4 flex gap-6 text-sm">
          <div><span className="text-slate-400">Total:</span> <span className="font-medium">{job.total_calls}</span></div>
          <div><span className="text-slate-400">Done:</span> <span className="font-medium text-green-700">{job.done_calls}</span></div>
          {job.error_calls > 0 && (
            <div><span className="text-slate-400">Errors:</span> <span className="font-medium text-red-600">{job.error_calls}</span></div>
          )}
        </div>
      </div>

      {/* Calls table */}
      {job.calls.length > 0 && (
        <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
          <div className="px-6 py-3 border-b border-slate-100">
            <h2 className="text-sm font-semibold text-slate-700">Calls ({job.calls.length})</h2>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50">
                  <th className="px-4 py-2 text-left text-xs text-slate-400 font-medium w-10">#</th>
                  <th className="px-4 py-2 text-left text-xs text-slate-400 font-medium w-36">Date/Time</th>
                  <th className="px-4 py-2 text-left text-xs text-slate-400 font-medium w-32">Inmate</th>
                  <th className="px-4 py-2 text-left text-xs text-slate-400 font-medium w-28">Outside #</th>
                  <th className="px-4 py-2 text-left text-xs text-slate-400 font-medium">Filename</th>
                  <th className="px-4 py-2 text-left text-xs text-slate-400 font-medium w-20">Duration</th>
                  <th className="px-4 py-2 text-left text-xs text-slate-400 font-medium w-32">Status</th>
                  <th className="px-4 py-2 text-left text-xs text-slate-400 font-medium w-28">Flags</th>
                </tr>
              </thead>
              <tbody>
                {job.calls.map(call => (
                  <tr key={call.index} className="border-b border-slate-50 hover:bg-slate-50 transition-colors">
                    <td className="px-4 py-2.5 text-slate-400 tabular-nums">{call.index + 1}</td>
                    <td className="px-4 py-2.5 text-slate-600 tabular-nums text-xs">{call.call_datetime_str || '–'}</td>
                    <td className="px-4 py-2.5 text-slate-700 text-xs">{call.inmate_name || '–'}</td>
                    <td className="px-4 py-2.5 text-slate-500 text-xs tabular-nums">{call.outside_number_fmt || '–'}</td>
                    <td className="px-4 py-2.5 font-mono text-xs text-slate-700">{call.filename}</td>
                    <td className="px-4 py-2.5 text-slate-500 tabular-nums">{formatDuration(call.duration_seconds)}</td>
                    <td className={`px-4 py-2.5 font-medium ${STATUS_COLORS[call.status] || 'text-slate-500'}`}>
                      {STATUS_LABELS[call.status] || call.status}
                      {call.error && (
                        <div className="text-xs text-red-400 font-normal truncate max-w-xs" title={call.error}>{call.error}</div>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex gap-1 flex-wrap">
                        {call.repaired && (
                          <span className="inline-block px-1.5 py-0.5 bg-amber-50 text-amber-700 border border-amber-200 rounded text-xs">Repaired</span>
                        )}
                        {call.has_transcript && (
                          <span className="inline-block px-1.5 py-0.5 bg-green-50 text-green-700 border border-green-200 rounded text-xs">Transcript</span>
                        )}
                        {call.has_summary && (
                          <span className="inline-block px-1.5 py-0.5 bg-violet-50 text-violet-700 border border-violet-200 rounded text-xs">Summary</span>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
