/**
 * Typed client for the backend API (proxied at /api by next.config.js).
 *
 * Every page talks to the server through `api.*`; the response types below
 * mirror the JSON shapes in backend/server.py.
 */

export const API = '/api';

// ────────────────────────── Types ──────────────────────────

export type JobStage =
  | 'created' | 'converting' | 'transcribing' | 'summarizing'
  | 'generating' | 'packaging' | 'done' | 'error' | 'paused';

export type JobSummary = {
  id: string;
  case_name: string;
  input_folder: string;
  stage: JobStage;
  total_calls: number;
  done_calls: number;
  error_calls: number;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  has_zip: boolean;
  error?: string | null;
  defendant_name?: string | null;
  summary_prompt?: string | null;
  speaker_assignment?: string;
};

export type CallSummary = {
  index: number;
  filename: string;
  status: string;
  duration_seconds?: number | null;
  has_transcript: boolean;
  has_summary: boolean;
  repaired: boolean;
  error?: string | null;
  inmate_name?: string | null;
  call_datetime_str?: string | null;
  outside_number_fmt?: string | null;
  facility?: string | null;
  call_outcome?: string | null;
};

export type JobDetail = JobSummary & { calls: CallSummary[] };

export type JobSettings = {
  case_name: string;
  defendant_name: string;
  input_folder: string;
  file_paths: string[];
  summary_prompt: string;
  xml_metadata_path: string;
  skip_summary: boolean;
  transcription_engine: string;
  summarization_engine: string;
  auto_message_mode: string;
  speaker_assignment: string;
};

export type CreateJobBody = {
  case_name: string;
  defendant_name: string;
  input_folder: string;
  file_paths?: string[];
  xml_metadata_path?: string;
  summary_prompt: string;
  skip_summary: boolean;
  transcription_engine?: string;
  summarization_engine?: string;
  auto_message_mode: string;
  speaker_assignment: string;
};

export type AppConfig = {
  assemblyai_configured: boolean;
  gemini_configured: boolean;
  ffmpeg_found: boolean;
  ffmpeg_path: string;
  default_summary_prompt: string;
  gemini_model: string;
  default_transcription_engine: string;
  available_transcription_engines: string[];
  default_summarization_engine: string;
  available_summarization_engines: string[];
};

export type XmlPreview = {
  parsed: boolean;
  call_count: number;
  inmate_names?: string[];
  facilities?: string[];
  date_range?: { start: string; end: string } | null;
  unique_numbers?: number;
};

export type Turn = {
  speaker: string;
  text: string;
  timestamp?: string | null;
  is_continuation: boolean;
};

export type CallTranscript = {
  index: number;
  filename: string;
  duration_seconds?: number | null;
  turns: Turn[];
};

export type JobAction = 'start' | 'pause' | 'resume' | 'retry-errors' | 'package';

// ────────────────────────── Helpers ──────────────────────────

/** Stages in which no pipeline worker is running. */
export const IDLE_STAGES: readonly string[] = ['created', 'done', 'error', 'paused'];

export function isRunning(stage: string): boolean {
  return !IDLE_STAGES.includes(stage);
}

/** Must match CASE_CONTEXT_MARKER in backend/job_settings.py. */
export const CASE_CONTEXT_MARKER = 'CASE CONTEXT:\n';

export function extractCaseContext(summaryPrompt?: string | null): string | null {
  if (!summaryPrompt || !summaryPrompt.includes(CASE_CONTEXT_MARKER)) return null;
  return summaryPrompt.split(CASE_CONTEXT_MARKER).pop() ?? null;
}

export function formatDuration(seconds?: number | null, empty = '-'): string {
  if (!seconds) return empty;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export function formatElapsed(startIso?: string | null, endIso?: string | null, now = Date.now()): string {
  if (!startIso) return '';
  const start = new Date(startIso).getTime();
  if (isNaN(start)) return '';
  const end = endIso ? new Date(endIso).getTime() : now;
  const secs = Math.max(0, Math.floor((end - start) / 1000));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// ────────────────────────── Transport ──────────────────────────

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init ?? {};
  const res = await fetch(`${API}${path}`, {
    ...rest,
    headers: json !== undefined ? { 'Content-Type': 'application/json', ...(rest.headers ?? {}) } : rest.headers,
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  const text = await res.text();
  let data: unknown = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    const detail = (data as { detail?: string } | null)?.detail;
    throw new ApiError(res.status, detail || res.statusText || `HTTP ${res.status}`);
  }
  return data as T;
}

function form(files: File[] | FileList, field: string): FormData {
  const fd = new FormData();
  Array.from(files).forEach(f => fd.append(field, f));
  return fd;
}

export const api = {
  config: () => request<AppConfig>('/config'),
  scanFolder: (path: string) => request<{ paths: string[] }>('/scan/folder', { method: 'POST', json: { path } }),
  uploadAudio: (files: FileList) => request<{ paths: string[] }>('/upload/audio', { method: 'POST', body: form(files, 'files') }),
  uploadXml: (file: File) => request<{ path: string; preview: XmlPreview }>('/upload/xml', { method: 'POST', body: form([file], 'file') }),
  previewXml: (path: string) => request<{ path: string; preview: XmlPreview }>('/xml/preview', { method: 'POST', json: { path } }),

  jobs: {
    list: () => request<JobSummary[]>('/jobs'),
    get: (id: string) => request<JobDetail>(`/jobs/${id}`),
    settings: (id: string) => request<JobSettings>(`/jobs/${id}/settings`),
    create: (body: CreateJobBody) => request<JobSummary>('/jobs', { method: 'POST', json: body }),
    action: (id: string, action: JobAction) => request<{ status: string }>(`/jobs/${id}/${action}`, { method: 'POST' }),
    delete: (id: string) => request<{ deleted: string }>(`/jobs/${id}`, { method: 'DELETE' }),
    clearCompleted: () => request<{ deleted: number }>('/jobs', { method: 'DELETE' }),
    transcript: (id: string, index: number) => request<CallTranscript>(`/jobs/${id}/calls/${index}/transcript`),
    summary: (id: string, index: number) => request<{ summary: string }>(`/jobs/${id}/calls/${index}/summary`),
    updateSummary: (id: string, index: number, summary: string) =>
      request<{ summary: string }>(`/jobs/${id}/calls/${index}/summary`, { method: 'PUT', json: { summary } }),
    downloadUrl: (id: string) => `${API}/jobs/${id}/download`,
    eventsUrl: (id: string) => `${API}/jobs/${id}/events`,
  },
};
