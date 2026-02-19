'use client';

import { useState, useEffect, useRef } from 'react';
import Link from 'next/link';

const API = '/api';

type JobSummary = {
  id: string;
  case_name: string;
  input_folder: string;
  stage: string;
  total_calls: number;
  done_calls: number;
  error_calls: number;
  created_at: string;
  has_zip: boolean;
  error?: string;
};

const STAGE_LABELS: Record<string, string> = {
  created: 'Created',
  converting: 'Converting audio…',
  transcribing: 'Transcribing…',
  summarizing: 'Summarizing…',
  generating: 'Generating PDFs…',
  packaging: 'Packaging…',
  done: 'Done',
  error: 'Error',
};

function StatusBadge({ stage, error }: { stage: string; error?: string }) {
  const colors: Record<string, string> = {
    created: 'bg-slate-200 text-slate-700',
    converting: 'bg-blue-100 text-blue-800',
    transcribing: 'bg-violet-100 text-violet-800',
    summarizing: 'bg-amber-100 text-amber-800',
    generating: 'bg-orange-100 text-orange-800',
    packaging: 'bg-cyan-100 text-cyan-800',
    done: 'bg-green-100 text-green-800',
    error: 'bg-red-100 text-red-800',
  };
  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${colors[stage] || 'bg-slate-100 text-slate-600'}`}>
      {STAGE_LABELS[stage] || stage}
    </span>
  );
}

export default function JobsPage() {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [defaultPrompt, setDefaultPrompt] = useState('');

  const caseNameRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);

  const loadJobs = async () => {
    try {
      const res = await fetch(`${API}/jobs`);
      if (res.ok) setJobs(await res.json());
    } catch {}
    setLoading(false);
  };

  const loadConfig = async () => {
    try {
      const res = await fetch(`${API}/config`);
      if (res.ok) {
        const cfg = await res.json();
        setDefaultPrompt(cfg.default_summary_prompt || '');
      }
    } catch {}
  };

  useEffect(() => {
    loadJobs();
    loadConfig();
    const interval = setInterval(loadJobs, 5000);
    return () => clearInterval(interval);
  }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSubmitting(true);

    const body = {
      case_name: caseNameRef.current?.value.trim() || '',
      input_folder: folderRef.current?.value.trim() || '',
      summary_prompt: promptRef.current?.value.trim() || defaultPrompt,
    };

    if (!body.case_name || !body.input_folder) {
      setError('Case name and input folder are required.');
      setSubmitting(false);
      return;
    }

    try {
      const res = await fetch(`${API}/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json();
        setError(err.detail || 'Failed to create job');
      } else {
        if (caseNameRef.current) caseNameRef.current.value = '';
        if (folderRef.current) folderRef.current.value = '';
        await loadJobs();
      }
    } catch (e) {
      setError(String(e));
    }
    setSubmitting(false);
  };

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-slate-900">Jail Call Service</h1>
        <p className="mt-1 text-sm text-slate-500">
          Batch transcription and packaging for G.729 jail call recordings.
        </p>
      </div>

      {/* New Job Form */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm mb-8 p-6">
        <h2 className="text-base font-semibold text-slate-800 mb-4">New Job</h2>
        <form onSubmit={handleCreate} className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1">Case Name</label>
              <input
                ref={caseNameRef}
                type="text"
                placeholder="People v. Smith 2024"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-400 focus:border-transparent"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1">Input Folder Path</label>
              <input
                ref={folderRef}
                type="text"
                placeholder="/Users/you/jail-calls/smith"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-400 focus:border-transparent font-mono"
              />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              Summary Prompt <span className="text-slate-400 font-normal">(optional – uses default if blank)</span>
            </label>
            <textarea
              ref={promptRef}
              rows={3}
              placeholder={defaultPrompt}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-400 resize-none"
            />
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <div className="flex justify-end">
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 bg-slate-800 text-white text-sm font-medium rounded-lg hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {submitting ? 'Creating…' : 'Create Job'}
            </button>
          </div>
        </form>
      </div>

      {/* Jobs List */}
      <div>
        <h2 className="text-base font-semibold text-slate-800 mb-3">Jobs</h2>
        {loading ? (
          <div className="text-center py-12 text-slate-400 text-sm">Loading…</div>
        ) : jobs.length === 0 ? (
          <div className="bg-white rounded-xl border border-slate-200 p-12 text-center text-slate-400 text-sm">
            No jobs yet. Create one above.
          </div>
        ) : (
          <div className="space-y-3">
            {jobs.map(job => (
              <Link key={job.id} href={`/jobs/${job.id}`} className="block">
                <div className="bg-white rounded-xl border border-slate-200 shadow-sm hover:border-slate-300 hover:shadow transition-all p-4">
                  <div className="flex items-center justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-3">
                        <span className="font-semibold text-slate-900">{job.case_name}</span>
                        <StatusBadge stage={job.stage} />
                      </div>
                      <div className="mt-1 text-xs text-slate-400 font-mono truncate">{job.input_folder}</div>
                    </div>
                    <div className="text-right flex-shrink-0">
                      <div className="text-sm text-slate-600">
                        {job.done_calls}/{job.total_calls} calls
                        {job.error_calls > 0 && (
                          <span className="text-red-500 ml-1">({job.error_calls} errors)</span>
                        )}
                      </div>
                      <div className="text-xs text-slate-400 mt-0.5">
                        {new Date(job.created_at).toLocaleDateString()}
                      </div>
                    </div>
                    {job.has_zip && (
                      <a
                        href={`/api/jobs/${job.id}/download`}
                        onClick={e => e.stopPropagation()}
                        className="flex-shrink-0 px-3 py-1.5 bg-green-50 text-green-700 border border-green-200 rounded-lg text-xs font-medium hover:bg-green-100 transition-colors"
                      >
                        Download
                      </a>
                    )}
                  </div>
                  {job.stage !== 'created' && job.stage !== 'done' && job.stage !== 'error' && (
                    <div className="mt-3">
                      <div className="h-1.5 bg-slate-100 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-slate-600 rounded-full transition-all"
                          style={{ width: `${job.total_calls > 0 ? (job.done_calls / job.total_calls) * 100 : 0}%` }}
                        />
                      </div>
                    </div>
                  )}
                  {job.error && (
                    <div className="mt-2 text-xs text-red-600 bg-red-50 rounded px-2 py-1">{job.error}</div>
                  )}
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
