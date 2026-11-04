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
