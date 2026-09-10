'use client';

import { useState, useEffect, useRef } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';

import {
  api, errorMessage, extractCaseContext, formatDuration, formatElapsed, isRunning, IDLE_STAGES,
  type JobAction, type JobDetail,
} from '@/lib/api';

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

const ACTION_VERBS: Record<JobAction, string> = {
  start: 'start job',
  pause: 'pause',
  resume: 'resume',
  'retry-errors': 'retry',
  package: 'package',
};

const SSE_MAX_RETRIES = 5;
const POLL_INTERVAL_MS = 3000;

function StageIndicator({ stage }: { stage: string }) {
  const currentIdx = STAGE_STEPS.findIndex(s => s.key === stage);
  return (
    <div className="flex items-center gap-0 flex-wrap">
      {STAGE_STEPS.map((step, idx) => {
        const isDone = stage === 'done' || (currentIdx >= 0 && idx < currentIdx);
        const isActive = step.key === stage;
        const isPaused = stage === 'paused' && idx === 0;
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
  const [busy, setBusy] = useState<JobAction | null>(null);
  const [fetchError, setFetchError] = useState('');
  const [now, setNow] = useState(() => Date.now());

  const eventSourceRef = useRef<EventSource | null>(null);
  const initialLoadDone = useRef(false);
  const sseRetriesRef = useRef(0);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadJob = async (): Promise<JobDetail | null> => {
    try {
      const data = await api.jobs.get(jobId);
      setJob(data);
      setFetchError('');
      return data;
    } catch (e) {
      // Only redirect on the initial load; a transient error while polling
      // should not bounce the user off the page.
      if (!initialLoadDone.current) router.push('/');
      else setFetchError(`Failed to load job: ${errorMessage(e)}`);
      return null;
    }
  };

  const stopPolling = () => {
    if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null; }
  };

  const startPolling = () => {
    stopPolling();
    pollingRef.current = setInterval(loadJob, POLL_INTERVAL_MS);
  };

  // Live progress over SSE, with retry and a polling fallback.
  const connectSSE = () => {
    eventSourceRef.current?.close();
    stopPolling();
    const es = new EventSource(api.jobs.eventsUrl(jobId));
    es.onmessage = async (e) => {
      sseRetriesRef.current = 0;
      const event = JSON.parse(e.data);
      if (event.type === 'done' || event.type === 'error') {
        es.close();
        eventSourceRef.current = null;
        stopPolling();
        await loadJob();
      } else if (event.type !== 'ping') {
        await loadJob();
      }
    };
    es.onerror = () => {
      es.close();
      eventSourceRef.current = null;
      sseRetriesRef.current += 1;
      if (sseRetriesRef.current < SSE_MAX_RETRIES) {
        setTimeout(connectSSE, Math.min(1000 * sseRetriesRef.current, 5000));
      } else {
        startPolling();
      }
    };
    eventSourceRef.current = es;
  };

  useEffect(() => {
    loadJob().then(data => {
      initialLoadDone.current = true;
      setLoading(false);
      if (data && isRunning(data.stage)) connectSSE();
      startPolling();
    });
    const ticker = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      eventSourceRef.current?.close();
      stopPolling();
      clearInterval(ticker);
    };
  }, [jobId]);

  const runAction = async (action: JobAction, reconnect: boolean) => {
    setBusy(action);
    setFetchError('');
    try {
      await api.jobs.action(jobId, action);
      if (reconnect) connectSSE();
      await loadJob();
    } catch (e) {
      setFetchError(`Failed to ${ACTION_VERBS[action]}: ${errorMessage(e)}`);
    }
    setBusy(null);
  };

  const handleDelete = async () => {
    if (!confirm('Delete this job and all its files?')) return;
    try {
      await api.jobs.delete(jobId);
      router.push('/');
    } catch (e) {
      setFetchError(`Failed to delete: ${errorMessage(e)}`);
    }
  };

  if (loading) {
    return <div className="flex items-center justify-center h-64 text-slate-400">Loading...</div>;
  }
  if (!job) {
    return <div className="flex items-center justify-center h-64 text-red-500">Job not found.</div>;
  }

  const running = isRunning(job.stage);
  const pct = job.total_calls > 0 ? Math.round((job.done_calls / job.total_calls) * 100) : 0;
  const jobTitle = job.case_name || 'Jail Calls';
  const inmateSpeakerSide = job.speaker_assignment === 'right_inmate' ? 'Right' : 'Left';
  const caseContext = extractCaseContext(job.summary_prompt);
  const elapsed = job.stage === 'created' ? '' : formatElapsed(job.started_at, job.completed_at, now);

  const actionButton = (action: JobAction, label: string, busyLabel: string, className: string, reconnect = true) => (
    <button
      onClick={() => runAction(action, reconnect)}
      disabled={busy === action}
      className={`px-4 py-2 text-sm font-medium rounded-lg disabled:opacity-50 transition-colors ${className}`}
    >
      {busy === action ? busyLabel : label}
    </button>
  );

  return (
    <div className="max-w-6xl mx-auto px-4 py-8">
      {/* Breadcrumb */}
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <Link href="/" className="hover:text-slate-900 transition-colors">Jobs</Link>
          <span>/</span>
          <span className="text-slate-900 font-medium">{jobTitle}</span>
        </div>
        {IDLE_STAGES.includes(job.stage) && (
          <button onClick={handleDelete} className="text-xs text-slate-400 hover:text-red-600 transition-colors">
            Delete Job
          </button>
        )}
      </div>

      {/* Header */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-xl font-bold text-slate-900">{jobTitle}</h1>
            <p className="text-sm text-slate-400 font-mono mt-1">{job.input_folder}</p>
            {caseContext && (
              <div className="mt-3 text-sm text-slate-600 bg-slate-50 rounded-lg px-4 py-3 border border-slate-200">
                <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Case Context</div>
                <div className="whitespace-pre-wrap">{caseContext}</div>
              </div>
            )}
          </div>
          <div className="flex gap-3 flex-shrink-0">
            {job.stage === 'created' && actionButton('start', 'Start Existing Job', 'Starting...', 'bg-slate-800 text-white hover:bg-slate-700')}
            {running && actionButton('pause', 'Pause', 'Pausing...', 'bg-amber-100 text-amber-800 hover:bg-amber-200', false)}
            {job.stage === 'paused' && actionButton('resume', 'Resume Processing', 'Resuming...', 'bg-slate-800 text-white hover:bg-slate-700')}
            {job.error_calls > 0 && !running && actionButton('retry-errors', `Retry ${job.error_calls} Errors`, 'Retrying...', 'bg-red-100 text-red-800 hover:bg-red-200')}
            {job.stage === 'done' && (
              <>
                {actionButton('package', 'Re-package', 'Packaging...', 'bg-white border border-slate-300 text-slate-700 hover:bg-slate-50')}
                <Link
                  href={`/jobs/${jobId}/review`}
                  className="px-4 py-2 bg-violet-600 text-white text-sm font-medium rounded-lg hover:bg-violet-700 transition-colors"
                >
                  Review Transcripts
                </Link>
                {job.has_zip && (
                  <a
                    href={api.jobs.downloadUrl(jobId)}
                    className="px-4 py-2 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 transition-colors"
                  >
                    Download Zip
                  </a>
                )}
              </>
            )}
          </div>
        </div>

        <div className="mt-5">
          <StageIndicator stage={job.stage} />
        </div>

        {(running || job.stage === 'paused') && (
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

        {fetchError && (
          <div className="mt-4 bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 text-sm text-amber-700 flex items-center justify-between">
            <span>{fetchError}</span>
            <button onClick={() => setFetchError('')} className="text-amber-500 hover:text-amber-700 ml-2">&times;</button>
          </div>
        )}

        <div className="mt-4 flex gap-6 text-sm">
          <div><span className="text-slate-400">Total:</span> <span className="font-medium">{job.total_calls}</span></div>
          <div><span className="text-slate-400">Done:</span> <span className="font-medium text-green-700">{job.done_calls}</span></div>
          <div><span className="text-slate-400">Inmate Speaker:</span> <span className="font-medium">{inmateSpeakerSide}</span></div>
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
