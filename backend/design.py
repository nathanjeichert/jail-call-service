"""
Design constants — backwards-compatibility shim.

The shared design tokens, font registration, and template utilities have
moved to ``pdf_utils.py``.  This module re-exports ``RELEVANCE_DESC`` so
that any lingering imports continue to work.
"""

from .pdf_utils import RELEVANCE_DESC  # noqa: F401

__all__ = ["RELEVANCE_DESC"]
