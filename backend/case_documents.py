"""Case documents: files the operator attaches to a job as case context.

A criminal complaint, a police report, or any other PDF / Word / text file
is converted to text once at job creation (``extract_document_text``) and
stored on the job as a :class:`CaseDocument`. ``build_case_documents_block``
renders those documents as one prompt block that ``job_settings``
(``effective_summary_prompt``) and ``delivery/case_report`` append to their
prompts. The text never enters ``Job.summary_prompt``.

Leaf module: imports only stdlib, ``config``, and ``models``; pypdf and
python-docx are imported lazily inside the extractors.
"""

import os
import re
from typing import Iterable, Optional, Tuple

from . import config as cfg
from .models import CaseDocument

DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".txt", ".md"})

CASE_DOCUMENTS_HEADER = "CASE DOCUMENTS:"
CASE_DOCUMENTS_INSTRUCTION = (
    "Provided by the legal team for context only. Use them to judge what in a call "
    "may be relevant; never treat their contents as something said on the call."
)

_TRAILING_SPACES = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_LINE_RUNS = re.compile(r"\n{3,}")


def normalize_document_text(text: str) -> str:
    """Strip trailing spaces per line and collapse runs of blank lines to one paragraph break."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_SPACES.sub("", text)
    text = _BLANK_LINE_RUNS.sub("\n\n", text)
    return text.strip()


def _extract_pdf(path: str) -> Tuple[str, Optional[int]]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages), len(pages)


def _extract_docx(path: str) -> Tuple[str, Optional[int]]:
    from docx import Document
    from docx.table import Table

    document = Document(path)
    chunks = []
    for block in document.iter_inner_content():  # paragraphs and tables in document order
        if isinstance(block, Table):
            for row in block.rows:
                cells = [cell.text.strip() for cell in row.cells]
                chunks.append(" | ".join(cell for cell in cells if cell))
        else:
            chunks.append(block.text)
    return "\n".join(chunks), None


def _extract_plain_text(path: str) -> Tuple[str, Optional[int]]:
    with open(path, "r", encoding="utf-8", errors="replace") as fp:
        return fp.read(), None


_EXTRACTORS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".txt": _extract_plain_text,
    ".md": _extract_plain_text,
}


def extract_document_text(path: str) -> CaseDocument:
    """Read one document from disk and return it with its text normalized.

    Raises ``ValueError`` with an operator-facing message for an unsupported
    extension (including legacy ``.doc``), a missing or unreadable file, or a
    file that yields no text (a scanned image, typically).
    """
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext not in DOCUMENT_EXTENSIONS:
        raise ValueError(f"Unsupported document type: {name}. Save it as PDF or .docx and try again.")
    if not os.path.isfile(path):
        raise ValueError(f"Document not found: {path}")
    try:
        raw, pages = _EXTRACTORS[ext](path)
    except ValueError:
        raise
    except Exception as exc:  # a corrupt PDF or docx: pypdf / python-docx raise their own types
        raise ValueError(f"Could not read {name}: {exc}") from exc
    text = normalize_document_text(raw)
    if not text:
        raise ValueError(f"No text could be extracted from {name}. If it is a scanned image, run OCR first.")
    return CaseDocument(name=name, path=os.path.abspath(path), text=text, pages=pages)


def _document_header(document: CaseDocument) -> str:
    if document.pages is None:
        return f"=== {document.name} ==="
    unit = "page" if document.pages == 1 else "pages"
    return f"=== {document.name} ({document.pages} {unit}) ==="


def build_case_documents_block(documents: Optional[Iterable[CaseDocument]], max_chars: Optional[int] = None) -> str:
    """Render the documents as one prompt block, or "" when there are none.

    The concatenated document text (headers excluded) is capped at
    ``max_chars`` (default ``config.MAX_CASE_DOCUMENT_CHARS``); a document
    that does not fit at all is dropped, and any truncation ends the block
    with a marker saying how many characters were omitted.
    """
    documents = list(documents or [])
    if not documents:
        return ""
    remaining = cfg.MAX_CASE_DOCUMENT_CHARS if max_chars is None else max_chars
    lines = [CASE_DOCUMENTS_HEADER, CASE_DOCUMENTS_INSTRUCTION]
    omitted = 0
    for document in documents:
        text = document.text
        if remaining <= 0:
            omitted += len(text)
            continue
        if len(text) > remaining:
            omitted += len(text) - remaining
            text = text[:remaining]
        remaining -= len(text)
        lines.extend(["", _document_header(document), text])
    if omitted:
        lines.append(f"[... truncated: {omitted} more characters omitted]")
    return "\n".join(lines)
