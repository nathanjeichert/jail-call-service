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
  defendant_name?: string;
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
  repairing: 'Repairing header...',
  converting: 'Converting...',
  transcribing: 'Transcribing...',
  summarizing: 'Summarizing...',
  generating_pdf: 'Generating PDF...',
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
  if (!s) return '-';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

function formatElapsed(startedAt?: string): string {
  if (!startedAt) return '';
  const start = new Date(startedAt).getTime();
  if (isNaN(start)) return '';
  const elapsed = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const h = Math.floor(elapsed / 3600);
  const m = Math.floor((elapsed % 3600) / 60);
  const s = elapsed % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function StageIndicator({ stage }: { stage: string }) {
  const stageOrder = STAGE_STEPS.map(s => s.key);
  // For "paused" state, find where we were paused (keep that step highlighted)
  const effectiveStage = stage === 'paused' || stage === 'error' ? stage : stage;
  const currentIdx = stageOrder.indexOf(effectiveStage);

  return (
    <div className="flex items-center gap-0 flex-wrap">
      {STAGE_STEPS.map((step, idx) => {
        const isDone = stage === 'done' || (currentIdx >= 0 && idx < currentIdx);
        const isActive = step.key === stage;
        const isPaused = stage === 'paused' && idx === 0; // show first incomplete step
        return (
          <div key={step.key} className="flex items-center">
            <div className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              isDone ? 'bg-green-100 text-green-700' :
              isActive ? 'bg-slate-800 text-white' :
              isPaused ? 'bg-yellow-100 text-yellow-800' :
              'bg-slate-100 text-slate-400'
            }`}>
              {isDone && <span>&#10003;</span>}
              {step.label}
            </div>
            {idx < STAGE_STEPS.length - 1 && (
              <div className={`w-6 h-0.5 ${isDone ? 'bg-green-300' : 'bg-slate-200'}`} />
            )}
          </div>
        );
      })}
      {stage === 'paused' && (
        <div className="ml-3 px-3 py-1.5 bg-yellow-100 text-yellow-800 rounded-lg text-xs font-medium">
          PAUSED
        </div>
      )}
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
  const [pausing, setPausing] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [elapsed, setElapsed] = useState('');
  const eventSourceRef = useRef<EventSource | null>(null);

  const loadJob = async () => {
    try {
      const res = await fetch(`${API}/jobs/${jobId}`);
      if (!res.ok) { router.push('/'); return null; }
      const data = await res.json();
      setJob(data);
      return data;
    } catch { }
    setLoading(false);
    return null;
  };

  const connectSSE = () => {
    if (eventSourceRef.current) eventSourceRef.current.close();
    const es = new EventSource(`${API}/jobs/${jobId}/events`);
    es.onmessage = async (e) => {
      const event = JSON.parse(e.data);
      if (event.type === 'done' || event.type === 'error') {
        es.close();
        eventSourceRef.current = null;
        await loadJob();
      } else if (event.type !== 'ping') {
        await loadJob();
      }
    };
    es.onerror = () => {
      es.close();
      eventSourceRef.current = null;
    };
    eventSourceRef.current = es;
  };

  useEffect(() => {
    let pollInterval: ReturnType<typeof setInterval> | null = null;
    let elapsedInterval: ReturnType<typeof setInterval> | null = null;

    loadJob().then(data => {
      setLoading(false);
      if (data && !['created', 'done', 'error'].includes(data.stage)) {
        connectSSE();
      }
      // Polling fallback
      pollInterval = setInterval(() => { loadJob(); }, 3000);
      // Elapsed time ticker
      elapsedInterval = setInterval(() => {
        setJob(prev => {
          if (prev?.started_at && !['created', 'done'].includes(prev.stage)) {
            setElapsed(formatElapsed(prev.started_at));
          } else if (prev?.started_at && prev?.completed_at) {
            // Show final elapsed
            const start = new Date(prev.started_at).getTime();
            const end = new Date(prev.completed_at).getTime();
            const secs = Math.max(0, Math.floor((end - start) / 1000));
            const h = Math.floor(secs / 3600);
            const m = Math.floor((secs % 3600) / 60);
            const s = secs % 60;
            setElapsed(h > 0 ? `${h}h ${m}m ${s}s` : m > 0 ? `${m}m ${s}s` : `${s}s`);
          }
          return prev;
        });
      }, 1000);
    });

    return () => {
      eventSourceRef.current?.close();
      if (pollInterval) clearInterval(pollInterval);
      if (elapsedInterval) clearInterval(elapsedInterval);
    };
  }, [jobId]);

  const handleStart = async () => {
    setStarting(true);
    try {
      const res = await fetch(`${API}/jobs/${jobId}/start`, { method: 'POST' });
      if (res.ok) {
        connectSSE();
        await loadJob();
      }
    } catch { }
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
    } catch { }
    setPackaging(false);
  };

  const handlePause = async () => {
    setPausing(true);
    try {
      const res = await fetch(`${API}/jobs/${jobId}/pause`, { method: 'POST' });
      if (res.ok) await loadJob();
    } catch { }
    setPausing(false);
  };

  const handleResume = async () => {
    setResuming(true);
    try {
      const res = await fetch(`${API}/jobs/${jobId}/resume`, { method: 'POST' });
      if (res.ok) { connectSSE(); await loadJob(); }
    } catch { }
    setResuming(false);
  };

  const handleRetryErrors = async () => {
    setRetrying(true);
    try {
      const res = await fetch(`${API}/jobs/${jobId}/retry-errors`, { method: 'POST' });
      if (res.ok) { connectSSE(); await loadJob(); }
    } catch { }
    setRetrying(false);
  };

  const handleDelete = async () => {
    if (!confirm('Delete this job and all its files?')) return;
    try {
      const res = await fetch(`${API}/jobs/${jobId}`, { method: 'DELETE' });
      if (res.ok) router.push('/');
    } catch { }
  };

  if (loading) {
    return <div className="flex items-center justify-center h-64 text-slate-400">Loading...</div>;
  }

  if (!job) {
    return <div className="flex items-center justify-center h-64 text-red-500">Job not found.</div>;
  }

  const isRunning = !['created', 'done', 'error', 'paused'].includes(job.stage);
  const pct = job.total_calls > 0 ? Math.round((job.done_calls / job.total_calls) * 100) : 0;

  return (
    <div className="max-w-6xl mx-auto px-4 py-8">
      {/* Breadcrumb */}
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <Link href="/" className="hover:text-slate-900 transition-colors">Jobs</Link>
          <span>/</span>
          <span className="text-slate-900 font-medium">{job.case_name}</span>
        </div>
        {['created', 'done', 'error'].includes(job.stage) && (
          <button
            onClick={handleDelete}
            className="text-xs text-slate-400 hover:text-red-600 transition-colors"
          >
            Delete Job
          </button>
        )}
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
                {starting ? 'Starting...' : 'Start Processing'}
              </button>
            )}
            {isRunning && (
              <button
                onClick={handlePause}
                disabled={pausing}
                className="px-4 py-2 bg-amber-100 text-amber-800 text-sm font-medium rounded-lg hover:bg-amber-200 disabled:opacity-50 transition-colors"
              >
                {pausing ? 'Pausing...' : 'Pause'}
              </button>
            )}
            {job.stage === 'paused' && (
              <button
                onClick={handleResume}
                disabled={resuming}
                className="px-4 py-2 bg-slate-800 text-white text-sm font-medium rounded-lg hover:bg-slate-700 disabled:opacity-50 transition-colors"
              >
                {resuming ? 'Resuming...' : 'Resume Processing'}
              </button>
            )}
            {job.error_calls > 0 && !isRunning && (
              <button
                onClick={handleRetryErrors}
                disabled={retrying}
                className="px-4 py-2 bg-red-100 text-red-800 text-sm font-medium rounded-lg hover:bg-red-200 disabled:opacity-50 transition-colors"
              >
                {retrying ? 'Retrying...' : `Retry ${job.error_calls} Errors`}
              </button>
            )}
            {job.stage === 'done' && (
              <>
                <button
                  onClick={handlePackage}
                  disabled={packaging}
                  className="px-4 py-2 bg-white border border-slate-300 text-slate-700 text-sm font-medium rounded-lg hover:bg-slate-50 disabled:opacity-50 transition-colors"
                >
                  {packaging ? 'Packaging...' : 'Re-package'}
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
        {(isRunning || job.stage === 'paused') && (
          <div className="mt-4">
            <div className="flex justify-between text-xs text-slate-500 mb-1">
              <span>{job.done_calls} / {job.total_calls} calls done</span>
              <div className="flex gap-4">
                {elapsed && <span className="tabular-nums">{elapsed}</span>}
                <span>{pct}%</span>
              </div>
            </div>
            <div className="h-2 bg-slate-100 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-500 ${job.stage === 'paused' ? 'bg-yellow-500' : 'bg-slate-700'}`}
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
          {elapsed && (job.stage === 'done' || job.stage === 'error') && (
            <div><span className="text-slate-400">Elapsed:</span> <span className="font-medium tabular-nums">{elapsed}</span></div>
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
                    <td className="px-4 py-2.5 text-slate-600 tabular-nums text-xs">{call.call_datetime_str || '-'}</td>
                    <td className="px-4 py-2.5 text-slate-700 text-xs">{call.inmate_name || '-'}</td>
                    <td className="px-4 py-2.5 text-slate-500 text-xs tabular-nums">{call.outside_number_fmt || '-'}</td>
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
