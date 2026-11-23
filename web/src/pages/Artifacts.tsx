import { useEffect, useMemo, useState } from "react";
import "highlight.js/styles/github-dark.css";
import { api, post } from "../api";
import { Link } from "react-router-dom";
import { MarkdownPane, Modal, PageHeader, timeAgo } from "../components";
import { Button, CardGrid, Empty, EntityCard, Notice, ResourceView } from "../kit";
import { useResource } from "../hooks";
import type { Artifact } from "../types";

// The pipeline stages, in the order they run - so a task's artifacts read as the
// story of that run rather than in whatever order the filesystem returned them.
export const stageOrder = ["research", "architecture", "implementation", "qa"];
export const stageIcon: Record<string, string> = {
  news: "☼",
  notes: "✎",
  wellness: "♥",
  research: "◇",
  architecture: "▣",
  implementation: "▸",
  qa: "✓",
};

const OWNER_KIND: Record<string, string> = { flow: "Flow", session: "Session", task: "Task" };

/** The line under a file's name: its kind, its size when known, and when it changed. */
export function artifactMeta(file: Artifact): string {
  return [file.kind, file.size ? `${Math.ceil(file.size / 1024)} KB` : "", timeAgo(file.updated_at)].filter(Boolean).join(" · ");
}

/** How a file is shown: as a page, as Markdown, as colored text, by the browser's own player or viewer,
 *  or not at all (only a download). */
export type ArtifactView = "page" | "markdown" | "code" | "image" | "audio" | "video" | "pdf" | "file";

const TEXT_APPLICATION_TYPES = new Set(["application/json", "application/xml", "application/x-yaml", "application/yaml"]);
const PAGE_TYPES = new Set(["text/html", "application/xhtml+xml"]);

/** The view for a file. Every text type the viewer does not know is colored text, so a new kind of file
 *  needs no new code: before, every text file went through the Markdown pane, and an HTML page or a JSON
 *  file showed as raw text. Shown as text, a 5-second recording was a screen of bytes (issue #81). */
export function artifactView(mediaType: string): ArtifactView {
  if (PAGE_TYPES.has(mediaType)) return "page";
  if (mediaType === "text/markdown") return "markdown";
  if (mediaType.startsWith("text/") || TEXT_APPLICATION_TYPES.has(mediaType)) return "code";
  for (const view of ["image", "audio", "video"] as const) if (mediaType.startsWith(`${view}/`)) return view;
  return mediaType === "application/pdf" ? "pdf" : "file";
}

/** Whether the viewer needs the file's text, which only /artifacts/{id} returns. */
const needsText = (view: ArtifactView) => view === "markdown" || view === "code";

/** The file's extension, which names its language for coloring ("" when it has none). */
export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** JSON indented, so a one-line file reads as a structure; anything that does not parse stays as it is. */
export function readable(text: string, extension: string): string {
  if (extension !== "json") return text;
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}

// Larger text is shown plain: coloring two megabytes would hold the page for seconds.
const COLOR_LIMIT_CHARS = 300_000;

/** Text colored by its language, plain until the colorer loads. highlight.js names the language from the
 *  extension when it knows it, and detects it from the text when it does not, so a new kind of file needs
 *  nothing. It is one file loaded when a file opens: Shiki, tried first, made the built dashboard 11 MB
 *  in 314 files, one per language and theme. */
function CodeView({ name, text }: { name: string; text: string }) {
  const extension = extensionOf(name);
  const shown = readable(text, extension);
  const [colored, setColored] = useState("");
  useEffect(() => {
    if (shown.length > COLOR_LIMIT_CHARS) return;
    let current = true;
    import("highlight.js/lib/common")
      .then(({ default: hljs }) => {
        const result = extension && hljs.getLanguage(extension)
          ? hljs.highlight(shown, { language: extension, ignoreIllegals: true })
          : hljs.highlightAuto(shown);
        if (current) setColored(result.value);
      })
      .catch(() => undefined);  // plain text stays: coloring is a help, not the content
    return () => { current = false; };
  }, [shown, extension]);
  // highlight.js escapes the text it colors; the file's own markup never becomes part of the page.
  return <pre className="artifact-code hljs">{colored
    ? <code dangerouslySetInnerHTML={{ __html: colored }}/>
    : <code>{shown}</code>}</pre>;
}

/** Where the file itself is served, with its media type. */
export const artifactFileUrl = (file: Artifact) => `/web/api/artifacts/${file.id}/file`;

/** The file manager's name on this computer. The dashboard is served to this computer only (§12.6),
 *  so the browser's platform is north's. */
export function fileManagerName(platform: string): string {
  if (/mac/i.test(platform)) return "Finder";
  if (/win/i.test(platform)) return "Explorer";
  return "folder";
}

const thisPlatform = () => {
  const nav = typeof navigator === "undefined" ? undefined : navigator as Navigator & { userAgentData?: { platform?: string } };
  return nav?.userAgentData?.platform || nav?.platform || "";
};

/** A video file, or a sound bar when the file holds only sound: north records audio into `.mov`,
 *  which showed an empty black picture box above the controls. */
function Recording({ url }: { url: string }) {
  const [soundOnly, setSoundOnly] = useState(false);
  return <video className={`artifact-media${soundOnly ? " artifact-sound-only" : ""}`} src={url} controls preload="metadata"
    onLoadedMetadata={event => setSoundOnly(event.currentTarget.videoWidth === 0)}/>;
}

/** The file as the browser shows it; text goes through the shared Markdown pane. */
export function ArtifactBody({ file }: { file: Artifact }) {
  const url = artifactFileUrl(file);
  const view = artifactView(file.media_type);
  const download = <p className="artifact-download"><a href={url} download={file.name}>Download {file.name}</a></p>;
  if (view === "markdown") return <MarkdownPane>{file.content || ""}</MarkdownPane>;
  if (view === "code") return <CodeView name={file.name} text={file.content || ""}/>;
  // A page, sealed by the file route's own `Content-Security-Policy: sandbox`: its scripts run, with no
  // access to north. The seal is on the file, not this frame, so it holds when the file is opened alone;
  // a `sandbox` attribute here was refused outright by some browsers, which showed an empty frame.
  if (view === "page") return <iframe className="artifact-media artifact-page" src={url} title={file.name}/>;
  if (view === "image") return <><img className="artifact-media" src={url} alt={file.name}/>{download}</>;
  if (view === "video") return <><Recording url={url}/>{download}</>;
  if (view === "audio") return <><audio className="artifact-media" src={url} controls preload="metadata"/>{download}</>;
  if (view === "pdf") return <><iframe className="artifact-media artifact-pdf" src={url} title={file.name}/>{download}</>;
  return <><Empty>This kind of file ({file.media_type}) cannot be shown here.</Empty>{download}</>;
}

/** Opening a file: a text file's content is fetched; any other file is shown from its own address. */
function useArtifactViewer() {
  const [selected, setSelected] = useState<Artifact | null>(null);
  const [error, setError] = useState("");
  const [shown, setShown] = useState("");
  const open = async (file: Artifact) => {
    setError("");
    setShown("");
    if (!needsText(artifactView(file.media_type))) { setSelected(file); return; }
    try {
      setSelected({ ...(await api<Artifact>(`/web/api/artifacts/${file.id}`)), owner: file.owner });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  /** North shows the file in the file manager; it never opens it (CODING_STYLE §5.1). */
  const showInFileManager = async (file: Artifact) => {
    try {
      await post(`/web/api/artifacts/${file.id}/reveal`, {});
      setShown("ok");
    } catch (err) {
      setShown(err instanceof Error ? err.message : String(err));
    }
  };
  const viewer = <Modal
    open={!!selected}
    eyebrow={selected?.owner ? `${OWNER_KIND[selected.owner.kind]} · ${selected.owner.name}` : selected?.kind || ""}
    title={selected?.name || ""}
    onClose={() => setSelected(null)}
    actions={selected && <Button onClick={() => void showInFileManager(selected)}>Show in {fileManagerName(thisPlatform())}</Button>}
  >
    {shown && <Notice tone={shown === "ok" ? "success" : "error"}>{shown === "ok" ? `Shown in ${fileManagerName(thisPlatform())}.` : shown}</Notice>}
    {selected && <ArtifactBody file={selected}/>}
  </Modal>;
  return { open, error, viewer };
}

/** One file as a card that opens it. Every page that lists files uses this one. */
export function ArtifactCard({ file, onOpen }: { file: Artifact; onOpen: (file: Artifact) => void }) {
  return <EntityCard icon={stageIcon[file.kind] || "◇"} title={file.name} meta={artifactMeta(file)} onOpen={() => onOpen(file)}/>;
}

/** Files as cards, each opening in the viewer: a task's outputs on its own page. */
export function ArtifactGrid({ files }: { files: Artifact[] }) {
  const { open, error, viewer } = useArtifactViewer();
  return <>
    {error && <Notice tone="error">{error}</Notice>}
    <CardGrid>{files.map(file => <ArtifactCard key={file.id} file={file} onOpen={open}/>)}</CardGrid>
    {viewer}
  </>;
}

/** Every file grouped under the flow, session or task that made it, newest first. */
export function ArtifactLibrary() {
  const resource = useResource<Artifact[]>("/web/api/artifacts", 10000);
  const { open, error, viewer } = useArtifactViewer();
  const files = resource.data || [];
  const groups = useMemo(() => {
    const grouped = new Map<string, Artifact[]>();
    for (const file of files) grouped.set(file.task || "", [...(grouped.get(file.task || "") || []), file]);
    return [...grouped]
      .map(([task, items]) => ({
        task,
        owner: items[0].owner,
        items: [...items].sort(
          (a, b) => stageOrder.indexOf(a.kind) - stageOrder.indexOf(b.kind) || a.name.localeCompare(b.name),
        ),
        updated: Math.max(...items.map((item) => item.updated_at || 0)),
      }))
      .sort((a, b) => b.updated - a.updated);
  }, [files]);
  return (
    <>
      {resource.error && resource.data && <Notice tone="error">{resource.error}</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
      <ResourceView resource={resource} isEmpty={data => !data.length} empty="No files have been generated yet.">
        {() => groups.map((group) => (
          <section className="artifact-run" key={group.task}>
            <h3>
              {group.owner && (
                <>
                  <span className="artifact-owner-kind">{OWNER_KIND[group.owner.kind]}</span>
                  <Link to={group.owner.link}>{group.owner.name}</Link>
                </>
              )}
              <small>{timeAgo(group.updated)}</small>
            </h3>
            <CardGrid>{group.items.map(file => <ArtifactCard key={file.id} file={file} onOpen={open}/>)}</CardGrid>
          </section>
        ))}
      </ResourceView>
      {viewer}
    </>
  );
}

export function Artifacts() {
  return (
    <div className="page">
      <PageHeader
        title="Artifacts"
        subtitle="Everything North has made."
      />
      <ArtifactLibrary />
    </div>
  );
}
