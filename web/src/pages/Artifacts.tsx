import { useMemo, useState } from "react";
import { api } from "../api";
import { Link } from "react-router-dom";
import { Empty, ErrorNotice, Loading, Markdown, Modal, PageHeader, timeAgo } from "../components";
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

/** Every file grouped under the flow, session or task that made it, newest first. */
export function ArtifactLibrary() {
  const resource = useResource<Artifact[]>("/web/api/artifacts", 10000);
  const [selected, setSelected] = useState<Artifact | null>(null);
  const [error, setError] = useState("");
  const files = resource.data || [];
  const open = async (file: Artifact) => {
    try {
      setSelected({ ...(await api<Artifact>(`/web/api/artifacts/${file.id}`)), owner: file.owner });
    } catch (err) {
      setError(String(err));
    }
  };
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
      // Files from before every output belonged to a task go last, as one group.
      .sort((a, b) => Number(!a.task) - Number(!b.task) || b.updated - a.updated);
  }, [files]);
  const card = (file: Artifact) => (
    <button className="artifact-card" key={file.id} onClick={() => open(file)}>
      <span>{stageIcon[file.kind] || "◇"}</span>
      <div>
        <b>{file.name}</b>
        <small>
          {file.kind} · {file.size ? `${Math.ceil(file.size / 1024)} KB` : ""} · {timeAgo(file.updated_at)}
        </small>
      </div>
    </button>
  );
  if (resource.loading) return <Loading />;
  return (
    <>
      {(resource.error || error) && <ErrorNotice message={resource.error || error} />}
      {groups.map((group) => (
        <section className="artifact-run" key={group.task || "earlier"}>
          <h3>
            {group.task && group.owner ? (
              <>
                <span className="artifact-owner-kind">{OWNER_KIND[group.owner.kind]}</span>
                <Link to={group.owner.link}>{group.owner.name}</Link>
              </>
            ) : (
              "Earlier files"
            )}
            <small>{timeAgo(group.updated)}</small>
          </h3>
          <div className="artifact-grid">{group.items.map(card)}</div>
        </section>
      ))}
      {!files.length && <Empty>No files have been generated yet.</Empty>}
      <Modal
        open={!!selected}
        eyebrow={selected?.owner ? `${OWNER_KIND[selected.owner.kind]} · ${selected.owner.name}` : selected?.kind || ""}
        title={selected?.name || ""}
        onClose={() => setSelected(null)}
      >
        <Markdown>{selected?.content || ""}</Markdown>
      </Modal>
    </>
  );
}

export function Artifacts() {
  return (
    <div className="page">
      <PageHeader
        eyebrow="Outputs"
        title="Artifacts"
        subtitle="Every report, briefing, note and plan North has produced, under the flow, session or task that made it."
      />
      <ArtifactLibrary />
    </div>
  );
}
