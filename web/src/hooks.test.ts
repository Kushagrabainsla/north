import { describe, expect, it } from "vitest";
import { parsePersistentValue } from "./hooks";

describe("persistent UI preferences", () => {
  it("keeps JSON value types", () => {
    expect(parsePersistentValue("true", false)).toBe(true);
    expect(parsePersistentValue("320", 250)).toBe(320);
  });

  it("continues to read legacy plain-string preferences", () => {
    expect(parsePersistentValue("comfortable", "compact", value => typeof value === "string")).toBe("comfortable");
  });

  it("falls back when a stored value is no longer valid", () => {
    expect(parsePersistentValue("900", 250, value => typeof value === "number" && value <= 520)).toBe(250);
    expect(parsePersistentValue(null, "documents")).toBe("documents");
  });
});
