"""North reads the text of a PDF or a Word document: a resume is the file a job application starts from.

`read_file` did this until issue #78; now `north text` does it, from any shell command.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import bind_approvals, rejecting_store
from tools.models import ToolInput
from tools.specialized import _os_sandbox
from tools.specialized.bash import BashTool
from utils.documents import file_text


def _pdf_with_text(path: Path, text: str) -> None:
    """A minimal one-page PDF whose content stream draws *text*."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def test_a_pdf_is_read_as_its_text(tmp_path) -> None:
    resume = tmp_path / "resume.pdf"
    _pdf_with_text(resume, "Jane Doe - Software Engineer")

    assert "Jane Doe - Software Engineer" in file_text(resume)


def test_a_pdf_with_no_text_says_so(tmp_path) -> None:
    scan = tmp_path / "scan.pdf"
    _pdf_with_text(scan, "")

    with pytest.raises(ValueError, match="no text layer"):
        file_text(scan)


def test_a_text_file_is_read_as_it_is_and_a_binary_file_is_refused(tmp_path) -> None:
    (tmp_path / "notes.md").write_text("café notes\n", encoding="utf-8")
    (tmp_path / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")

    assert file_text(tmp_path / "notes.md") == "café notes\n"
    with pytest.raises(ValueError, match="binary"):
        file_text(tmp_path / "photo.png")


@pytest.mark.skipif(_os_sandbox.current() is None, reason="needs macOS Seatbelt")
async def test_a_shell_command_reads_a_pdf_without_a_card(tmp_path) -> None:
    """`north text` is on every command's PATH, and reading a PDF changes nothing, so nobody is asked."""
    _pdf_with_text(tmp_path / "resume.pdf", "Jane Doe - Software Engineer")
    tool = bind_approvals(BashTool(os_sandbox=True), store=rejecting_store())

    out = await tool.execute(
        ToolInput(
            params={"command": "north text resume.pdf", "workspace": str(tmp_path)}, granted_workspace=str(tmp_path)
        )
    )

    assert out.success, out.error
    assert "Jane Doe - Software Engineer" in out.data["stdout"]
