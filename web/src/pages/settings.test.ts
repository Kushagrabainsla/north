import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { describeChoice, ModelPicker, rowMode, SettingsPage } from "./Settings";
import { DialogProvider } from "../dialog";

const settings = {
  power: "cruise",
  autonomy: "ask",
  autonomy_options: [{ value: "ask", description: "Ask first" }],
  models: {
    default: { model: "anthropic:claude-sonnet-5-5", power: "cruise" },
    kinds: [
      { kind: "tasks", label: "Your tasks", description: "The work you ask north to do.", choice: null },
      { kind: "background", label: "Background", description: "Small jobs.", choice: { model: "", power: "eco" } },
    ],
  },
  timezone: "UTC",
  timezone_options: ["UTC"],
};
const catalog = [{ provider: "anthropic", models: ["claude-sonnet-5-5", "claude-haiku-5-5"] }];

vi.mock("../hooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("../hooks")>(),
  useResource: (path: string) => ({
    data: path === "/orchestrator/inference/catalog" ? catalog : path === "/orchestrator/settings" ? settings : null,
    loading: false,
    error: "",
    reload: async () => {},
  }),
}));

const render = () =>
  renderToStaticMarkup(createElement(MemoryRouter, null, createElement(DialogProvider, null, createElement(SettingsPage))));

describe("a row of the Models panel (#80)", () => {
  it("is Same as default with no row, Auto with no model, else a chosen model", () => {
    expect(rowMode(null)).toBe("same");
    expect(rowMode({ model: "", power: "eco" })).toBe("auto");
    expect(rowMode({ model: "anthropic:claude-sonnet-5-5", power: "eco" })).toBe("model");
  });

  it("names a chosen model without its provider, and Auto with its power", () => {
    expect(describeChoice({ model: "anthropic:claude-sonnet-5-5", power: "cruise" })).toBe("claude-sonnet-5-5");
    expect(describeChoice({ model: "openrouter:qwen/qwen3:free", power: "cruise" })).toBe("qwen/qwen3:free");
    expect(describeChoice({ model: "", power: "eco" })).toBe("Auto, eco");
  });
});

describe("the Models panel", () => {
  it("shows the Default row and each kind, in one panel", () => {
    const markup = render();
    expect(markup).toContain("Default");
    expect(markup).toContain("Your tasks");
    expect(markup).toContain("Background");
    expect(markup).not.toContain("Model routing");
  });

  it("shows power only on a row that is Auto, and what a Same-as-default row uses", () => {
    const markup = render();
    expect(markup).toContain('aria-label="Background power"');
    expect(markup).not.toContain('aria-label="Default power"');
    expect(markup).not.toContain('aria-label="Your tasks power"');
    expect(markup).toContain("Same as the Default row: claude-sonnet-5-5.");
  });

  it("chooses what a row uses with the kit's tabs, the Default row without a default tab", () => {
    const tabs = (row: string) => {
      const list = render().split(`aria-label="${row} uses"`)[1].split("</div>")[0];
      return [...list.matchAll(/role="tab"[^>]*>([^<]+)</g)].map((match) => match[1]);
    };
    expect(tabs("Your tasks")).toEqual(["default", "auto", "model"]);
    expect(tabs("Default")).toEqual(["auto", "model"]);
  });

  it("offers a placeholder until a model is chosen, so even a provider's only model can be picked", () => {
    const picker = (value: string) =>
      renderToStaticMarkup(createElement(ModelPicker, { value, busy: false, onPick: () => {} }));
    expect(picker("")).toContain('<option value="" disabled="" selected="">Choose a model</option>');
    expect(picker("anthropic:claude-haiku-5-5")).not.toContain("Choose a model");
  });
});
