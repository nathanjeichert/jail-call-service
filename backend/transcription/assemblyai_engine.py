"""
AssemblyAI transcription engine for multichannel jail call audio.
"""

import asyncio
import inspect
import logging
from typing import Dict, List, Optional

from tenacity import retry, retry_if_exception_type, wait_random_exponential, stop_after_attempt

from ..models import TranscriptTurn, WordTimestamp
from .base import mark_continuation_turns

logger = logging.getLogger(__name__)

try:
    import assemblyai as aai
    ASSEMBLYAI_AVAILABLE = True
except ImportError:
    ASSEMBLYAI_AVAILABLE = False
    logger.warning("AssemblyAI SDK not installed. Run: pip install assemblyai")


class AssemblyAIEngine:
    """Cloud transcription via AssemblyAI multichannel API."""

    def __init__(
        self,
        api_key: str,
        speech_model: str = "universal-3-pro",
        polling_interval: int = 15,
    ):
        if not ASSEMBLYAI_AVAILABLE:
            raise RuntimeError("AssemblyAI SDK not installed. Run: pip install assemblyai")
        self.api_key = api_key
        self.speech_model = speech_model
        self.polling_interval = polling_interval

    def _build_config(self) -> "aai.TranscriptionConfig":
        kwargs = {
            "speech_models": [self.speech_model],
            "format_text": True,
            "multichannel": True,
        }
        if "temperature" in inspect.signature(aai.TranscriptionConfig).parameters:
            kwargs["temperature"] = 0.1
        return aai.TranscriptionConfig(**kwargs)

    async def transcribe(
        self,
        audio_path: str,
        channel_labels: Optional[Dict[int, str]] = None,
    ) -> List[TranscriptTurn]:
        aai.settings.api_key = self.api_key
        aai.settings.http_timeout = 600.0

        logger.info("AssemblyAI: starting multichannel transcription: %s", audio_path)

        loop = asyncio.get_event_loop()
        config = self._build_config()

        @retry(
            wait=wait_random_exponential(min=2, max=60),
            stop=stop_after_attempt(4),
            retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
        )
        def _submit():
            t = aai.Transcriber()
            return t.submit(audio_path, config=config)

        transcript = await loop.run_in_executor(None, _submit)

        while True:
            status = transcript.status
            if status == aai.TranscriptStatus.completed:
                break
            if status == aai.TranscriptStatus.error:
                raise RuntimeError(f"AssemblyAI error: {transcript.error}")
            await asyncio.sleep(self.polling_interval)
            tid = transcript.id
            transcript = await loop.run_in_executor(
                None,
                lambda tid=tid: aai.Transcript.get_by_id(tid),
            )

        logger.info("AssemblyAI: transcription done, converting utterances: %s", audio_path)
        turns = _turns_from_multichannel_response(transcript, channel_labels)
        return mark_continuation_turns(turns)


def _turns_from_multichannel_response(
    response: object,
    channel_labels: Optional[Dict[int, str]] = None,
) -> List[TranscriptTurn]:
    transcript = getattr(response, "transcript", None) or response
    utterances = getattr(transcript, "utterances", None) or []

    labels: Dict[int, str] = {}
    if channel_labels:
        for k, v in channel_labels.items():
            try:
                labels[int(k)] = str(v).strip()
            except (TypeError, ValueError):
                pass

    turns: List[TranscriptTurn] = []
    for utterance in utterances:
        channel_raw = getattr(utterance, "channel", None)
        try:
            channel_index = int(channel_raw)
        except (TypeError, ValueError):
            channel_index = 1

        speaker_name = labels.get(channel_index) or f"CHANNEL {channel_index}"

        timestamp_str = None
        if getattr(utterance, "start", None) is not None:
            start_ms = float(utterance.start)
            minutes = int(start_ms // 60000)
            seconds = int((start_ms % 60000) // 1000)
            timestamp_str = f"[{minutes:02d}:{seconds:02d}]"

        word_timestamps: List[WordTimestamp] = []
        for word in getattr(utterance, "words", None) or []:
            word_text = getattr(word, "text", "")
            if not word_text:
                continue
            start_val = getattr(word, "start", None)
            end_val = getattr(word, "end", None)
            if start_val is None or end_val is None:
                continue
            confidence_val = getattr(word, "confidence", None)
            word_timestamps.append(WordTimestamp(
                text=str(word_text),
                start=float(start_val),
                end=float(end_val),
                confidence=float(confidence_val) if confidence_val is not None else None,
                speaker=speaker_name,
            ))

        turns.append(TranscriptTurn(
            speaker=speaker_name,
            text=str(getattr(utterance, "text", "") or ""),
            timestamp=timestamp_str,
            words=word_timestamps if word_timestamps else None,
        ))

    if turns:
        return turns

    text_value = str(getattr(transcript, "text", "") or "").strip()
    if not text_value:
        return []
    return [TranscriptTurn(speaker="CHANNEL 1", text=text_value, timestamp="[00:00]")]
