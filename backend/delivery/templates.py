"""Template and asset locations for the delivery artifacts.

Every template in ``delivery/templates/`` is Jinja, rendered with
:func:`render_template` and ``autoescape=False``: callers pass pre-escaped
strings and script-safe JSON (:mod:`backend.html_json`). The HTML pages'
CSS/JS contain no Jinja delimiters other than their placeholders, so the
single-file viewer and search templates render the same way as the PDFs.
"""

from pathlib import Path

import jinja2

_PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _PACKAGE_DIR / "templates"
ASSETS_DIR = _PACKAGE_DIR / "assets"

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=False,
    auto_reload=False,
    keep_trailing_newline=True,
)


def render_template(name: str, **context) -> str:
    """Render a Jinja template from ``delivery/templates/``."""
    return _jinja_env.get_template(name).render(**context)
