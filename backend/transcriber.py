"""
Transcription utilities and backward-compatible shims.

The actual engine implementations live in backend.transcription.
This module re-exports shared helpers for use by other modules.
"""

from .transcription.base import normalize_speaker_label, mark_continuation_turns

__all__ = ["normalize_speaker_label", "mark_continuation_turns"]
