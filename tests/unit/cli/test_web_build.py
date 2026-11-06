import os
import time

from cli.main import _web_build_is_stale


def test_web_build_is_stale_when_dist_is_missing(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.tsx").write_text("export {}", encoding="utf-8")
    assert _web_build_is_stale(tmp_path)


def test_web_build_is_stale_when_source_is_newer(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    source = src / "main.tsx"
    source.write_text("export {}", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    output = dist / "index.html"
    output.write_text("built", encoding="utf-8")
    now = time.time()
    os.utime(output, (now - 10, now - 10))
    os.utime(source, (now, now))
    assert _web_build_is_stale(tmp_path)


def test_web_build_is_stale_when_an_icon_is_newer(tmp_path):
    """The build copies web/public, the logo and app icons, so a redrawn icon needs a new build too."""
    (tmp_path / "src").mkdir()
    (tmp_path / "public").mkdir()
    icon = tmp_path / "public" / "favicon.svg"
    icon.write_text("<svg/>", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    output = tmp_path / "dist" / "index.html"
    output.write_text("built", encoding="utf-8")
    now = time.time()
    os.utime(output, (now - 10, now - 10))
    os.utime(icon, (now, now))
    assert _web_build_is_stale(tmp_path)
