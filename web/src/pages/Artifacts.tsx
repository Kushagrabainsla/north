import { useMemo, useState } from "react";
import { api } from "../api";
import { Link } from "react-router-dom";
import { MarkdownPane, Modal, PageHeader, timeAgo } from "../components";
import { CardGrid, EntityCard, Notice, ResourceView } from "../kit";
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

/** Opening a file: fetch its content and show it in the one viewer. */
function useArtifactViewer() {
  const [selected, setSelected] = useState<Artifact | null>(null);
  const [error, setError] = useState("");
  const open = async (file: Artifact) => {
    setError("");
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
    <MarkdownPane>{selected?.content || ""}</MarkdownPane>
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
