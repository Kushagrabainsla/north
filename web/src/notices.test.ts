import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { NoticeList } from "./components";
import { noticeBody, noticePreview, savedFile } from "./notices";

// A reminder that fired reached no one on a dashboard-only install. It now
// waits on the dashboard, saying what it is, until dismissed.
describe("notices", () => {
  it("show what north delivered, with a way to dismiss it", () => {
    const html = renderToStaticMarkup(createElement(MemoryRouter, null, createElement(NoticeList, {
      notices: [{ id: "n1", title: "Reminder", message: "Take the laundry out.", source: "reminder", task_id: null, created_at: new Date().toISOString() }],
      onDismissed: () => {},
    })));
    expect(html).toContain("Reminder");
    expect(html).toContain("Take the laundry out.");
    expect(html).toContain("Dismiss");
  });
});

// The research briefing reached the dashboard whole, hundreds of lines with its ## and ** marks, and the
// news briefing as {"artifacts": [...]}: the Attention panel ran down the page and the System panel beside
// it became a tall empty box.
const BRIEFING = [
  "# Daily Research Briefing - November 6, 2026",
  "",
  "Coverage: October 30 through November 6, 2026. I reviewed all saved briefings.",
  "",
  "## Agentic Harnesses",
  "",
  "### The Power of Meta Access",
  "",
  "- **Submission date:** November 5, 2026",
  "- **URL:** [arXiv](https://arxiv.org/abs/2611.08428)",
  "",
  "This paper argues for a structured, permissioned interface model. ".repeat(12),
  "",
  "[cut short]",
  "",
  "_Saved at /Users/kush/.north/news/research/2026-11-06.md_",
].join("\n");
const JSON_NOTICE = '{"artifacts": ["/Users/kush/.north/tasks/bd15f01a/2026-11-06.md"]}';

describe("a notice's preview, body and file", () => {
  it("previews a briefing in plain words, without its Markdown marks, its cut note or its saved line", () => {
    const preview = noticePreview(BRIEFING);
    expect(preview.startsWith("Daily Research Briefing - November 6, 2026 Coverage: October 30")).toBe(true);
    expect(preview).not.toMatch(/[#*]|\[cut short\]|Saved at/);
    expect(preview.length).toBeLessThanOrEqual(221);
    expect(savedFile(BRIEFING)).toBe("/Users/kush/.north/news/research/2026-11-06.md");
    expect(noticeBody(BRIEFING)).not.toContain("Saved at");
  });

  it("reads a JSON answer naming a file as the file it saved", () => {
    expect(noticePreview(JSON_NOTICE)).toBe("Saved 2026-11-06.md.");
    expect(savedFile(JSON_NOTICE)).toBe("/Users/kush/.north/tasks/bd15f01a/2026-11-06.md");
    expect(noticeBody(JSON_NOTICE)).toBe("");
  });
});

describe("notices on the dashboard and on Approvals", () => {
  const notices = [
    { id: "n1", title: "daily-research-briefing is ready", message: BRIEFING, source: "flow", task_id: null, created_at: new Date().toISOString() },
    { id: "n2", title: "daily-news-briefing is ready", message: JSON_NOTICE, source: "flow", task_id: null, created_at: new Date().toISOString() },
  ];
  const render = (compact: boolean) =>
    renderToStaticMarkup(createElement(MemoryRouter, null, createElement(NoticeList, { notices, onDismissed: () => {}, compact })));

  it("shows one short row each on the dashboard, with the rest a link away", () => {
    const html = render(true);
    expect(html).toContain("notice-preview");
    expect(html).toContain("Read it all");
    expect(html).toContain("saved as <span");
    expect(html).not.toContain("permissioned interface model. This paper argues for a structured, permissioned interface model. This paper");
    expect(html).not.toContain("{&quot;artifacts&quot;");
  });

  it("reads each in full on Approvals, formatted, a long one opened on demand", () => {
    const html = render(false);
    expect(html).toContain("<details");
    expect(html).toMatch(/<h2>(<span>)+Agentic Harnesses(<\/span>)+<\/h2>/);
    expect(html).toContain("<strong>Submission date:</strong>");
  });
});
