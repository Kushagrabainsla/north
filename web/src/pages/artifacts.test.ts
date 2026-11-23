// Issue #81: every artifact opened as Markdown text, so a recording or a screenshot was a screen of bytes.

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ArtifactBody, artifactView, extensionOf, fileManagerName, readable } from "./Artifacts";
import type { Artifact } from "../types";

const file = (name: string, media_type: string, content = ""): Artifact => ({ id: "a1", name, kind: "task", media_type, content });
const body = (artifact: Artifact) => renderToStaticMarkup(createElement(ArtifactBody, { file: artifact }));

describe("how an artifact is shown", () => {
  it("picks the browser's own player or viewer by media type", () => {
    expect(artifactView("text/markdown")).toBe("markdown");
    expect(artifactView("application/json")).toBe("code");
    expect(artifactView("text/html")).toBe("page");
    expect(artifactView("video/quicktime")).toBe("video");
    expect(artifactView("video/mp4")).toBe("video");
    expect(artifactView("audio/x-m4a")).toBe("audio");
    expect(artifactView("image/png")).toBe("image");
    expect(artifactView("application/pdf")).toBe("pdf");
    expect(artifactView("application/zip")).toBe("file");
  });

  it("plays a recording from its own address, with a way to download it", () => {
    const html = body(file("camera-5s.mov", "video/quicktime"));
    expect(html).toMatch(/<video class="artifact-media" src="\/web\/api\/artifacts\/a1\/file" controls=""/);
    expect(html).toContain('download="camera-5s.mov"');
  });

  it("shows a screenshot as a picture and a briefing as text", () => {
    expect(body(file("s1.png", "image/png"))).toMatch(/<img class="artifact-media" src="\/web\/api\/artifacts\/a1\/file" alt="s1.png"/);
    const briefing = body(file("2026-11-16.md", "text/markdown", "# Today"));
    expect(briefing).toContain("Today");
    expect(briefing).not.toContain("/file");
  });

  it("says when a file cannot be shown, and still offers it", () => {
    const html = body(file("bundle.zip", "application/zip"));
    expect(html).toContain("cannot be shown here");
    expect(html).toContain('href="/web/api/artifacts/a1/file"');
  });
});

// 2026-11-22: every text file went through the Markdown pane, so an HTML page and a JSON file
// showed as raw text. Each kind now has its view, and any other text type is colored text.
describe("a text file is shown as what it is", () => {
  it("shows an HTML page in a frame, from its own address, which the server seals", () => {
    // The seal is the file route's Content-Security-Policy (tests/unit/web/test_artifacts_api.py).
    const html = body(file("photo-flashcards.html", "text/html"));
    expect(html).toMatch(/<iframe class="artifact-media artifact-page" src="\/web\/api\/artifacts\/a1\/file"/);
    expect(html).not.toContain("allow-same-origin");
  });

  it("shows JSON indented, and any text as text, never as Markdown or markup", () => {
    expect(readable('{"cards":[{"q":1}]}', "json")).toBe('{\n  "cards": [\n    {\n      "q": 1\n    }\n  ]\n}');
    expect(readable("{not json", "json")).toBe("{not json");
    const shown = body(file("deck.json", "application/json", '{"title":"<b>Deck</b>"}'));
    expect(shown).toContain('<pre class="artifact-code hljs">');
    expect(shown).toContain("&lt;b&gt;Deck&lt;/b&gt;");
  });

  it("names the language of a file by its extension", () => {
    expect(extensionOf("block4-function-approximation.txt")).toBe("txt");
    expect(extensionOf("config.YAML")).toBe("yaml");
    expect(extensionOf("Makefile")).toBe("");
  });

  it("names the file manager the way this computer does", () => {
    expect(fileManagerName("MacIntel")).toBe("Finder");
    expect(fileManagerName("macOS")).toBe("Finder");
    expect(fileManagerName("Win32")).toBe("Explorer");
    expect(fileManagerName("Linux x86_64")).toBe("folder");
  });
});
