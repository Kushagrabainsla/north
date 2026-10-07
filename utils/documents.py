"""Text out of document files that are not plain text (PDF, Word).

Shared by the tools that read files and the memory layer that imports documents, which may not import
each other.
"""

from __future__ import annotations

import io


def pdf_text(content: bytes) -> str:
    """The text of a PDF, page by page. An empty string for a PDF with no text layer (a scan)."""
    from pypdf import PdfReader  # type: ignore[import-untyped]

    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


def docx_text(content: bytes) -> str:
    """The text of a Word document, one paragraph per line."""
    from docx import Document  # type: ignore[import-untyped]

    document = Document(io.BytesIO(content))
    return "\n".join(p.text for p in document.paragraphs if p.text.strip()).strip()
