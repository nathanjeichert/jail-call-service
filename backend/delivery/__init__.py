"""Client-facing delivery artifacts.

Everything that ends up inside the delivery ZIP is produced here:

* ``transcript_pdf``  - per-call transcript PDF (cover, summary sheets, ruled transcript)
* ``case_report``     - case-level dossier PDF (Paged.js)
* ``guide_pdf``       - reviewer's user guide PDF
* ``index_html``      - self-contained index.html: call index + synced audio viewer

Shared plumbing: ``pdf_render`` (headless Chromium), ``templates`` (Jinja +
static templates, asset paths), ``fonts`` (embedded/linked @font-face CSS),
``font_metrics`` and ``summary_layout`` (Python-side layout estimation).

Modules here are imported lazily by the pipeline so the API server starts
without paying for Playwright until the first render.
"""
