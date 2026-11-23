import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown } from "./components";

const html = (markdown: string) => renderToStaticMarkup(createElement(Markdown, null, markdown));

describe("a link in a reply or an artifact", () => {
  it("keeps the parentheses in its destination", () => {
    // Issue #76, P10: the href was cut at the first ")", which then trailed the label: "Mercury)".
    const out = html("See [Mercury](https://en.wikipedia.org/wiki/Mercury_(planet)) for more.");

    expect(out).toContain('href="https://en.wikipedia.org/wiki/Mercury_(planet)"');
    expect(out).toContain(">Mercury</a>");
    expect(out).not.toContain("Mercury)");
  });

  it("still ends at the first ) when the destination holds none", () => {
    expect(html("[docs](https://example.com/a) (see above)")).toContain('href="https://example.com/a"');
  });
});

describe("Markdown in a briefing, a reply or a card", () => {
  it("shows a table as a table", () => {
    // North's own parser showed rows of "|" characters.
    const out = html("| Paper | Score |\n| --- | --- |\n| Q-learning | 9 |");
    expect(out).toContain("<table>");
    expect(out).toContain("<td>Q-learning</td>");
  });

  it("keeps a single line break, as replies and cards are written", () => {
    expect(html("first line\nsecond line")).toContain("first line<br/>");
  });

  it("shows raw HTML in the text as text, and opens links in a new tab", () => {
    const out = html("<script>alert(1)</script>\n\nSee [docs](https://example.com).");
    expect(out).not.toContain("<script>");
    expect(out).toContain('target="_blank"');
  });
});
