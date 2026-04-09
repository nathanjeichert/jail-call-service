"""
End-to-end batch test: 30 calls, Parakeet transcription, Qwen summarization.
Tracks timing per pipeline stage and opens 5 random PDFs at the end.
"""

import asyncio
import glob
import os
import random
import subprocess
import time

# Collect 30 WAV files from uploads
UPLOADS = "/Users/nathanieleichert/Jail Call Service/uploads"
all_wavs = sorted(glob.glob(os.path.join(UPLOADS, "**/*.wav"), recursive=True))
selected = all_wavs[:30]
print(f"Selected {len(selected)} WAV files for batch test")

# ── Run pipeline directly (no server needed) ──

async def main():
    # Ensure DB schema is up to date
    from backend.db import Base, engine as db_engine
    Base.metadata.create_all(bind=db_engine)

    from backend import job_store, config as cfg
    from backend.pipeline import run_job

    # Create the job
    job = job_store.create_job(
        case_name="Qwen Batch Test (30 calls)",
        input_folder="",
        summary_prompt=cfg.DEFAULT_SUMMARY_PROMPT,
        defendant_name="Test Defendant",
        skip_summary=False,
        file_paths=selected,
        transcription_engine="parakeet",
        summarization_engine="qwen",
    )
    print(f"Job created: {job.id}")
    print(f"Pipeline mode: ALL-LOCAL (two-phase: Parakeet then Qwen)")
    print()

    # Instrument timing by watching job stage changes
    stage_times = {}
    last_stage = None
    last_time = time.time()
    start_time = last_time

    # Poll stage in a background task
    done_event = asyncio.Event()

    async def monitor():
        nonlocal last_stage, last_time
        while not done_event.is_set():
            current_stage = job_store.get_job_stage(job.id)
            if current_stage != last_stage:
                now = time.time()
                if last_stage is not None:
                    stage_times[last_stage] = stage_times.get(last_stage, 0) + (now - last_time)
                    print(f"  [{last_stage}] completed in {now - last_time:.1f}s")
                last_stage = current_stage
                last_time = now
                print(f"Stage: {current_stage}")
            await asyncio.sleep(1)
        # Record final stage
        if last_stage:
            now = time.time()
            stage_times[last_stage] = stage_times.get(last_stage, 0) + (now - last_time)

    monitor_task = asyncio.create_task(monitor())

    # Run the pipeline
    print("Starting pipeline...")
    print()
    await run_job(job.id)
    done_event.set()
    await monitor_task

    total_time = time.time() - start_time

    # Print timing summary
    print()
    print("=" * 50)
    print("TIMING SUMMARY")
    print("=" * 50)
    for stage, duration in stage_times.items():
        print(f"  {stage:20s}  {duration:7.1f}s  ({duration/60:.1f}m)")
    print(f"  {'TOTAL':20s}  {total_time:7.1f}s  ({total_time/60:.1f}m)")
    print()

    # Reload job to get output paths
    job = job_store.get_job(job.id)
    output_dir = job_store.get_job_output_dir(job.id)
    transcripts_dir = os.path.join(output_dir, "transcripts")

    # Count results
    done_calls = [c for c in job.calls if c.status == "done"]
    error_calls = [c for c in job.calls if c.status == "error"]
    print(f"Results: {len(done_calls)} done, {len(error_calls)} errors out of {len(job.calls)} total")

    if error_calls:
        print("Errors:")
        for c in error_calls[:5]:
            print(f"  {c.filename}: {c.error}")

    # Open 5 random PDFs
    pdfs = glob.glob(os.path.join(transcripts_dir, "*.pdf"))
    if pdfs:
        sample = random.sample(pdfs, min(5, len(pdfs)))
        print(f"\nOpening {len(sample)} random PDFs...")
        for pdf in sample:
            print(f"  {os.path.basename(pdf)}")
            subprocess.run(["open", pdf])
    else:
        print("No PDFs found!")

asyncio.run(main())
