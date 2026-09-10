'use client';

import { useState, useEffect } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';

import {
  api, ApiError, errorMessage, formatDuration,
  type CallTranscript, type JobDetail,
} from '@/lib/api';

export default function ReviewPage() {
  const params = useParams();
  const router = useRouter();
  const jobId = params.id as string;

  const [job, setJob] = useState<JobDetail | null>(null);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [transcript, setTranscript] = useState<CallTranscript | null>(null);
  const [summary, setSummary] = useState('');
  const [editedSummary, setEditedSummary] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState('');
  const [loadingTranscript, setLoadingTranscript] = useState(false);
  const [approvedAll, setApprovedAll] = useState(false);
  const [packaging, setPackaging] = useState(false);
  const [fetchError, setFetchError] = useState('');

  const loadCall = async (index: number) => {
    setSelectedIndex(index);
    setLoadingTranscript(true);
    setSaveMsg('');
    try {
      const [transcriptResult, summaryResult] = await Promise.allSettled([
        api.jobs.transcript(jobId, index),
        api.jobs.summary(jobId, index),
      ]);
      setTranscript(transcriptResult.status === 'fulfilled' ? transcriptResult.value : null);
      if (summaryResult.status === 'fulfilled') {
        setSummary(summaryResult.value.summary || '');
        setEditedSummary(summaryResult.value.summary || '');
      }
      setFetchError('');
    } catch (e) {
      setFetchError(`Failed to load call: ${errorMessage(e)}`);
    }
    setLoadingTranscript(false);
  };

  useEffect(() => {
    api.jobs.get(jobId)
      .then(data => {
        setJob(data);
        setFetchError('');
        const firstDone = data.calls.find(c => c.has_transcript);
        if (firstDone) loadCall(firstDone.index);
      })
      .catch(e => {
        if (e instanceof ApiError && e.status === 404) router.push('/');
        else setFetchError(`Failed to load job: ${errorMessage(e)}`);
      });
  }, [jobId]);

  const saveSummary = async () => {
    if (selectedIndex === null) return;
    setSaving(true);
    setSaveMsg('');
    setFetchError('');
    try {
      await api.jobs.updateSummary(jobId, selectedIndex, editedSummary);
      setSummary(editedSummary);
      setSaveMsg('Saved!');
      setTimeout(() => setSaveMsg(''), 2000);
    } catch (e) {
      setFetchError(`Failed to save summary: ${errorMessage(e)}`);
    }
    setSaving(false);
  };

  const handleApproveAndPackage = async () => {
    setPackaging(true);
    setFetchError('');
    try {
      await api.jobs.action(jobId, 'package');
      setApprovedAll(true);
    } catch (e) {
      setFetchError(`Failed to package: ${errorMessage(e)}`);
    }
    setPackaging(false);
  };

  // Warn on unsaved changes
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (editedSummary !== summary) e.preventDefault();
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [editedSummary, summary]);

  if (!job) {
    return <div className="flex items-center justify-center h-64 text-slate-400">Loading…</div>;
  }

  const doneCalls = job.calls.filter(c => c.has_transcript);

  return (
    <div className="flex h-screen overflow-hidden bg-slate-100">
      {/* Left: call list */}
      <div className="w-72 bg-white border-r border-slate-200 flex flex-col flex-shrink-0">
        <div className="px-4 py-3 border-b border-slate-100 flex items-center justify-between">
          <div>
            <Link href={`/jobs/${jobId}`} className="text-xs text-slate-400 hover:text-slate-700">← Back</Link>
            <h2 className="text-sm font-semibold text-slate-800 mt-0.5">{job.case_name}</h2>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto">
          {doneCalls.length === 0 ? (
            <div className="px-4 py-8 text-xs text-slate-400 text-center">No completed calls yet.</div>
          ) : (
            doneCalls.map(call => (
              <button
                key={call.index}
                onClick={() => loadCall(call.index)}
                className={`w-full text-left px-4 py-3 border-b border-slate-50 hover:bg-slate-50 transition-colors ${
                  selectedIndex === call.index ? 'bg-slate-100 border-l-2 border-l-slate-700' : ''
                }`}
              >
                <div className="text-xs font-medium text-slate-700 truncate">{call.filename}</div>
                <div className="text-xs text-slate-400 mt-0.5">{formatDuration(call.duration_seconds, '')}</div>
              </button>
            ))
          )}
        </div>
        <div className="p-4 border-t border-slate-100">
          {approvedAll ? (
            <a
              href={api.jobs.downloadUrl(jobId)}
              className="w-full block text-center px-4 py-2 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 transition-colors"
            >
              Download Zip
            </a>
          ) : (
            <button
              onClick={handleApproveAndPackage}
              disabled={packaging}
              className="w-full px-4 py-2 bg-slate-800 text-white text-sm font-medium rounded-lg hover:bg-slate-700 disabled:opacity-50 transition-colors"
            >
              {packaging ? 'Packaging…' : 'Approve All & Package'}
            </button>
          )}
        </div>
      </div>

      {/* Right: transcript + summary */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {selectedIndex === null ? (
          <div className="flex items-center justify-center h-full text-slate-400 text-sm">
            Select a call to review.
          </div>
        ) : loadingTranscript ? (
          <div className="flex items-center justify-center h-full text-slate-400 text-sm">Loading…</div>
        ) : (
          <>
            {fetchError && (
              <div className="bg-amber-50 border-b border-amber-200 px-6 py-2 text-sm text-amber-700 flex items-center justify-between">
                <span>{fetchError}</span>
                <button onClick={() => setFetchError('')} className="text-amber-500 hover:text-amber-700 ml-2">&times;</button>
              </div>
            )}

            <div className="bg-white border-b border-slate-200 px-6 py-3 flex items-center justify-between">
              <div>
                <h2 className="text-sm font-semibold text-slate-900">{transcript?.filename}</h2>
                {transcript?.duration_seconds && (
                  <span className="text-xs text-slate-400">{formatDuration(transcript.duration_seconds, '')}</span>
                )}
              </div>
            </div>

            <div className="flex-1 flex overflow-hidden">
              {/* Transcript */}
              <div className="flex-1 overflow-y-auto p-6 bg-slate-50">
                <div className="max-w-2xl mx-auto bg-white rounded-xl border border-slate-200 p-6 shadow-sm">
                  <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-4">Transcript</h3>
                  {transcript?.turns.length === 0 ? (
                    <p className="text-sm text-slate-400">No transcript content.</p>
                  ) : (
                    <div className="space-y-1">
                      {transcript?.turns.map((turn, i) => (
                        <div key={i} className="flex gap-3 text-sm">
                          {turn.is_continuation ? (
                            <span className="min-w-[140px] shrink-0" />
                          ) : (
                            <span className="font-semibold text-slate-600 min-w-[140px] text-right shrink-0">
                              {turn.timestamp && <span className="font-mono text-slate-400 text-xs mr-1">{turn.timestamp}</span>}
                              {turn.speaker}:
                            </span>
                          )}
                          <p className="text-slate-800 leading-relaxed">{turn.text}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              {/* Summary editor */}
              <div className="w-80 bg-white border-l border-slate-200 flex flex-col flex-shrink-0">
                <div className="px-4 py-3 border-b border-slate-100 flex items-center justify-between">
                  <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-wider">AI Summary</h3>
                  <div className="flex items-center gap-2">
                    {saveMsg && <span className="text-xs text-green-600">{saveMsg}</span>}
                    <button
                      onClick={saveSummary}
                      disabled={saving || editedSummary === summary}
                      className="px-2.5 py-1 bg-slate-800 text-white text-xs font-medium rounded hover:bg-slate-700 disabled:opacity-40 transition-colors"
                    >
                      {saving ? 'Saving…' : 'Save'}
                    </button>
                  </div>
                </div>
                <textarea
                  value={editedSummary}
                  onChange={e => setEditedSummary(e.target.value)}
                  className="flex-1 p-4 text-sm text-slate-700 resize-none outline-none leading-relaxed"
                  placeholder="Summary will appear here after processing…"
                />
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
