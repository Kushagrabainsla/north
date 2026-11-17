import { useMemo, useState } from "react";
import { api } from "../api";
import { Link } from "react-router-dom";
import { MarkdownPane, Modal, PageHeader, timeAgo } from "../components";
import { CardGrid, Empty, EntityCard, Notice, ResourceView } from "../kit";
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

/** How a file is shown: as text, by the browser's own player or viewer, or not at all (only a download). */
export type ArtifactView = "text" | "image" | "audio" | "video" | "pdf" | "file";

const TEXT_APPLICATION_TYPES = new Set(["application/json", "application/xml", "application/x-yaml", "application/yaml"]);

/** The view for a media type. Shown as text, a 5-second recording was a screen of bytes (issue #81). */
export function artifactView(mediaType: string): ArtifactView {
  if (mediaType.startsWith("text/") || TEXT_APPLICATION_TYPES.has(mediaType)) return "text";
  for (const view of ["image", "audio", "video"] as const) if (mediaType.startsWith(`${view}/`)) return view;
  return mediaType === "application/pdf" ? "pdf" : "file";
}

/** Where the file itself is served, with its media type. */
export const artifactFileUrl = (file: Artifact) => `/web/api/artifacts/${file.id}/file`;

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
  if (view === "text") return <MarkdownPane>{file.content || ""}</MarkdownPane>;
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
  const open = async (file: Artifact) => {
    setError("");
    if (artifactView(file.media_type) !== "text") { setSelected(file); return; }
    try {
      setSelected({ ...(await api<Artifact>(`/web/api/artifacts/${file.id}`)), owner: file.owner });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  const viewer = <Modal
    open={!!selected}
    eyebrow={selected?.owner ? `${OWNER_KIND[selected.owner.kind]} · ${selected.owner.name}` : selected?.kind || ""}
    title={selected?.name || ""}
    onClose={() => setSelected(null)}
  >
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
