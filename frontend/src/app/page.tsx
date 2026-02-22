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
  defendant_name?: string;
};

type AppConfig = {
  assemblyai_configured: boolean;
  gemini_configured: boolean;
  ffmpeg_found: boolean;
  ffmpeg_path: string;
  default_summary_prompt: string;
  gemini_model: string;
};

const STAGE_LABELS: Record<string, string> = {
  created: 'Created',
  converting: 'Converting audio...',
  transcribing: 'Transcribing...',
  summarizing: 'Summarizing...',
  generating: 'Generating PDFs...',
  packaging: 'Packaging...',
  done: 'Done',
  error: 'Error',
  paused: 'Paused',
};

function StatusBadge({ stage }: { stage: string }) {
  const colors: Record<string, string> = {
    created: 'bg-slate-200 text-slate-700',
    converting: 'bg-blue-100 text-blue-800',
    transcribing: 'bg-violet-100 text-violet-800',
    summarizing: 'bg-amber-100 text-amber-800',
    generating: 'bg-orange-100 text-orange-800',
    packaging: 'bg-cyan-100 text-cyan-800',
    done: 'bg-green-100 text-green-800',
    error: 'bg-red-100 text-red-800',
    paused: 'bg-yellow-100 text-yellow-800',
  };
  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${colors[stage] || 'bg-slate-100 text-slate-600'}`}>
      {STAGE_LABELS[stage] || stage}
    </span>
  );
}

export default function JobsPage() {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const [uploading, setUploading] = useState(false);

  const caseNameRef = useRef<HTMLInputElement>(null);
  const defendantNameRef = useRef<HTMLInputElement>(null);
  const pathsRef = useRef<HTMLTextAreaElement>(null);
  const xmlRef = useRef<HTMLInputElement>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const skipSummaryRef = useRef<HTMLInputElement>(null);
  const audioInputRef = useRef<HTMLInputElement>(null);
  const xmlInputRef = useRef<HTMLInputElement>(null);

  const loadJobs = async () => {
    try {
      const res = await fetch(`${API}/jobs`);
      if (res.ok) setJobs(await res.json());
    } catch { }
    setLoading(false);
  };

  const loadConfig = async () => {
    try {
      const res = await fetch(`${API}/config`);
      if (res.ok) setConfig(await res.json());
    } catch { }
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

    const pathsStr = pathsRef.current?.value.trim() || '';
    const xmlPath = xmlRef.current?.value.trim() || '';
    const parsedPaths = pathsStr.split(/[\n,]+/).map(p => p.trim()).filter(Boolean);

    let inputFolder = '';
    let filePaths: string[] = [];

    if (parsedPaths.length === 1 && !parsedPaths[0].toLowerCase().endsWith('.wav')) {
      inputFolder = parsedPaths[0];
    } else {
      filePaths = parsedPaths;
    }

    const body = {
      case_name: caseNameRef.current?.value.trim() || '',
      defendant_name: defendantNameRef.current?.value.trim() || '',
      input_folder: inputFolder,
      file_paths: filePaths.length > 0 ? filePaths : undefined,
      xml_metadata_path: xmlPath || undefined,
      summary_prompt: promptRef.current?.value.trim() || (config?.default_summary_prompt ?? ''),
      skip_summary: skipSummaryRef.current?.checked || false,
    };

    if (!body.case_name || (!body.input_folder && (!body.file_paths || body.file_paths.length === 0))) {
      setError('Case name and input path(s) are required.');
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
        if (defendantNameRef.current) defendantNameRef.current.value = '';
        if (pathsRef.current) pathsRef.current.value = '';
        if (xmlRef.current) xmlRef.current.value = '';
        if (skipSummaryRef.current) skipSummaryRef.current.checked = false;
        if (audioInputRef.current) audioInputRef.current.value = '';
        if (xmlInputRef.current) xmlInputRef.current.value = '';
        await loadJobs();
      }
    } catch (e) {
      setError(String(e));
    }
    setSubmitting(false);
  };

  const handleDelete = async (e: React.MouseEvent, jobId: string) => {
    e.preventDefault();
    e.stopPropagation();
    if (!confirm('Delete this job and all its files?')) return;
    try {
      const res = await fetch(`${API}/jobs/${jobId}`, { method: 'DELETE' });
      if (res.ok) await loadJobs();
    } catch { }
  };

  const handleAudioUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    setUploading(true);
    setError('');
    try {
      const formData = new FormData();
      for (let i = 0; i < files.length; i++) {
        formData.append('files', files[i]);
      }
      const res = await fetch(`${API}/upload/audio`, { method: 'POST', body: formData });
      if (res.ok) {
        const data = await res.json();
        if (data.paths && pathsRef.current) {
          const existing = pathsRef.current.value.trim();
          const newPaths = (data.paths as string[]).join(',\n');
          pathsRef.current.value = existing ? `${existing},\n${newPaths}` : newPaths;
        }
      } else {
        const err = await res.json();
        setError(err.detail || 'Upload failed');
      }
    } catch (err) {
      setError(String(err));
    }
    setUploading(false);
    if (audioInputRef.current) audioInputRef.current.value = '';
  };

  const handleXmlUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    setUploading(true);
    setError('');
    try {
      const formData = new FormData();
      formData.append('file', files[0]);
      const res = await fetch(`${API}/upload/xml`, { method: 'POST', body: formData });
      if (res.ok) {
        const data = await res.json();
        if (data.path && xmlRef.current) {
          xmlRef.current.value = data.path;
        }
      } else {
        const err = await res.json();
        setError(err.detail || 'Upload failed');
      }
    } catch (err) {
      setError(String(err));
    }
    setUploading(false);
    if (xmlInputRef.current) xmlInputRef.current.value = '';
  };

  const warnings: string[] = [];
  if (config) {
    if (!config.ffmpeg_found) warnings.push('ffmpeg not found. Set FFMPEG_PATH in .env or install ffmpeg to PATH.');
    if (!config.assemblyai_configured) warnings.push('ASSEMBLYAI_API_KEY not set in .env. Transcription will fail.');
    if (!config.gemini_configured) warnings.push('GEMINI_API_KEY not set in .env. Summaries will fail (use Skip Gemini for testing).');
  }

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-slate-900">Jail Call Service</h1>
        <p className="mt-1 text-sm text-slate-500">
          Batch transcription and packaging for G.729 jail call recordings.
        </p>
      </div>

      {/* Warnings */}
      {warnings.length > 0 && (
        <div className="mb-6 bg-amber-50 border border-amber-200 rounded-xl p-4 space-y-1">
          {warnings.map((w, i) => (
            <div key={i} className="flex items-start gap-2 text-sm text-amber-800">
              <span className="mt-0.5 flex-shrink-0">!</span>
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

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
              <label className="block text-sm font-medium text-slate-700 mb-1">Defendant Name <span className="text-slate-400 font-normal">(Channel 1)</span></label>
              <input
                ref={defendantNameRef}
                type="text"
                placeholder="John Smith"
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-400 focus:border-transparent"
              />
            </div>
            <div className="col-span-2">
              <label className="block text-sm font-medium text-slate-700 mb-1">Audio Files Path(s)</label>
              <div className="flex gap-2 items-start">
                <textarea
                  ref={pathsRef}
                  rows={3}
                  placeholder={'Upload audio files or paste an absolute folder path (e.g. /Users/you/calls) or specific file paths separated by commas/newlines'}
                  className="flex-1 px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-400 focus:border-transparent font-mono resize-none"
                />
                <input
                  ref={audioInputRef}
                  type="file"
                  multiple
                  accept=".wav,.mp3,.m4a"
                  onChange={handleAudioUpload}
                  className="hidden"
                />
                <button
                  type="button"
                  onClick={() => audioInputRef.current?.click()}
                  disabled={uploading}
                  className="px-3 py-2 bg-slate-100 border border-slate-300 rounded-lg text-sm font-medium text-slate-700 hover:bg-slate-200 focus:outline-none focus:ring-2 focus:ring-slate-400 transition-colors shrink-0 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {uploading ? 'Uploading...' : 'Choose Audio Files...'}
                </button>
              </div>
            </div>
            <div className="col-span-2">
              <label className="block text-sm font-medium text-slate-700 mb-1">ICM Metadata XML Path <span className="text-slate-400 font-normal">(optional)</span></label>
              <div className="flex gap-2 items-center">
                <input
                  ref={xmlRef}
                  type="text"
                  placeholder={'Upload XML or paste path (e.g. /Users/you/calls/ICM_report.xml)'}
                  className="flex-1 px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-400 focus:border-transparent font-mono"
                />
                <input
                  ref={xmlInputRef}
                  type="file"
                  accept=".xml"
                  onChange={handleXmlUpload}
                  className="hidden"
                />
                <button
                  type="button"
                  onClick={() => xmlInputRef.current?.click()}
                  disabled={uploading}
                  className="px-3 py-2 bg-slate-100 border border-slate-300 rounded-lg text-sm font-medium text-slate-700 hover:bg-slate-200 focus:outline-none focus:ring-2 focus:ring-slate-400 transition-colors shrink-0 whitespace-nowrap disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {uploading ? 'Uploading...' : 'Upload XML...'}
                </button>
              </div>
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              Summary Prompt <span className="text-slate-400 font-normal">(optional - uses default if blank)</span>
            </label>
            <textarea
              ref={promptRef}
              rows={3}
              placeholder={config?.default_summary_prompt || ''}
              className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-400 resize-none"
            />
          </div>
          <div className="flex items-center gap-2 mt-2">
            <input
              type="checkbox"
              id="skipSummary"
              ref={skipSummaryRef}
              className="rounded border-slate-300 text-slate-800 focus:ring-slate-400"
            />
            <label htmlFor="skipSummary" className="text-sm font-medium text-slate-700">
              Skip Gemini Summary (Generate Dummy Summary for Testing)
            </label>
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <div className="flex justify-end">
            <button
              type="submit"
              disabled={submitting || uploading}
              className="px-4 py-2 bg-slate-800 text-white text-sm font-medium rounded-lg hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {uploading ? 'Uploading...' : submitting ? 'Creating...' : 'Create Job'}
            </button>
          </div>
        </form>
      </div>

      {/* Jobs List */}
      <div>
        <h2 className="text-base font-semibold text-slate-800 mb-3">Jobs</h2>
        {loading ? (
          <div className="text-center py-12 text-slate-400 text-sm">Loading...</div>
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
                      <div className="mt-1 text-xs text-slate-400 font-mono truncate">
                        {job.defendant_name ? `${job.defendant_name} - ` : ''}{job.input_folder}
                      </div>
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
                    <div className="flex gap-2 flex-shrink-0">
                      {job.has_zip && (
                        <a
                          href={`/api/jobs/${job.id}/download`}
                          onClick={e => e.stopPropagation()}
                          className="px-3 py-1.5 bg-green-50 text-green-700 border border-green-200 rounded-lg text-xs font-medium hover:bg-green-100 transition-colors"
                        >
                          Download
                        </a>
                      )}
                      {['created', 'done', 'error'].includes(job.stage) && (
                        <button
                          onClick={e => handleDelete(e, job.id)}
                          className="px-2 py-1.5 text-slate-400 hover:text-red-600 hover:bg-red-50 rounded-lg text-xs transition-colors"
                          title="Delete job"
                        >
                          Delete
                        </button>
                      )}
                    </div>
                  </div>
                  {!['created', 'done', 'error'].includes(job.stage) && (
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
