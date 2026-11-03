import { describe, expect, it } from "vitest";
import { providerFailing, providerStateLabel, savedKeyMessage } from "./connections";

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
