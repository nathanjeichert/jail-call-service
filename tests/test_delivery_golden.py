"""Golden regression test for the whole delivery package.

Builds the synthetic package (``tests/make_test_package.py``) with a pinned
"Generated" date and no audio, digests every client-facing artifact, and
compares each digest to the committed copy under ``tests/golden/``:

* ``index.html``: the page source verbatim, with the base64 font payloads
  replaced by a marker so the digest stays readable.
* ``search/index.js`` (golden ``search-index.js``): the keyword search
  sidecar verbatim; passages, vocabulary, and postings are deterministic.
  The package is built without the meaning layer, so no runtime assets are
  needed.
* ``case-report.pdf`` / ``guide.pdf`` / every transcript PDF: per-page
  extracted text plus every link annotation (``/Launch`` target, URI, or
  named destination), so pagination, copy, cites, and link portability are
  all pinned.

Any intentional change to a template, the layout core, summaries, or the
delivery code shows up here as a diff. Review it, then refresh the goldens::

    UPDATE_GOLDEN=1 python -m pytest tests/test_delivery_golden.py

On failure the actual digests are written to ``test-output/golden-actual/``
for side-by-side comparison.
"""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path

import pytest
from pypdf import PdfReader

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
ACTUAL_DIR = Path(__file__).resolve().parent.parent / "test-output" / "golden-actual"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"

_FONT_DATA_RE = re.compile(r"data:font/[a-z0-9]+;base64,[A-Za-z0-9+/=]+")


def _digest_html(path: Path) -> str:
    return _FONT_DATA_RE.sub("data:font/<embedded>", path.read_text(encoding="utf-8"))


def _link_line(annot) -> str | None:
    obj = annot.get_object()
    if obj.get("/Subtype") != "/Link":
        return None
    action = obj.get("/A")
    if action is not None:
        action = action.get_object()
        kind = str(action.get("/S", ""))
        target = action.get("/F") if kind == "/Launch" else action.get("/URI")
        if target is None:
            target = action.get("/D")
        return f"  link {kind} {target}"
    dest = obj.get("/Dest")
    if dest is not None:
        return f"  link /Dest {dest}"
    return "  link ?"


def _digest_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    out = [f"# {path.name}: {len(reader.pages)} page(s)"]
    for number, page in enumerate(reader.pages, start=1):
        out.append(f"## page {number}")
        out.append(page.extract_text())
        links = [line for line in (_link_line(a) for a in (page.get("/Annots") or [])) if line]
        out.extend(links)
    return "\n".join(out) + "\n"


def _digest_pdf_dir(directory: Path) -> str:
    return "\n".join(_digest_pdf(p) for p in sorted(directory.glob("*.pdf")))


ARTIFACTS = {
    "index.html": lambda root: _digest_html(root / "index.html"),
    "search-index.js": lambda root: (root / "search" / "index.js").read_text(encoding="utf-8"),
    "case-report.pdf": lambda root: _digest_pdf(root / "case-report.pdf"),
    "guide.pdf": lambda root: _digest_pdf(root / "guide.pdf"),
    "transcripts": lambda root: _digest_pdf_dir(root / "transcripts"),
    "transcripts-no-summary": lambda root: _digest_pdf_dir(root / "transcripts-no-summary"),
}


@pytest.mark.parametrize("name", list(ARTIFACTS))
def test_artifact_matches_golden(package_dir: Path, name: str) -> None:
    actual = ARTIFACTS[name](package_dir)
    golden_path = GOLDEN_DIR / f"{name}.txt"

    if UPDATE:
        GOLDEN_DIR.mkdir(exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return

    assert golden_path.is_file(), (
        f"no golden for {name}; run UPDATE_GOLDEN=1 python -m pytest {Path(__file__).name}"
    )
    expected = golden_path.read_text(encoding="utf-8")
    if actual == expected:
        return

    ACTUAL_DIR.mkdir(parents=True, exist_ok=True)
    (ACTUAL_DIR / f"{name}.txt").write_text(actual, encoding="utf-8")
    diff = list(difflib.unified_diff(
        expected.splitlines(), actual.splitlines(),
        fromfile=f"golden/{name}.txt", tofile=f"actual/{name}.txt", lineterm="", n=2,
    ))
    shown = "\n".join(diff[:80])
    more = f"\n... {len(diff) - 80} more diff lines" if len(diff) > 80 else ""
    pytest.fail(
        f"{name} differs from its golden ({len(diff)} diff lines). "
        f"Actual digest: {ACTUAL_DIR / (name + '.txt')}\n"
        f"If the change is intended: UPDATE_GOLDEN=1 python -m pytest tests/test_delivery_golden.py\n\n"
        f"{shown}{more}"
    )
