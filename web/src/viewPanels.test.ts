// Custom panels: inputs bound both ways, actions that send their bound values, and the new parts.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { boundValues, readAt, showableImage, ViewSurface, type ViewMessage } from "./views";

const data = { lights: { devices: [{ id: "lamp", name: "Lamp", on: false }] }, state: { level: 40, sound: { hue: 10 } } };

describe("reading and sending values", () => {
  it("reads what you set on the page before the data, at the path or above it", () => {
    expect(readAt(data, {}, "/lights/devices/0/on")).toBe(false);
    expect(readAt(data, { "/lights/devices/0/on": true }, "/lights/devices/0/on")).toBe(true);
    expect(readAt(data, { "/state/sound": { hue: 200 } }, "/state/sound/hue")).toBe(200);
  });

  it("sends each bound argument read where its part is: the row, or the view's state", () => {
    const read = (path: string) => readAt(data, { "/state/level": 75 }, path);
    expect(boundValues({ id: "id", on: "on" }, "/lights/devices/0", read)).toEqual({ id: "lamp", on: false });
    expect(boundValues({ brightness: "/state/level" }, "", read)).toEqual({ brightness: 75 });
    expect(boundValues({ missing: "/state/none" }, "", read)).toEqual({ missing: null });
  });

  it("shows only https, data: images and the dashboard's own pages", () => {
    expect(showableImage("https://example.com/a.png")).toBe(true);
    expect(showableImage("data:image/png;base64,AAAA")).toBe(true);
    expect(showableImage("/web/api/files/a.png")).toBe(true);
    expect(showableImage("http://example.com/a.png")).toBe(false);
    expect(showableImage("javascript:alert(1)")).toBe(false);
    expect(showableImage("//evil.example/a.png")).toBe(false);
  });
});

const panel: ViewMessage[] = [
  { version: "v0.9", createSurface: { surfaceId: "p", catalogId: "north:kit/v1" } },
  {
    version: "v0.9", updateComponents: {
      surfaceId: "p", components: [
        { id: "root", component: "Column", children: ["list", "note", "pick", "level", "hue", "mic", "pic"] },
        { id: "list", component: "Panel", title: "Lights", children: { path: "/lights/devices", componentId: "row" } },
        { id: "row", component: "Switch", label: { path: "name" }, value: { path: "on" }, action: { event: { name: "switch" } } },
        { id: "note", component: "TextField", label: "Note", value: { path: "/state/note" }, placeholder: "Say something" },
        { id: "pick", component: "Select", label: "Scene", value: { path: "/state/scene" }, options: ["Warm", "Cool"] },
        { id: "level", component: "Slider", label: "Brightness", value: { path: "/state/level" }, min: 0, max: 100, unit: "%" },
        { id: "hue", component: "ColorPicker", label: "Colour", value: { path: "/state/hue" } },
        { id: "mic", component: "Microphone", label: "Music", value: { path: "/state/sound" }, action: { event: { name: "music" } } },
        { id: "pic", component: "Image", src: "https://example.com/a.png", alt: "A picture" },
      ],
    },
  },
  { version: "v0.9", updateDataModel: { surfaceId: "p", path: "/", value: { ...data, state: { level: 40, note: "", scene: "Warm", hue: 120, sound: {} } } } },
];

describe("a panel's parts", () => {
  const html = renderToStaticMarkup(createElement(ViewSurface, { messages: panel, onAction: () => undefined }));

  it("draws a switch per row, an input, a select, sliders, a microphone and an image in the kit", () => {
    expect(html).toContain("Lamp");
    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('placeholder="Say something"');
    expect(html).toContain("<option value=\"Cool\">Cool</option>");
    expect(html).toContain("40%");
    expect(html).toContain('class="kit-hue"');
    expect(html).toContain("120°");
    expect(html).toContain("Listen");
    expect(html).toContain('<img class="kit-image" src="https://example.com/a.png" alt="A picture"/>');
  });

  it("disables every part of a draft, which does nothing until it is added", () => {
    const draft = renderToStaticMarkup(createElement(ViewSurface, { messages: panel }));
    expect(draft.match(/disabled=""/g)?.length).toBeGreaterThanOrEqual(6);
  });
});
