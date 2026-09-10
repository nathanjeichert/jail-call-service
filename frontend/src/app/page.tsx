'use client';

import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';

import { ToggleGroup } from '@/components/ToggleGroup';
import {
  api, errorMessage, extractCaseContext, isRunning,
  type AppConfig, type CreateJobBody, type EngineInfo, type JobSummary, type XmlPreview,
} from '@/lib/api';

const AUDIO_EXTS = ['.wav', '.mp3', '.m4a'];
const DEFAULT_AUTO_MESSAGE_MODE = 'label';
const DEFAULT_SPEAKER_ASSIGNMENT = 'left_inmate';

const SPEAKER_OPTIONS = [
  { value: 'left_inmate', label: 'Left = Inmate / Defendant' },
  { value: 'right_inmate', label: 'Right = Inmate / Defendant' },
];

const AUTO_MESSAGE_OPTIONS = [
  { value: '', label: 'Keep' },
  { value: 'exclude', label: 'Exclude' },
  { value: 'label', label: 'Label as Speaker' },
];

const AUTO_MESSAGE_HINTS: Record<string, string> = {
  exclude: 'IVR prompts, time warnings, and provider messages will be removed from transcripts',
  label: 'Automated messages will appear as "AUTOMATED MESSAGE:" speaker in transcripts',
  '': 'Telecom system audio (IVR prompts, time warnings) will be included as-is',
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

const STAGE_COLORS: Record<string, string> = {
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

const isAudioPath = (val: string) => AUDIO_EXTS.some(ext => val.toLowerCase().endsWith(ext));
const splitPaths = (val: string) => val.split(/[\n,]+/).map(p => p.trim()).filter(Boolean);
const countAudioPaths = (val: string) => splitPaths(val).filter(isAudioPath).length;

function titleCaseName(name: string): string {
  return name.toLowerCase().replace(/(^|[\s\-'])([a-z])/g, (_m, sep, ch) => sep + ch.toUpperCase());
}

function StatusBadge({ stage }: { stage: string }) {
  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${STAGE_COLORS[stage] || 'bg-slate-100 text-slate-600'}`}>
      {STAGE_LABELS[stage] || stage}
    </span>
  );
}

const inputClass = 'w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-slate-400 focus:border-transparent';
const secondaryButtonClass = 'px-3 py-2 bg-slate-100 border border-slate-300 rounded-lg text-sm font-medium text-slate-700 hover:bg-slate-200 focus:outline-none focus:ring-2 focus:ring-slate-400 transition-colors disabled:opacity-50 disabled:cursor-not-allowed';

export default function JobsPage() {
  const router = useRouter();
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [fileCount, setFileCount] = useState<number | null>(null);
  const [xmlPreview, setXmlPreview] = useState<XmlPreview | null>(null);

  const [selectedEngine, setSelectedEngine] = useState('');
  const [selectedSumEngine, setSelectedSumEngine] = useState('');
  const [autoMessageMode, setAutoMessageMode] = useState(DEFAULT_AUTO_MESSAGE_MODE);
  const [speakerAssignment, setSpeakerAssignment] = useState(DEFAULT_SPEAKER_ASSIGNMENT);

  // Text fields are uncontrolled: the form is reset/prefilled wholesale.
  const formRef = useRef<HTMLFormElement>(null);
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
      setJobs(await api.jobs.list());
      setLoadError('');
    } catch (e) {
      setLoadError(`Failed to load jobs: ${errorMessage(e)}`);
    }
    setLoading(false);
  };

  const loadConfig = async () => {
    try {
      const data = await api.config();
      setConfig(data);
      setSelectedEngine(prev => prev || data.default_transcription_engine);
      setSelectedSumEngine(prev => prev || data.default_summarization_engine);
    } catch { /* the warnings block explains a missing backend */ }
  };

  useEffect(() => {
    loadJobs();
    loadConfig();
    const interval = setInterval(loadJobs, 5000);
    return () => clearInterval(interval);
  }, []);

  const setPaths = (value: string) => {
    if (pathsRef.current) pathsRef.current.value = value;
    setFileCount(countAudioPaths(value) || null);
  };

  const resetForm = () => {
    formRef.current?.reset();
    setFileCount(null);
    setXmlPreview(null);
    setSelectedEngine(config?.default_transcription_engine || 'assemblyai');
    setSelectedSumEngine(config?.default_summarization_engine || 'gemini');
    setAutoMessageMode(DEFAULT_AUTO_MESSAGE_MODE);
    setSpeakerAssignment(DEFAULT_SPEAKER_ASSIGNMENT);
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSubmitting(true);

    const parsedPaths = splitPaths(pathsRef.current?.value || '');
    const singleFolder = parsedPaths.length === 1 && !isAudioPath(parsedPaths[0]);
    const body: CreateJobBody = {
      case_name: caseNameRef.current?.value.trim() || '',
      defendant_name: defendantNameRef.current?.value.trim() || '',
      input_folder: singleFolder ? parsedPaths[0] : '',
      file_paths: singleFolder || parsedPaths.length === 0 ? undefined : parsedPaths,
      xml_metadata_path: xmlRef.current?.value.trim() || undefined,
      summary_prompt: promptRef.current?.value.trim() || '',
      skip_summary: skipSummaryRef.current?.checked || false,
      transcription_engine: selectedEngine || undefined,
      summarization_engine: selectedSumEngine || undefined,
      auto_message_mode: autoMessageMode,
      speaker_assignment: speakerAssignment,
    };

    if (!body.input_folder && !body.file_paths?.length) {
      setError('Input path(s) are required.');
      setSubmitting(false);
      return;
    }

    try {
      const created = await api.jobs.create(body);
      resetForm();
      try {
        await api.jobs.action(created.id, 'start');
        setSubmitting(false);
        router.push(`/jobs/${created.id}`);
        return;
      } catch (startErr) {
        setError(errorMessage(startErr) || 'Job was created but failed to start.');
      }
      await loadJobs();
    } catch (e) {
      setError(errorMessage(e) || 'Failed to create job');
    }
    setSubmitting(false);
  };

  const handleDelete = async (e: React.MouseEvent, jobId: string) => {
    e.preventDefault();
    e.stopPropagation();
    if (!confirm('Delete this job and all its files?')) return;
    try {
      await api.jobs.delete(jobId);
      await loadJobs();
    } catch { /* list refresh will show the current state */ }
  };

  const handleClearHistory = async () => {
    if (!confirm('Clear all completed and errored jobs?')) return;
    try {
      await api.jobs.clearCompleted();
      await loadJobs();
    } catch { /* ditto */ }
  };

  const handleRerun = async (e: React.MouseEvent, jobId: string) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      const s = await api.jobs.settings(jobId);
      if (caseNameRef.current) caseNameRef.current.value = s.case_name || '';
      if (defendantNameRef.current) defendantNameRef.current.value = s.defendant_name || '';
      setPaths(s.file_paths?.length ? s.file_paths.join(',\n') : s.input_folder || '');
      if (xmlRef.current) xmlRef.current.value = s.xml_metadata_path || '';
      if (promptRef.current) promptRef.current.value = s.summary_prompt || '';
      if (skipSummaryRef.current) skipSummaryRef.current.checked = s.skip_summary || false;
      if (s.transcription_engine) setSelectedEngine(s.transcription_engine);
      if (s.summarization_engine) setSelectedSumEngine(s.summarization_engine);
      setAutoMessageMode(s.auto_message_mode || DEFAULT_AUTO_MESSAGE_MODE);
      setSpeakerAssignment(s.speaker_assignment || DEFAULT_SPEAKER_ASSIGNMENT);
      formRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch { /* settings unavailable: leave the form as is */ }
  };

  /** Run an upload/scan helper with the shared busy flag and error surface. */
  const withUploading = async (fn: () => Promise<void>) => {
    setUploading(true);
    setError('');
    try {
      await fn();
    } catch (err) {
      setError(errorMessage(err));
    }
    setUploading(false);
  };

  const handleAudioUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    await withUploading(async () => {
      const { paths } = await api.uploadAudio(files);
      const existing = pathsRef.current?.value.trim() || '';
      setPaths(existing ? `${existing},\n${paths.join(',\n')}` : paths.join(',\n'));
    });
    if (audioInputRef.current) audioInputRef.current.value = '';
  };

  // Surface what the ICM report contains and prefill the editable job
  // metadata (defendant name) so the operator can tweak it before the job
  // starts; the form fields remain the source of truth.
  const applyXmlPreview = (preview: XmlPreview | null | undefined) => {
    setXmlPreview(preview ?? null);
    if (!preview?.parsed) return;
    const names = preview.inmate_names || [];
    if (names.length === 1 && defendantNameRef.current && !defendantNameRef.current.value.trim()) {
      defendantNameRef.current.value = titleCaseName(names[0]);
    }
  };

  const handleXmlUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    await withUploading(async () => {
      const data = await api.uploadXml(files[0]);
      if (xmlRef.current) xmlRef.current.value = data.path;
      applyXmlPreview(data.preview);
    });
    if (xmlInputRef.current) xmlInputRef.current.value = '';
  };

  // Pasted (not uploaded) XML paths get previewed when the field loses focus.
  const handleXmlPathBlur = async () => {
    const path = xmlRef.current?.value.trim();
    if (!path) { setXmlPreview(null); return; }
    try {
      applyXmlPreview((await api.previewXml(path)).preview);
    } catch { /* an unreadable path just shows no preview */ }
  };

  const handleScanFolder = async () => {
    const folder = pathsRef.current?.value.trim();
    if (!folder) { setError('Paste a folder path first, then click Scan.'); return; }
    await withUploading(async () => {
      const { paths } = await api.scanFolder(folder);
      if (paths.length) setPaths(paths.join(',\n'));
      else setError('No audio files found in that folder.');
    });
  };

  const activeTranscriptionEngine = selectedEngine || config?.default_transcription_engine || 'assemblyai';
  const activeSummarizationEngine = selectedSumEngine || config?.default_summarization_engine || 'gemini';
  const warnings: string[] = [];
  if (config) {
    if (!config.ffmpeg_found) warnings.push('ffmpeg not found. Set FFMPEG_PATH in .env or install ffmpeg to PATH.');
    // An installed engine that still needs a key or binary: say what, from the registry.
    const notReady = (engines: EngineInfo[], id: string) => engines.find(e => e.id === id && e.installed && !e.ready);
    const t = notReady(config.transcription_engines, activeTranscriptionEngine);
    if (t) warnings.push(`${t.label}: ${t.requirement}. Jobs using it will fail.`);
    const s = notReady(config.summarization_engines, activeSummarizationEngine);
    if (s) warnings.push(`${s.label}: ${s.requirement}. Jobs using it will fail unless Skip Summary is checked.`);
  }

  const engineOptions = (engines?: EngineInfo[]) =>
    (engines || []).filter(e => e.installed).map(e => ({ value: e.id, label: e.label }));

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-slate-900">Jail Call Service</h1>
        <p className="mt-1 text-sm text-slate-500">
          Batch transcription and packaging for G.729 jail call recordings.
        </p>
      </div>

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
        <form ref={formRef} onSubmit={handleCreate} className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1">Case Name <span className="text-slate-400 font-normal">(optional)</span></label>
              <input ref={caseNameRef} type="text" placeholder="People v. Smith 2024" className={inputClass} />
            </div>
            <div>
              <label className="block text-sm font-medium text-slate-700 mb-1">Defendant Name <span className="text-slate-400 font-normal">(used for the inmate speaker label)</span></label>
              <input ref={defendantNameRef} type="text" placeholder="John Smith" className={inputClass} />
            </div>
            <ToggleGroup
              label="Speaker Assignment"
              options={SPEAKER_OPTIONS}
              value={speakerAssignment}
              onChange={setSpeakerAssignment}
              hint="Default is left speaker = inmate/defendant. Switch this if the recording sides are reversed for the whole job."
            />
            <ToggleGroup
              label="Transcription Engine"
              options={engineOptions(config?.transcription_engines)}
              value={activeTranscriptionEngine}
              onChange={setSelectedEngine}
            />
            <ToggleGroup
              label="Summarization Engine"
              options={engineOptions(config?.summarization_engines)}
              value={activeSummarizationEngine}
              onChange={setSelectedSumEngine}
            />
            <ToggleGroup
              label="Automated Messages"
              options={AUTO_MESSAGE_OPTIONS}
              value={autoMessageMode}
              onChange={setAutoMessageMode}
              hint={AUTO_MESSAGE_HINTS[autoMessageMode]}
            />
            <div className="col-span-2">
              <label className="block text-sm font-medium text-slate-700 mb-1">Audio Files Path(s)</label>
              <div className="flex gap-2 items-start">
                <div className="flex-1 flex flex-col gap-1">
                  <textarea
                    ref={pathsRef}
                    rows={3}
                    placeholder="Upload audio files or paste an absolute folder path (e.g. /Users/you/calls) or specific file paths separated by commas/newlines"
                    className={`${inputClass} font-mono resize-none`}
                    onChange={e => setFileCount(countAudioPaths(e.target.value) || null)}
                  />
                  {fileCount !== null && (
                    <p className="text-xs text-slate-500">{fileCount} audio file{fileCount !== 1 ? 's' : ''} selected</p>
                  )}
                </div>
                <input ref={audioInputRef} type="file" multiple accept=".wav,.mp3,.m4a" onChange={handleAudioUpload} className="hidden" />
                <div className="flex flex-col gap-1 shrink-0">
                  <button type="button" onClick={() => audioInputRef.current?.click()} disabled={uploading} className={secondaryButtonClass}>
                    {uploading ? 'Working...' : 'Upload Files...'}
                  </button>
                  <button type="button" onClick={handleScanFolder} disabled={uploading} className={secondaryButtonClass}>
                    Scan Folder
                  </button>
                </div>
              </div>
            </div>
            <div className="col-span-2">
              <label className="block text-sm font-medium text-slate-700 mb-1">ICM Metadata XML Path <span className="text-slate-400 font-normal">(optional)</span></label>
              <div className="flex gap-2 items-center">
                <input
                  ref={xmlRef}
                  type="text"
                  placeholder="Upload XML or paste path (e.g. /Users/you/calls/ICM_report.xml)"
                  onBlur={handleXmlPathBlur}
                  className={`flex-1 ${inputClass} font-mono`}
                />
                <input ref={xmlInputRef} type="file" accept=".xml" onChange={handleXmlUpload} className="hidden" />
                <button type="button" onClick={() => xmlInputRef.current?.click()} disabled={uploading} className={`${secondaryButtonClass} shrink-0 whitespace-nowrap`}>
                  {uploading ? 'Uploading...' : 'Upload XML...'}
                </button>
              </div>
              {xmlPreview && (
                xmlPreview.parsed ? (
                  <div className="mt-2 text-xs text-slate-600 bg-slate-50 rounded-lg px-3 py-2 border border-slate-200 space-y-0.5">
                    <p className="font-medium text-slate-700">
                      Metadata parsed: {xmlPreview.call_count} call record{xmlPreview.call_count !== 1 ? 's' : ''}
                      {xmlPreview.unique_numbers ? `, ${xmlPreview.unique_numbers} outside number${xmlPreview.unique_numbers !== 1 ? 's' : ''}` : ''}
                    </p>
                    {(xmlPreview.inmate_names?.length ?? 0) > 0 && (
                      <p>Inmate{(xmlPreview.inmate_names!.length !== 1) ? 's' : ''}: {xmlPreview.inmate_names!.map(titleCaseName).join(', ')}</p>
                    )}
                    {xmlPreview.date_range && (
                      <p>Dates: {xmlPreview.date_range.start} to {xmlPreview.date_range.end}</p>
                    )}
                    {(xmlPreview.facilities?.length ?? 0) > 0 && (
                      <p>Housing: {xmlPreview.facilities!.join(', ')}</p>
                    )}
                    <p className="text-slate-400">
                      This metadata flows onto transcript covers, the call index, and the case report.
                      Adjust the Case Name and Defendant Name fields above before starting if needed.
                    </p>
                  </div>
                ) : (
                  <div className="mt-2 text-xs text-amber-700 bg-amber-50 rounded-lg px-3 py-2 border border-amber-200">
                    Could not parse call records from this XML. The job will still run, but transcript covers
                    and reports will be missing call dates, phone numbers, and inmate names.
                  </div>
                )
              )}
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-700 mb-1">
              Case Context <span className="text-slate-400 font-normal">(optional — appended to the default prompt to guide the AI)</span>
            </label>
            <textarea
              ref={promptRef}
              rows={3}
              placeholder="E.g. Defendant is charged with first-degree murder. The alleged victim is John Smith. Focus on any references to the night of March 4th, contact with witnesses, or discussion of physical evidence."
              className={`${inputClass} resize-none`}
            />
          </div>
          <div className="flex items-center gap-2 mt-2">
            <input type="checkbox" id="skipSummary" ref={skipSummaryRef} className="rounded border-slate-300 text-slate-800 focus:ring-slate-400" />
            <label htmlFor="skipSummary" className="text-sm font-medium text-slate-700">
              Skip Summary (Generate Dummy Summary for Testing)
            </label>
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <div className="flex justify-end">
            <button
              type="submit"
              disabled={submitting || uploading}
              className="px-4 py-2 bg-slate-800 text-white text-sm font-medium rounded-lg hover:bg-slate-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {uploading ? 'Uploading...' : submitting ? 'Creating...' : 'Create & Start Job'}
            </button>
          </div>
        </form>
      </div>

      {loadError && (
        <div className="mb-4 bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 text-sm text-amber-700">
          {loadError}
        </div>
      )}

      {/* Jobs List */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold text-slate-800">Jobs</h2>
          {jobs.length > 0 && (
            <button
              onClick={handleClearHistory}
              className="px-3 py-1.5 text-xs font-medium text-red-600 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 transition-colors"
            >
              Clear History
            </button>
          )}
        </div>
        {loading ? (
          <div className="text-center py-12 text-slate-400 text-sm">Loading...</div>
        ) : jobs.length === 0 ? (
          <div className="bg-white rounded-xl border border-slate-200 p-12 text-center text-slate-400 text-sm">
            No jobs yet. Create one above.
          </div>
        ) : (
          <div className="space-y-3">
            {jobs.map(job => {
              const caseContext = extractCaseContext(job.summary_prompt);
              return (
                <Link key={job.id} href={`/jobs/${job.id}`} className="block">
                  <div className="bg-white rounded-xl border border-slate-200 shadow-sm hover:border-slate-300 hover:shadow transition-all p-4">
                    <div className="flex items-center justify-between gap-4">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-3">
                          <span className="font-semibold text-slate-900">{job.case_name || 'Jail Calls'}</span>
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
                            href={api.jobs.downloadUrl(job.id)}
                            onClick={e => e.stopPropagation()}
                            className="px-3 py-1.5 bg-green-50 text-green-700 border border-green-200 rounded-lg text-xs font-medium hover:bg-green-100 transition-colors"
                          >
                            Download
                          </a>
                        )}
                        {['done', 'error', 'paused'].includes(job.stage) && (
                          <button
                            onClick={e => handleRerun(e, job.id)}
                            className="px-3 py-1.5 bg-slate-50 text-slate-600 border border-slate-200 rounded-lg text-xs font-medium hover:bg-slate-100 transition-colors"
                            title="Re-run with same settings"
                          >
                            Re-run
                          </button>
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
                    {caseContext && (
                      <div className="mt-2 text-xs text-slate-500 bg-slate-50 rounded px-2.5 py-1.5 border border-slate-100">
                        <span className="font-medium text-slate-600">Case context:</span>{' '}
                        {caseContext.slice(0, 150)}{caseContext.length > 150 ? '...' : ''}
                      </div>
                    )}
                    {isRunning(job.stage) && (
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
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
