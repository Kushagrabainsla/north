import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { emptyDraft, ScheduleForm } from "./scheduleForm";
import type { Draft } from "./scheduleForm";

const render = (draft: Partial<Draft>, extra: { error?: string; busy?: boolean } = {}) =>
  renderToStaticMarkup(createElement(ScheduleForm, {
    title: "New schedule",
    draft: { ...emptyDraft(), ...draft },
    setDraft: () => undefined,
    onSubmit: () => undefined,
    onCancel: () => undefined,
    submitLabel: "Add schedule",
    busy: extra.busy ?? false,
    error: extra.error ?? "",
    allowOnce: true,
  }));

describe("the schedule form", () => {
  it("is the kit's form, with Cancel before the one primary Save", () => {
    const html = render({ repeat: "daily" });
    expect(html).toContain('<form class="kit-form">');
    const cancel = html.indexOf(">Cancel<");
    const save = html.indexOf(">Add schedule<");
    expect(cancel).toBeGreaterThan(-1);
    expect(save).toBeGreaterThan(cancel);
    expect(html.match(/kit-button-primary/g)).toHaveLength(1);
  });

  it("shows a failure as an error notice, never in the success colour", () => {
    const html = render({ repeat: "daily" }, { error: "That time has passed." });
    expect(html).toContain('class="kit-notice kit-notice-error" role="alert"');
    expect(html).toContain("That time has passed.");
    expect(html).not.toContain("kit-notice-success");
  });

  it("asks for a day, beside the days, and will not save a rule that matches none", () => {
    const html = render({ repeat: "custom", days: [] });
    expect(html).toContain('<small class="kit-field-error">Pick at least one day.</small>');
    expect(html).toMatch(/<button type="submit" class="kit-button kit-button-primary kit-button-md" disabled="">/);
  });

  it("shows which days are picked", () => {
    const html = render({ repeat: "custom", days: [0, 4] });
    expect(html.match(/type="checkbox" checked=""/g)).toHaveLength(2);
    expect(html).not.toContain("kit-field-error");
  });

  it("reads Saving… and cannot be sent twice while saving", () => {
    const html = render({ repeat: "daily" }, { busy: true });
    expect(html).toMatch(/disabled=""><span>Saving…<\/span>/);
  });
});
