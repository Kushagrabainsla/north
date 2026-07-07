"""Minimal, robust Language Server Protocol client (#1 LSP integration).

Powers *semantically accurate* code intelligence for engineering agents - real
cross-file "find references" and safe project-wide "rename symbol" - by driving a
language server over stdio JSON-RPC, instead of the brittle regex heuristics that
`find_references`/`search_symbols` fall back to.

Deliberately short-lived: each operation spawns a server, initializes on the
workspace root, opens the target file, runs one request, and tears the server
down. That trades a ~1-2s startup for zero long-lived process lifecycle to manage
- the right call for infrequent, high-value operations (references, rename).

Language support is a lookup table keyed on file suffix; only servers actually
installed on PATH are used, and everything fails soft (raises LspUnavailable)
so callers can fall back to the regex tools. Verified against pyright-langserver.
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# file suffix -> language server command (first token must resolve on PATH).
_SERVERS: dict[str, list[str]] = {
    ".py": ["pyright-langserver", "--stdio"],
    ".go": ["gopls"],
    ".ts": ["typescript-language-server", "--stdio"],
    ".tsx": ["typescript-language-server", "--stdio"],
    ".js": ["typescript-language-server", "--stdio"],
    ".jsx": ["typescript-language-server", "--stdio"],
}
_LANGUAGE_ID: dict[str, str] = {
    ".py": "python",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
}
_ANALYZE_WAIT_S: float = 1.5  # give the server a moment to index before the first query
_REQUEST_TIMEOUT_S: float = 20.0


class LspUnavailable(Exception):
    """No language server is installed/available for the requested file."""


class LspError(Exception):
    """The language server returned an error or malformed response."""


def server_command_for(suffix: str) -> list[str] | None:
    """Return the server command for *suffix* if its binary is on PATH, else None."""
    cmd = _SERVERS.get(suffix.lower())
    if cmd and shutil.which(cmd[0]):
        return cmd
    return None


def _uri(path: Path) -> str:
    return "file://" + str(path)


def _path_from_uri(uri: str) -> Path:
    return Path(uri.removeprefix("file://"))


class _Connection:
    """Blocking stdio JSON-RPC connection to a language server process."""

    def __init__(self, cmd: list[str]) -> None:
        self._proc = subprocess.Popen(  # noqa: S603 - cmd comes from a fixed internal table
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._id = 0
        self._buf = b""

    def _send(self, obj: dict[str, Any]) -> None:
        data = json.dumps(obj).encode()
        assert self._proc.stdin is not None
        self._proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        self._proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], timeout: float = _REQUEST_TIMEOUT_S) -> Any:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        end = time.time() + timeout
        while time.time() < end:
            msg = self._read_message(end - time.time())
            if msg is None:
                break
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise LspError(str(msg["error"]))
                return msg["result"]
            # ignore server-initiated requests/notifications/progress
        raise LspError(f"timeout waiting for {method}")

    def _read_message(self, timeout: float) -> dict[str, Any] | None:
        assert self._proc.stdout is not None
        end = time.time() + max(0.0, timeout)
        while b"\r\n\r\n" not in self._buf:
            if time.time() >= end:
                return None
            chunk = self._proc.stdout.read1(4096)
            if not chunk:
                time.sleep(0.01)
                continue
            self._buf += chunk
        header, _, rest = self._buf.partition(b"\r\n\r\n")
        length = 0
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        while len(rest) < length:
            chunk = self._proc.stdout.read1(4096)
            if not chunk:
                if time.time() >= end:
                    return None
                time.sleep(0.01)
                continue
            rest += chunk
        self._buf = rest[length:]
        return json.loads(rest[:length])

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.request("shutdown", {}, timeout=3)
            self.notify("exit", {})
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            with contextlib.suppress(Exception):
                self._proc.kill()


def _name_position(text: str, symbol: str) -> tuple[int, int] | None:
    """0-based (line, character) of *symbol*'s definition name, via Python AST."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef) and node.name == symbol:
            kw = "class " if isinstance(node, ast.ClassDef) else "def "
            return node.lineno - 1, node.col_offset + len(kw)
    return None


def _open(conn: _Connection, root: Path, target: Path, text: str) -> None:
    conn.request(
        "initialize",
        {
            "processId": None,
            "rootUri": _uri(root),
            "capabilities": {},
            "workspaceFolders": [{"uri": _uri(root), "name": root.name}],
        },
    )
    conn.notify("initialized", {})
    conn.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": _uri(target),
                "languageId": _LANGUAGE_ID.get(target.suffix.lower(), "plaintext"),
                "version": 1,
                "text": text,
            }
        },
    )
    time.sleep(_ANALYZE_WAIT_S)


def find_references(root: Path, target: Path, line: int, char: int) -> list[tuple[str, int, int]]:
    """Return semantic references to the symbol at (line, char) as (rel_path, line, char), 1-based.

    Raises LspUnavailable when no server is installed for the file's language.
    """
    cmd = server_command_for(target.suffix)
    if cmd is None:
        raise LspUnavailable(f"no language server for {target.suffix}")
    conn = _Connection(cmd)
    try:
        _open(conn, root, target, target.read_text(encoding="utf-8", errors="replace"))
        result = conn.request(
            "textDocument/references",
            {
                "textDocument": {"uri": _uri(target)},
                "position": {"line": line, "character": char},
                "context": {"includeDeclaration": True},
            },
        )
    finally:
        conn.close()
    out: list[tuple[str, int, int]] = []
    for loc in result or []:
        p = _path_from_uri(loc["uri"])
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            rel = str(p)
        start = loc["range"]["start"]
        out.append((rel, start["line"] + 1, start["character"] + 1))
    return out


def rename_symbol(root: Path, target: Path, symbol: str, new_name: str) -> tuple[int, int, list[str]]:
    """Rename *symbol* (defined in *target*) project-wide. Returns (files, edits, changed_rel_paths).

    Applies the language server's WorkspaceEdit to disk. Raises LspUnavailable when
    no server exists for the language, or LspError when the rename can't be resolved.
    """
    cmd = server_command_for(target.suffix)
    if cmd is None:
        raise LspUnavailable(f"no language server for {target.suffix}")
    text = target.read_text(encoding="utf-8", errors="replace")
    pos = _name_position(text, symbol) if target.suffix.lower() == ".py" else None
    if pos is None:
        raise LspError(f"could not locate a definition of {symbol!r} in {target.name}")
    line, char = pos

    conn = _Connection(cmd)
    try:
        _open(conn, root, target, text)
        edit = conn.request(
            "textDocument/rename",
            {
                "textDocument": {"uri": _uri(target)},
                "position": {"line": line, "character": char},
                "newName": new_name,
            },
        )
    finally:
        conn.close()
    if not edit:
        raise LspError("the language server refused the rename (no edit produced)")
    return _apply_workspace_edit(root, edit)


def _apply_workspace_edit(root: Path, edit: dict[str, Any]) -> tuple[int, int, list[str]]:
    """Apply an LSP WorkspaceEdit (documentChanges or changes) to disk, within *root*."""
    per_file: dict[Path, list[dict[str, Any]]] = {}
    if edit.get("documentChanges"):
        for dc in edit["documentChanges"]:
            if "textDocument" not in dc or "edits" not in dc:
                continue  # skip create/rename/delete file operations
            per_file.setdefault(_path_from_uri(dc["textDocument"]["uri"]), []).extend(dc["edits"])
    elif edit.get("changes"):
        for uri, edits in edit["changes"].items():
            per_file.setdefault(_path_from_uri(uri), []).extend(edits)

    root_resolved = root.resolve()
    changed: list[str] = []
    total_edits = 0
    for path, edits in per_file.items():
        resolved = path.resolve()
        try:
            rel = resolved.relative_to(root_resolved)  # never edit outside the workspace
        except ValueError:
            continue
        if not resolved.exists():
            continue
        new_text = _apply_edits(resolved.read_text(encoding="utf-8", errors="replace"), edits)
        resolved.write_text(new_text, encoding="utf-8")
        changed.append(str(rel))
        total_edits += len(edits)
    return len(changed), total_edits, sorted(changed)


def _apply_edits(text: str, edits: list[dict[str, Any]]) -> str:
    """Apply LSP TextEdits to *text*. Edits are applied last-to-first so offsets stay valid."""
    lines = text.splitlines(keepends=True)
    line_starts = [0]
    for ln in lines:
        line_starts.append(line_starts[-1] + len(ln))

    def offset(pos: dict[str, int]) -> int:
        line = min(pos["line"], len(line_starts) - 1)
        return line_starts[line] + pos["character"]

    resolved = sorted(
        ((offset(e["range"]["start"]), offset(e["range"]["end"]), e["newText"]) for e in edits),
        key=lambda x: x[0],
        reverse=True,
    )
    for start, end, new in resolved:
        text = text[:start] + new + text[end:]
    return text


def python_symbol_position(text: str, symbol: str) -> tuple[int, int] | None:
    """Public helper: 0-based (line, char) of a Python symbol's definition name."""
    return _name_position(text, symbol)
