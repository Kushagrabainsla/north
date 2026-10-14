import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { ArtifactLibrary } from "./Artifacts";
import { SETUP_STEPS, YourFiles } from "./Setup";
import { TelegramConnection } from "../connections";

const DATA: Record<string, unknown> = {
  "/web/api/artifacts": [
    { id: "b", name: "2026-10-12.md", kind: "news", task: "t1", updated_at: 10, media_type: "text/markdown",
      owner: { kind: "flow", name: "daily-news-briefing", link: "/flows" } },
    { id: "c", name: "plan.md", kind: "notes", task: "t2", updated_at: 20, media_type: "text/markdown",
      owner: { kind: "session", name: "Trip planning", link: "/sessions/c1" } },
  ],
  "/web/api/bootstrap": { status: "not_started", candidates: ["/Users/me/Documents/resume.pdf"], completed: [], candidate_count: 1 },
  "/web/api/telegram": { configured: true, token_hint: "1234…wxyz", allowed_chat_ids: "12345", problem: "", bot: "" },
};

vi.mock("../hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("../hooks")>(),
  useResource: (path: string) => ({ data: DATA[path], loading: false, error: "", reload: async () => {} }),
}));

const render = (element: ReturnType<typeof createElement>) =>
  renderToStaticMarkup(createElement(MemoryRouter, null, element));

describe("onboarding", () => {
  it("walks the connections and your files, with no separate permissions step", () => {
    expect(SETUP_STEPS).toEqual(["AI connection", "Coding agents", "Browser profiles", "Telegram", "Your files", "Finish"]);
  });

  it("sets up Telegram in the dashboard, never showing the saved token", () => {
    const markup = render(createElement(TelegramConnection));
    expect(markup).toContain("@BotFather");
    expect(markup).toContain("@userinfobot");
    expect(markup).toContain("Ready 1234…wxyz");
    expect(markup).toContain('value="12345"');
    expect(markup).toContain("Disconnect");
    expect(markup).not.toMatch(/type="password"[^>]*value="[^"]+"/);
  });

  it("explains what reading your files does and never starts on its own", () => {
    const markup = render(createElement(YourFiles));
    expect(markup).toContain("Nothing starts until you press the button.");
    expect(markup).toContain("goes to your AI provider");
    expect(markup).toContain("Read my files");
    expect(markup).toContain("resume.pdf");
  });
});

describe("artifacts", () => {
  it("groups every file under the flow, session or task that made it, by name", () => {
    const markup = render(createElement(ArtifactLibrary));
    const headings = [...markup.matchAll(/<h3>(.*?)<small>/g)].map(match => match[1].replace(/<[^>]+>/g, " ").trim());
    expect(headings).toEqual(["Session  Trip planning", "Flow  daily-news-briefing"]);
    expect(markup).not.toContain(">t1<");
  });
});
