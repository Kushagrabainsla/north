import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { BrowserProfiles, newBrowserProfile, profileTestLabel } from "./browserProfiles";
import { setupNeedsAttention } from "./pages/Setup";
import { SettingsPage } from "./pages/Verbose";

vi.mock("./hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("./hooks")>(),
  useResource: (path: string) => ({
    data: path === "/web/api/browser/profiles" ? { profiles: [], tests: {} } : {
      routing: "auto", power: "cruise", autonomy: "safe",
      autonomy_options: [{ value: "safe", description: "Review actions" }],
      timezone: "UTC", timezone_options: ["UTC"],
    },
    loading: false, error: "", reload: async () => {},
  }),
}));

describe("browser setup evidence", () => {
  it("uses separate stable IDs and visible managed browsers by default", () => {
    const a = newBrowserProfile(), b = newBrowserProfile();
    expect(a.id).not.toBe(b.id);
    expect(a.context).toBe("isolated");
    expect(a.headed).toBe(true);
    expect(a.connect).toBe("");
  });
  it("never calls a connection check a verified login", () => {
    expect(profileTestLabel({ status: "completed", data: { profile_verified: true } })).toContain("login not checked");
    expect(profileTestLabel({ status: "completed" })).toBe("Connection not verified");
    expect(profileTestLabel({ status: "running" })).toContain("Approvals");
    expect(profileTestLabel({ status: "failed", error: "Wrong profile" })).toBe("Wrong profile");
  });
  it("keeps onboarding optional and resumable", () => {
    expect(setupNeedsAttention("not_started")).toBe(true);
    expect(setupNeedsAttention("in_progress")).toBe(true);
    expect(setupNeedsAttention("skipped")).toBe(false);
    expect(setupNeedsAttention("completed")).toBe(false);
  });
});

describe("browser profile presentation", () => {
  it("keeps every existing Settings panel before browser profiles", () => {
    const markup = renderToStaticMarkup(createElement(MemoryRouter, null, createElement(SettingsPage)));
    const titles = [...markup.matchAll(/<h2>(.*?)<\/h2>/g)].map(match => match[1]);
    expect(titles).toEqual(["Model routing", "Power", "Autonomy", "Time zone", "Browser profiles"]);
  });

  it("uses North's existing secondary button variant and a helpful empty state", () => {
    const markup = renderToStaticMarkup(createElement(MemoryRouter, null, createElement(BrowserProfiles)));
    const buttons = [...markup.matchAll(/<button\b[^>]*>/g)].map(match => match[0]);
    expect(buttons).toHaveLength(2);
    expect(buttons.every(button => button.includes('class="ghost-button"'))).toBe(true);
    expect(markup).toContain("No browser profiles yet.");
  });
});
