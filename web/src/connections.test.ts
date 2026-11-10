import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { agentStatus, providerFailing, providerStateLabel, providerStateTone, savedKeyMessage, savedKeyTone, secretDetail, secretProblem, secretRemoval, Secrets } from "./connections";
import { DialogProvider } from "./dialog";

vi.mock("./hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("./hooks")>(),
  useResource: () => ({
    data: { secrets: [{ name: "openweather", note: "", updated_at: "", used_by: ["get_forecast"] }], missing: [] },
    loading: false, error: "", reload: async () => {},
  }),
}));

// Issue #76, M6: "Local embeddings ... Ready" stood beside a model that failed to load (403 Forbidden).
describe("a provider row says what trying it found", () => {
  it("names why the local embedder is not working", () => {
    const local = { id: "local", auth_kind: "local", state: "unavailable", state_detail: "could not load BAAI/bge-small-en-v1.5 (403 Forbidden)" };
    expect(providerStateLabel(local, true)).toBe("Not working: could not load BAAI/bge-small-en-v1.5 (403 Forbidden)");
    expect(providerFailing(local)).toBe(true);
  });

  it("says Ready only for one that works", () => {
    expect(providerStateLabel({ state: "ready" }, true)).toBe("Ready");
    expect(providerStateLabel({ state: "configured", credential_hint: "sk-a…wxyz" }, true)).toBe("Ready sk-a…wxyz");
    expect(providerStateLabel({ state: "loading" }, true)).toBe("Loading");
    expect(providerStateLabel({ state: "not_configured" }, false)).toBe("Not configured");
  });
});

// Issue #76, P6: a key Gemini rejected (400) was answered "Provider credentials saved and runtime refreshed."
describe("saving a key says what trying it found", () => {
  it("says when the provider did not accept the key", () => {
    expect(savedKeyMessage({ state: "failing", state_detail: "Gemini /models returned 400" })).toBe(
      "Key saved, but the provider did not accept it: Gemini /models returned 400",
    );
    expect(savedKeyMessage({ state: "ready" })).toBe("Key saved. The provider answered and its models are listed.");
  });

  it("names a provider whose key was rejected as not working", () => {
    expect(providerStateLabel({ state: "failing", state_detail: "Gemini /models returned 400" }, true)).toBe(
      "Not working: Gemini /models returned 400",
    );
  });
});

describe("the Secrets panel", () => {
  it("says why a name or value cannot be saved, with north's own rules", () => {
    expect(secretProblem("Open Weather", "")).toMatch(/lowercase/);
    expect(secretProblem("openweather", "short")).toMatch(/at least 6/);
    expect(secretProblem("openweather", "owm-1234-abcd")).toBe("");
    expect(secretProblem("", "")).toBe("");
  });

  it("names the tools that use a secret, and says when none does", () => {
    const secret = { name: "openweather", note: "weather", updated_at: "", used_by: ["get_forecast"] };
    expect(secretDetail(secret)).toBe("weather · used by get_forecast");
    expect(secretDetail({ ...secret, note: "", used_by: [] })).toBe("no tool uses it yet");
  });
});

// A key the provider rejected was shown in the success colour, like one it accepted.
describe("connection states have the colour of what they mean", () => {
  it("shows a rejected key as a failure, never as success", () => {
    expect(savedKeyTone({ state: "failing" })).toBe("error");
    expect(savedKeyTone({ state: "ready" })).toBe("success");
    expect(savedKeyTone({})).toBe("info");
  });

  it("colours a provider green only when it works", () => {
    expect(providerStateTone({ state: "failing" }, true)).toBe("bad");
    expect(providerStateTone({ state: "ready" }, true)).toBe("good");
    expect(providerStateTone({ state: "ready" }, false)).toBe("neutral");
    expect(providerStateTone({ state: "loading" }, true)).toBe("busy");
    expect(providerStateTone({ state: "ready" }, true, true)).toBe("busy");
  });

  it("does not call an installed agent ready", () => {
    expect(agentStatus({ installed: true, ready: true })).toEqual({ label: "Ready", tone: "good" });
    expect(agentStatus({ installed: true, ready: false })).toEqual({ label: "Needs attention", tone: "warn" });
    expect(agentStatus({ installed: false, ready: false })).toEqual({ label: "Not installed", tone: "neutral" });
  });

  it("says before removing a secret which tools lose it", () => {
    const secret = { name: "openweather", note: "", updated_at: "", used_by: ["get_forecast"] };
    expect(secretRemoval(secret)).toBe("get_forecast can no longer use it until you add it again.");
    expect(secretRemoval({ ...secret, used_by: [] })).toMatch(/No tool uses it/);
  });
});

describe("the Secrets panel markup", () => {
  it("removes a saved secret with a red Remove", () => {
    const markup = renderToStaticMarkup(createElement(DialogProvider, null, createElement(Secrets)));
    expect(markup).toMatch(/<button[^>]*kit-button-danger[^>]*><span>Remove<\/span><\/button>/);
    expect(markup).toContain("used by get_forecast");
    expect(markup).toContain("<h2>Add a secret</h2>");
  });
});
