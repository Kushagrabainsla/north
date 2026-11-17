import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { choiceText, choiceValue, ModelSelect, SettingsPage, valueChoice } from "./Settings";
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

describe("a row's one list (#80)", () => {
  it("names Same as default, Auto with its power, and a provider's model as one value each", () => {
    expect(choiceValue(null)).toBe("same");
    expect(choiceValue({ model: "", power: "eco" })).toBe("auto:eco");
    expect(choiceValue({ model: "anthropic:claude-sonnet-5-5", power: "eco" })).toBe("model:anthropic:claude-sonnet-5-5");
  });

  it("reads a value back, keeping the row's power under a chosen model", () => {
    const row = { model: "", power: "sport" };
    expect(valueChoice("same", row)).toBeNull();
    expect(valueChoice("auto:eco", row)).toEqual({ model: "", power: "eco" });
    expect(valueChoice("model:openrouter:qwen/qwen3:free", row)).toEqual({ model: "openrouter:qwen/qwen3:free", power: "sport" });
  });

  it("names a chosen model without its provider, and Auto with its power", () => {
    expect(choiceText({ model: "anthropic:claude-sonnet-5-5", power: "cruise" })).toBe("claude-sonnet-5-5");
    expect(choiceText({ model: "openrouter:qwen/qwen3:free", power: "cruise" })).toBe("qwen/qwen3:free");
    expect(choiceText({ model: "", power: "eco" })).toBe("Auto · eco");
  });

  it("keeps a saved model the catalog no longer lists, rather than showing another", () => {
    const markup = renderToStaticMarkup(createElement(ModelSelect, {
      label: "Checks", choice: { model: "groq:gone-model", power: "cruise" }, catalog, busy: false, onChange: () => {},
    }));
    expect(markup).toContain('<optgroup label="Not reachable now"><option value="model:groq:gone-model" selected="">');
  });
});

describe("the Models panel", () => {
  it("shows the Default row and each kind, each with one list", () => {
    const markup = render();
    for (const title of ["Default", "Your tasks", "Background"]) expect(markup).toContain(`<b>${title}</b>`);
    expect(markup.split('class="kit-row-side"><div class="kit-field"').length - 1).toBe(3);
  });

  it("offers Same as default on a kind's list only, naming what the default is", () => {
    const markup = render();
    expect(markup.match(/Same as default \(claude-sonnet-5-5\)/g)).toHaveLength(2);
    expect(markup).toContain('<option value="auto:eco" selected="">Auto · eco</option>');
    expect(markup).toContain('<optgroup label="anthropic">');
  });
});
