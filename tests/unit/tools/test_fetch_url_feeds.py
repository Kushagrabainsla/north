"""A fetched Atom or RSS feed comes back as compact entries, cut only between entries."""

from __future__ import annotations

import pytest

import tools.universal.fetch_url as fetch_url
from tools.models import ToolInput
from utils.net import FetchedText

_ARXIV_ENTRY = """
  <entry>
    <id>http://arxiv.org/abs/2610.{n:05d}v1</id>
    <title>Paper {n}: World
      Models at Scale</title>
    <updated>2026-10-16T10:00:00Z</updated>
    <link href="https://arxiv.org/abs/2610.{n:05d}v1" rel="alternate" type="text/html"/>
    <link href="https://arxiv.org/pdf/2610.{n:05d}v1" rel="related" type="application/pdf" title="pdf"/>
    <summary>We study world models. {padding}</summary>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <published>2026-10-15T17:59:11Z</published>
    <arxiv:comment>Accepted at NeurIPS 2026</arxiv:comment>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
  </entry>"""


def _arxiv_feed(entries: int, padding: str = "") -> str:
    body = "".join(_ARXIV_ENTRY.format(n=n, padding=padding) for n in range(entries))
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'
        f"<title>arXiv Query</title>{body}</feed>"
    )


def _fetch(monkeypatch: pytest.MonkeyPatch, text: str, content_type: str) -> str:
    monkeypatch.setattr(
        fetch_url, "fetch_url_text", lambda url: FetchedText(url=url, text=text, content_type=content_type)
    )
    output = fetch_url._fetch_sync("https://export.arxiv.org/api/query")
    assert output.success
    return output.data["content"]


def test_an_arxiv_feed_keeps_what_a_reader_needs_and_drops_the_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    content = _fetch(monkeypatch, _arxiv_feed(1), "application/atom+xml; charset=utf-8")

    assert content == (
        "Paper 0: World Models at Scale\n"
        "https://arxiv.org/abs/2610.00000v1 | 2026-10-15T17:59:11Z\n"
        "Authors: Ada Lovelace, Alan Turing\n"
        "Comment: Accepted at NeurIPS 2026\n"
        "We study world models."
    )


def test_a_long_feed_is_cut_between_entries_and_says_how_many_were_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _fetch(monkeypatch, _arxiv_feed(25, padding="x" * 2_000), "application/atom+xml")

    shown = content.count("https://arxiv.org/abs/")
    assert 0 < shown < 25
    assert content.endswith(f"[{25 - shown} more entries not shown]")
    assert len(content) <= fetch_url._MAX_CHARS + 50
    assert "<summary>" not in content


def test_an_rss_feed_served_as_text_is_still_read_as_a_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    rss = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>'
        "<item><title>Release notes</title><link>https://example.com/notes</link>"
        "<pubDate>Thu, 15 Oct 2026 09:00:00 GMT</pubDate>"
        "<description>&lt;p&gt;Faster &lt;b&gt;builds&lt;/b&gt;.&lt;/p&gt;</description></item>"
        "</channel></rss>"
    )

    content = _fetch(monkeypatch, rss, "text/plain")

    assert content == "Release notes\nhttps://example.com/notes | Thu, 15 Oct 2026 09:00:00 GMT\nFaster builds ."


@pytest.mark.parametrize(
    ("text", "content_type"),
    [
        ("<?xml version='1.0'?><sitemap><loc>https://example.com</loc></sitemap>", "application/xml"),
        ("<?xml version='1.0'?><broken>", "application/xml"),
    ],
)
def test_xml_that_is_not_a_feed_comes_back_as_it_was(
    monkeypatch: pytest.MonkeyPatch, text: str, content_type: str
) -> None:
    assert _fetch(monkeypatch, text, content_type) == text


_BROKEN = (
    "https://export.arxiv.org/api/query?search_query=all:%22World%20Models%22"
    "%26sortBy=submittedDate%26sortOrder=descending%26max_results=25"
)
_MEANT = (
    "https://export.arxiv.org/api/query?search_query=all:%22World%20Models%22"
    "&sortBy=submittedDate&sortOrder=descending&max_results=25"
)


def test_an_arxiv_query_with_its_ampersands_encoded_is_repaired() -> None:
    """As written, arXiv read the whole string as the search and returned papers from 2024 to Oct 2."""
    url, note = fetch_url._repair_arxiv_query(_BROKEN)

    assert url == _MEANT
    assert note.startswith("Repaired the arXiv query")


@pytest.mark.parametrize(
    "url",
    [_MEANT, "https://example.com/search?q=a%26b", "https://export.arxiv.org/abs/2610.22041"],
)
def test_every_other_url_is_left_exactly_as_given(url: str) -> None:
    assert fetch_url._repair_arxiv_query(url) == (url, "")


@pytest.mark.asyncio
async def test_the_result_says_the_query_was_repaired(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[str] = []

    def fake_fetch(url: str) -> FetchedText:
        fetched.append(url)
        return FetchedText(url=url, text=_arxiv_feed(1), content_type="application/atom+xml")

    monkeypatch.setattr(fetch_url, "fetch_url_text", fake_fetch)

    output = await fetch_url.FetchUrlTool().run(ToolInput(params={"url": _BROKEN}))

    assert fetched == [_MEANT]
    assert output.data["note"].startswith("Repaired the arXiv query")
