import { describe, expect, it } from "vitest";
import { providerFailing, providerStateLabel } from "./connections";

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
