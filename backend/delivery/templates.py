"""Template and asset locations for the delivery artifacts.

Two kinds of template live in ``delivery/templates/``:

* Jinja templates for the PDFs (``transcript_pdf.html``, ``case_report.html``,
  ``guide.html``), rendered with :func:`render_template`.
* Static single-file HTML pages (``viewer.html``, ``search.html``) whose
  CSS/JS is full of braces, so they use ``__PLACEHOLDER__`` string
  substitution instead of Jinja; load them with :func:`load_static_template`
  and ``.replace()`` the placeholders.
"""

from functools import lru_cache
from pathlib import Path

import jinja2

_PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _PACKAGE_DIR / "templates"
ASSETS_DIR = _PACKAGE_DIR / "assets"

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=False,
    auto_reload=False,
)


def render_template(name: str, **context) -> str:
    """Render a Jinja template from ``delivery/templates/``."""
    return _jinja_env.get_template(name).render(**context)


@lru_cache(maxsize=None)
def load_static_template(name: str) -> str:
    """Read a placeholder-substitution template from ``delivery/templates/``."""
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")
