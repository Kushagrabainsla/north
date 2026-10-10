import { describe, expect, it } from "vitest";
import { newBrowserProfile, profileTestLabel } from "./browserProfiles";
import { setupNeedsAttention } from "./pages/Setup";

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
