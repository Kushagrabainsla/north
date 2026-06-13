"""Server and process management helper functions for the north CLI.

Extracted from cli/main.py to decouple commands from process control details.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import httpx


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _is_north_server(host: str, port: int) -> bool:
    try:
        resp = httpx.get(f"http://{host}:{port}/docs", timeout=2.0)
        return resp.status_code == 200
    except Exception:
        return False


def _sync_docker_secret(compose_file: Path) -> None:
    """Read the north secret from the running Docker container and cache it locally."""
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "exec", "-T", "north", "cat", "/data/secret.key"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            secret_path = Path.home() / ".north" / "secret.key"
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_text(result.stdout.strip(), encoding="utf-8")
    except Exception:
        pass


def _kill_port(host: str, port: int) -> bool:
    try:
        import psutil

        killed = False
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr.port == port and conn.status == "LISTEN":
                try:
                    psutil.Process(conn.pid).kill()
                    killed = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        return killed
    except ImportError:
        # Fallback: platform-agnostic subprocess approach
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True,
                text=True,
            )
            pids = result.stdout.strip().split()
            if not pids:
                return False
            subprocess.run(["kill", "-9"] + pids, capture_output=True)
            return True
        except Exception:
            return False
    except Exception:
        return False


def _find_compose_file() -> Path | None:
    """Return the canonical (image-based) compose file to use.

    Priority:
    1. Walk up from CWD for a docker-compose.yml the user dropped in their tree.
    2. ~/.north/docker-compose.yml — written on first run from the bundled copy.
    3. Bundled copy inside the installed package (cli/docker-compose.yml).

    The source-build variant lives at docker-compose.dev.yml and is selected
    explicitly (docker compose -f docker-compose.dev.yml …); it is intentionally
    never auto-discovered here, so `north start --docker` behaves identically
    regardless of the working directory.
    """
    north_home = Path(os.environ.get("NORTH_HOME", "~/.north")).expanduser()
    installed = north_home / "docker-compose.yml"

    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        f = candidate / "docker-compose.yml"
        if f.exists() and f != installed:
            return f

    if not installed.exists():
        bundled = Path(__file__).parent / "docker-compose.yml"
        if bundled.exists():
            north_home.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled, installed)

    return installed if installed.exists() else None


def _find_git_root(start: Path) -> Path:
    """Walk up from start to find the git repo root. Falls back to start."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def _resolve_workspace(explicit: str | None) -> str:
    """Resolve the effective workspace root — the single source of truth.

    An explicit --workspace always wins. Otherwise default to the enclosing git
    root, except when that root is $HOME itself (e.g. a dotfiles repo at ~/.git),
    in which case fall back to the current directory so the tool sandbox is never
    silently widened to the whole home directory. Running directly in $HOME still
    yields $HOME because cwd is home.
    """
    if explicit:
        return str(Path(explicit).resolve())
    cwd = Path.cwd()
    root = _find_git_root(cwd)
    return str(cwd if root == Path.home() else root)


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _get_install_url() -> tuple[str | None, bool]:
    """Return (url, is_git_url) describing how north was installed.

    Reads the PEP 610 direct_url.json from the installed dist-info.
    is_git_url is True when north was installed with `uv tool install git+<url>`,
    meaning `uv tool upgrade north` is the right update path.
    """
    try:
        import json as _json
        from importlib.metadata import Distribution

        du = Distribution.from_name("north").read_text("direct_url.json")
        if du:
            data = _json.loads(du)
            url = data.get("url", "")
            is_git = "vcs_info" in data and not url.startswith("file://")
            return url, is_git
    except Exception:
        pass
    return None, False


def _find_project_root() -> Path | None:
    """Find the north project root (directory containing pyproject.toml + agents/).

    Walks up from __file__ first (editable installs where main.py lives in the
    repo), then falls back to CWD (running directly from the checkout).
    """
    for p in Path(__file__).resolve().parents:
        if (p / "pyproject.toml").exists() and (p / "agents").is_dir():
            return p
    for p in [Path.cwd(), *Path.cwd().parents]:
        if (p / "pyproject.toml").exists() and (p / "agents").is_dir():
            return p
    return None


def _start_server_process(port: int, project_root: Path | None = None) -> subprocess.Popen:
    """Spawn uvicorn and record the PID. Mirrors the logic in the start command."""
    from config.settings import settings

    log_path = settings.north_home / "north.log"
    pid_path = settings.north_home / "north.pid"
    workspace_path = settings.north_home / "workspace.txt"
    workspace = (
        workspace_path.read_text(encoding="utf-8").strip() if workspace_path.exists() else _resolve_workspace(None)
    )
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "orchestrator.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "info",
    ]
    server_env = {**os.environ, "NORTH_NORTH_WORKSPACE": workspace}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file, env=server_env)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    return proc


def _git_describe(root: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%h %s"],
            capture_output=True,
            text=True,
            cwd=root,
            timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None
