// Issue #81: every artifact opened as Markdown text, so a recording or a screenshot was a screen of bytes.

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ArtifactBody, artifactView } from "./Artifacts";
import type { Artifact } from "../types";

const file = (name: string, media_type: string, content = ""): Artifact => ({ id: "a1", name, kind: "task", media_type, content });
const body = (artifact: Artifact) => renderToStaticMarkup(createElement(ArtifactBody, { file: artifact }));

describe("how an artifact is shown", () => {
  it("picks the browser's own player or viewer by media type", () => {
    expect(artifactView("text/markdown")).toBe("text");
    expect(artifactView("application/json")).toBe("text");
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
