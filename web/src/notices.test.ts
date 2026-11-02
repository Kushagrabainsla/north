import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { NoticeList } from "./components";

// A reminder that fired reached no one on a dashboard-only install. It now
// waits on the dashboard, saying what it is, until dismissed.
describe("notices", () => {
  it("show what north delivered, with a way to dismiss it", () => {
    const html = renderToStaticMarkup(createElement(NoticeList, {
      notices: [{ id: "n1", title: "Reminder", message: "Take the laundry out.", source: "reminder", task_id: null, created_at: new Date().toISOString() }],
      onDismissed: () => {},
    }));
    expect(html).toContain("Reminder");
    expect(html).toContain("Take the laundry out.");
    expect(html).toContain("Dismiss");
  });
});
