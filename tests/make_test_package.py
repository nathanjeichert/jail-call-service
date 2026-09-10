"""Build a complete synthetic delivery package for manual review.

This is the canonical end-to-end check before shipping big changes:

    python tests/make_test_package.py            # → test-output/<case>/
    python tests/make_test_package.py --zip      # also build the delivery zip
    python tests/make_test_package.py --calls 25 # bigger batch

It fabricates a batch of fake calls (default 10) — scripted transcripts
with word-level timestamps, structured dummy summaries across every
relevance tier (plus the unstructured-fallback and skip-summary paths),
ICM-style metadata, and real tone MP3s generated with ffmpeg — then runs
them through the REAL delivery code:

  * per-call transcript PDFs via ``delivery.transcript_pdf.create_pdf``
    (both ``transcripts/`` and ``transcripts-no-summary/`` variants)
  * ``search.html`` / ``viewer.html`` / ``guide.pdf`` / ``case-report.pdf``
    via the pipeline's own ``_stage_generate_delivery_assets``
  * case-report synthesis through a stub engine returning canned
    FINDING/IDENTITY blocks, so the real parsing path runs with no API

Everything is exercised EXCEPT live transcription and summarization.
No network, no API keys, no job database. Open the printed paths in a
browser / PDF reader and review.
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.delivery.call_view import build_call_view
from backend.delivery.transcript_pdf import create_pdf
from backend.formatting import format_duration
from backend.models import CallResult, Job, TranscriptTurn, WordTimestamp, call_stem
from backend.pipeline import _stage_generate_delivery_assets
from backend.summaries import normalize_structured_summary, parse_summary_sections, render_summary_text
from backend.summarization.base import SummarizationEngine, TokenUsage
from backend.summarization.schemas import SummaryNote, SummaryResponse
from backend.summarization.text_protocol import parse_case_report_text
from backend.transcript_layout import compute_line_entries

CASE_NAME = "State v. Marcus Reeves"
DEFENDANT = "Marcus Reeves"
FACILITY = "Riverton County Detention Center"

PARTIES = [
    ("9095550144", "(909) 555-0144", "Sister; addressed as 'Tanya' across multiple calls."),
    ("9095550226", "(909) 555-0226", "Mother; defendant uses 'Mama' throughout."),
    ("7605550317", "(760) 555-0317", "Friend; handled the impound retrieval."),
    ("9515550419", "(951) 555-0419", "First name only ('Keisha'); relationship unestablished."),
]

AUTOMATED_OPENER = (
    "This call is from an inmate at a county detention facility. "
    "This call is recorded and subject to monitoring."
)

# Three scripted conversations, rotated across the batch. Quotes and
# apostrophes are deliberate — they exercise HTML/JSON escaping.
SCRIPTS = [
    [
        ("INMATE", "Hey, it's me. Did you get the paperwork from Castillo's office yet or are they still dragging their feet on it?"),
        ("OUTSIDE PARTY", "It came yesterday. Your dad signed where they flagged it and I'm dropping it off Monday morning before work."),
        ("INMATE", "Okay good. Listen, when you talk to Darnell, tell him not to say anything about that night until the lawyer calls him first. I mean it."),
        ("OUTSIDE PARTY", "He already knows. He said the investigator from the DA's office came by his apartment Tuesday and he didn't open the door."),
        ("INMATE", "They came to the house? Man. That's exactly what Castillo said would happen. Don't let him talk to anybody, period."),
        ("OUTSIDE PARTY", "I know, I told him. How are you holding up in there? Did they move you off the medical unit yet?"),
        ("INMATE", "Yeah, Wednesday. I'm back on B pod. Commissary went through so I'm alright for now. Put whatever you can on the books next week."),
        ("OUTSIDE PARTY", "I will. Tanya said she can do forty and Mama's sending sixty after the first. We got you, okay? Don't stress about that part."),
        ("INMATE", "Alright. Castillo said the preliminary got moved to the twenty-second, so tell everybody that, not the fourteenth anymore."),
        ("OUTSIDE PARTY", "The twenty-second. Got it. I already asked off work for it. Love you. Be safe in there, okay?"),
    ],
    [
        ("INMATE", "You get the car out of impound yet or do they still want all that money up front?"),
        ("OUTSIDE PARTY", "Three hundred eighty to release it. I'm not paying that until your hearing — it just sits there racking up fees either way."),
        ("INMATE", "Whatever you think. Was everything still in it? The cops went through the trunk, right?"),
        ("OUTSIDE PARTY", "The guy at the lot said the police took some stuff out of the trunk before it got towed. He didn't have a list."),
        ("INMATE", "Of course they did. Tell Castillo that — he said anything they took has to show up in the inventory."),
        ("OUTSIDE PARTY", "I'll call the office tomorrow. Hector says he can cover your shifts through the end of the month, by the way."),
        ("INMATE", "Tell him I said thanks. And tell him they might call him — I was closing the shop that night, he knows that. I was there till at least nine-forty."),
        ("OUTSIDE PARTY", "He already told them that once. He's not worried about saying it again."),
    ],
    [
        ("INMATE", "Hey. You alright? You sounded stressed in your message."),
        ("OUTSIDE PARTY", "I'm fine. Your daughter's birthday is Sunday and she keeps asking when you're coming home."),
        ("INMATE", "Put her on next time, alright? I don't want her hearing all this through other people."),
        ("OUTSIDE PARTY", "I will. Church is doing a thing for her after service. Everybody keeps asking about you."),
        ("INMATE", "Tell them I'm good. I've been keeping my head down, going to the law library, all of that."),
        ("OUTSIDE PARTY", "Good. Don't get caught up in nothing in there. You heard me?"),
        ("INMATE", "I heard you. The money came through Tuesday, by the way. I'm set for a couple weeks."),
        ("OUTSIDE PARTY", "Okay. Call me Sunday so she can talk to you. Don't forget."),
    ],
]

CUE_REASONS = [
    "Defendant instructs the outside party to keep a named witness from speaking to investigators before counsel makes contact.",
    "Outside party reports items were removed from the impounded vehicle before towing; no inventory was provided.",
    "Statement about the defendant's whereabouts and timeline on the night of the incident.",
    "Preliminary hearing date moved from the fourteenth to the twenty-second per defense counsel.",
]

BRIEFS = [
    "The defendant coordinates legal paperwork and instructs the outside party to keep a witness from speaking to investigators before counsel calls; the hearing was rescheduled to the twenty-second.",
    "Discussion of retrieving the defendant's car from impound; the outside party reports police removed items from the trunk before towing.",
    "Routine family conversation about the defendant's daughter, church, and commissary money.",
]

CANNED_SYNTHESIS = """
FINDING_START
CALL_ID: 0
HEADLINE: Pre-Hearing Contact With a Listed Witness
TIMESTAMP: [{ts0}]
DETAIL: Across multiple calls, the defendant asked the outside party to keep a man named Darnell from speaking to investigators until defense counsel made contact. The outside party reported a DA investigator had already visited the witness's apartment.
FINDING_END

FINDING_START
CALL_ID: 1
HEADLINE: Items Removed From the Impounded Vehicle
TIMESTAMP: [{ts1}]
DETAIL: An outside party retrieving the defendant's car reported that police removed items from the trunk before towing and that no inventory list was available at the lot.
FINDING_END

FINDING_START
CALL_ID: 2
HEADLINE: Employer Statement on the Night of the Incident
TIMESTAMP: NONE
DETAIL: The defendant stated his employer could confirm he was closing the shop until at least 9:40 PM on the night in question and expected investigators to contact the employer.
FINDING_END

IDENTITY_START
NUMBER: (909) 555-0144
INFERENCE: Tanya, sister
CONFIDENCE: HIGH
IDENTITY_END

IDENTITY_START
NUMBER: (909) 555-0226
INFERENCE: Mother of defendant
CONFIDENCE: HIGH
IDENTITY_END

IDENTITY_START
NUMBER: (760) 555-0317
INFERENCE: Friend
CONFIDENCE: MEDIUM
IDENTITY_END

IDENTITY_START
NUMBER: (951) 555-0419
INFERENCE: Unknown
CONFIDENCE: LOW
IDENTITY_END
"""


class StubSynthesisEngine(SummarizationEngine):
    """Stands in for Gemini/Gemma during the case-report synthesis call.

    Feeds canned FINDING/IDENTITY blocks through the real text-protocol
    parser, so the parsing path runs with no API.
    """

    name = "stub"

    def __init__(self, response_text: str):
        self._text = response_text

    async def summarize_call(self, turns, prompt, metadata=None, *, detect_system_audio=False):
        raise NotImplementedError("the test package fabricates summaries directly")

    async def synthesize_case_report(self, inputs):
        return parse_case_report_text(self._text), TokenUsage()


def _build_turns(script, duration: float) -> list:
    """Turn a script into TranscriptTurns with synthetic word timestamps."""
    turns = [
        TranscriptTurn(speaker="AUTOMATED MESSAGE", timestamp="[00:00]", text=AUTOMATED_OPENER)
    ]
    slot = duration / (len(script) + 1)
    for i, (speaker, text) in enumerate(script):
        start = (i + 1) * slot
        mins, secs = divmod(int(start), 60)
        words = text.split()
        word_dur_ms = min(380.0, (slot * 0.7 * 1000.0) / max(len(words), 1))
        word_objs = [
            WordTimestamp(
                text=w,
                start=round(start * 1000.0 + j * word_dur_ms, 1),
                end=round(start * 1000.0 + (j + 1) * word_dur_ms, 1),
            )
            for j, w in enumerate(words)
        ]
        turns.append(
            TranscriptTurn(
                speaker=speaker,
                timestamp=f"[{mins:02d}:{secs:02d}]",
                text=text,
                words=word_objs,
            )
        )
    return turns


def _build_summary(call_index: int, relevance: str, cue_count: int,
                   party_idx: int, script_idx: int, turns, duration: float) -> str:
    line_entries = compute_line_entries(turns, duration)

    def line_ref(turn_idx: int) -> str:
        rows = [e for e in line_entries if e["turn_index"] == turn_idx]
        return f"{rows[0]['page']}:{rows[0]['line']}-{rows[-1]['page']}:{rows[-1]['line']}"

    # Cite speech turns (turn 0 is the automated opener).
    notes = [
        SummaryNote(
            line_ref=line_ref(1 + (call_index + n * 2) % (len(turns) - 2)),
            reason=CUE_REASONS[(call_index + n) % len(CUE_REASONS)],
            importance_rank=n + 1,
        )
        for n in range(cue_count)
    ]
    summary = SummaryResponse(
        relevance=relevance,
        notes=notes,
        identity_of_outside_party=PARTIES[party_idx][2],
        brief_summary=BRIEFS[script_idx],
    )
    normalized = normalize_structured_summary(summary, line_entries)
    return render_summary_text(normalized, line_entries)


def _write_tone_mp3(path: Path, duration: float, freq: int) -> None:
    """Generate a quiet tone MP3 of the call's exact duration via ffmpeg."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration:.1f}",
            "-filter:a", "volume=0.12",
            "-ac", "1", "-b:a", "32k",
            str(path),
        ],
        check=True,
    )


def build_calls(count: int, audio_dir: Path, with_audio: bool) -> list:
    """Fabricate the synthetic batch, covering every summary path."""
    start_date = date(2026, 3, 3)
    span_days = 88
    calls = []

    # Relevance plan: HIGH and MEDIUM up front, then LOW; the last two calls
    # exercise the unstructured-summary fallback and the skip-summary stub.
    plan = []
    for i in range(count):
        frac = i / max(count - 1, 1)
        if i == count - 1:
            plan.append("dummy")
        elif i == count - 2:
            plan.append("raw")
        elif frac < 0.3:
            plan.append("HIGH")
        elif frac < 0.6:
            plan.append("MEDIUM")
        else:
            plan.append("LOW")

    for i, kind in enumerate(plan):
        day = start_date + timedelta(days=round(i * span_days / max(count - 1, 1)))
        hour = 9 + (i * 3) % 11
        duration = float(240 + (i * 97) % 420)  # 4–11 minutes
        raw_number, fmt_number, _ = PARTIES[i % len(PARTIES)]
        script_idx = i % len(SCRIPTS)
        turns = _build_turns(SCRIPTS[script_idx], duration)
        filename = f"2026{day.month:02d}{day.day:02d}_{hour:02d}3302_{raw_number}.wav"
        stem = call_stem(i, filename)

        if kind == "dummy":
            summary = (
                f"**DUMMY SUMMARY FOR {filename}**\n\n"
                f"- The user requested to skip AI processing for this test job.\n"
                f"- Call duration: {duration} sec."
            )
        elif kind == "raw":
            summary = "Summary unavailable for this call."
        else:
            cue_count = {"HIGH": 3, "MEDIUM": 2, "LOW": 0}[kind]
            summary = _build_summary(i, kind, cue_count, i % len(PARTIES), script_idx, turns, duration)

        mp3_path = audio_dir / f"{stem}.mp3"
        if with_audio:
            _write_tone_mp3(mp3_path, duration, freq=240 + i * 40)

        calls.append(CallResult(
            index=i,
            filename=filename,
            original_path=f"/test-input/{filename}",
            mp3_path=str(mp3_path),
            duration_seconds=duration,
            turns=turns,
            summary=summary,
            status="done",
            inmate_name=DEFENDANT,
            outside_number=raw_number,
            outside_number_fmt=fmt_number,
            call_date=day.strftime("%Y-%m-%d"),
            call_time=f"{hour:02d}:33",
            call_datetime_str=f"{day.strftime('%Y-%m-%d')} {hour:02d}:33",
            facility=FACILITY,
            call_outcome="Completed",
        ))
    return calls


def write_call_pdfs(calls, transcripts_dir: Path, no_summary_dir: Path) -> None:
    """Per-call PDFs, mirroring pipeline._generate_pdf_one's title_data."""
    for call in calls:
        stem = call_stem(call.index, call.filename)
        title_data = {
            "CASE_NAME": CASE_NAME,
            "FILE_NAME": call.filename,
            "AUDIO_FILENAME": os.path.basename(call.mp3_path),
            "FILE_DURATION": format_duration(call.duration_seconds),
            "INMATE_NAME": call.inmate_name or "",
            "CALL_DATETIME": call.call_datetime_str or "",
            "FACILITY": call.facility or "",
            "OUTSIDE_NUMBER_FMT": call.outside_number_fmt or "",
            "CALL_OUTCOME": call.call_outcome or "",
            "NOTES": call.notes or "",
        }
        view = build_call_view(call)
        (transcripts_dir / f"{stem}.pdf").write_bytes(create_pdf(view, title_data))
        (no_summary_dir / f"{stem}.pdf").write_bytes(create_pdf(view, title_data, include_summary=False))
        print(f"  transcript {call.index + 1}/{len(calls)}: {stem}.pdf")


def make_zip(output_dir: Path, case_name: str) -> Path:
    """Same arcname convention as pipeline._stage_package."""
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in case_name).strip()
    zip_path = output_dir.parent / f"{safe_name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(output_dir):
            for file in files:
                abs_path = os.path.join(root, file)
                arcname = os.path.join(safe_name, os.path.relpath(abs_path, output_dir))
                zf.write(abs_path, arcname)
    return zip_path


def build_package(output_dir: Path, count: int = 10, *, with_audio: bool = True,
                  gen_date: str | None = None) -> list:
    """Build the full synthetic package at *output_dir* (replaced if present).

    Returns the fabricated calls. ``gen_date`` pins the "Generated" stamp so
    the golden regression test gets byte-identical output run to run.
    """
    if output_dir.exists():
        shutil.rmtree(output_dir)
    audio_dir = output_dir / "audio"
    transcripts_dir = output_dir / "transcripts"
    no_summary_dir = output_dir / "transcripts-no-summary"
    for d in (audio_dir, transcripts_dir, no_summary_dir):
        d.mkdir(parents=True)

    print(f"Building {count} synthetic calls …")
    calls = build_calls(count, audio_dir, with_audio=with_audio)

    print("Rendering per-call transcript PDFs …")
    write_call_pdfs(calls, transcripts_dir, no_summary_dir)

    job = Job(
        id="test-package",
        case_name=CASE_NAME,
        input_folder="/test-input",
        summary_prompt="",
        created_at="2026-01-01T00:00:00",
        defendant_name=DEFENDANT,
        calls=calls,
    )

    # Timestamps for the canned findings: first cue of calls 0 and 1.
    def first_cue_ts(call):
        items = parse_summary_sections(call.summary or "").get("review_cue_items") or []
        return (items[0].get("timestamp", "[01:00]") if items else "[01:00]").strip("[]")
    synthesis = CANNED_SYNTHESIS.format(ts0=first_cue_ts(calls[0]), ts1=first_cue_ts(calls[1]))

    print("Generating search.html, viewer.html, guide.pdf, case-report.pdf …")
    asyncio.run(_stage_generate_delivery_assets(
        job, str(output_dir), StubSynthesisEngine(synthesis), gen_date=gen_date,
    ))

    expected = ["search.html", "viewer.html", "guide.pdf", "case-report.pdf"]
    missing = [name for name in expected if not (output_dir / name).is_file()]
    if missing:
        raise RuntimeError(f"missing delivery assets: {', '.join(missing)}")
    return calls


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="test-output", help="output directory (default: test-output/)")
    parser.add_argument("--calls", type=int, default=10, help="number of fake calls (default: 10)")
    parser.add_argument("--zip", action="store_true", help="also build the delivery zip")
    parser.add_argument("--no-audio", action="store_true", help="skip MP3 generation (faster; viewer audio won't play)")
    args = parser.parse_args()

    if not args.no_audio and shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found — install it or pass --no-audio")

    output_dir = Path(args.out).resolve() / "REEVES_TEST_PACKAGE"
    try:
        calls = build_package(output_dir, args.calls, with_audio=not args.no_audio)
    except RuntimeError as e:
        sys.exit(f"FAILED — {e}")

    zip_note = ""
    if args.zip:
        zip_path = make_zip(output_dir, CASE_NAME)
        zip_note = f"\n  zip:        {zip_path}"

    print(f"""
Done — {len(calls)} calls, no AI calls made.
  package:    {output_dir}{zip_note}

Review checklist:
  open "{output_dir / 'search.html'}"
      search a word (e.g. "Darnell"), expand a row, step matches with the arrows
  open "{output_dir / 'viewer.html'}"
      play a call, click transcript words, try Present mode (P), collapse the rails
  open "{output_dir / 'case-report.pdf'}"
      check the cover TOC links, chart, findings, call cards, caller stats
  open a PDF in transcripts/ and its clean copy in transcripts-no-summary/
  open "{output_dir / 'guide.pdf'}"
""")


if __name__ == "__main__":
    main()
