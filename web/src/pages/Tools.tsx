import { useState } from "react";
import { api, del, post } from "../api";
import { Empty, ErrorNotice, Loading, PageHeader, Status, Modal } from "../components";
import { useResource } from "../hooks";
import { useDialog } from "../dialog";
import { ContentTextarea, ToolDetail, ToolSummary } from "./capabilities";

export function Tools() {
  const dialog = useDialog();
  const tools = useResource<ToolSummary[]>("/web/api/tools", 10000);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [creating, setCreating] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [message, setMessage] = useState("");
  const selectedTool = tools.data?.find((tool) => tool.name === selected);
  const closeEditor = () => {
    setSelected(null);
    setDetailLoading(false);
  };
  const startCreating = () => {
    setSelected(null);
    setName("");
    setDescription("");
    setMessage("");
    setCreating(true);
  };
  const open = async (toolName: string) => {
    setSelected(toolName);
    setCreating(false);
    setDetailLoading(true);
    setContent("");
    setMessage("");
    try {
      const detail = await api<ToolDetail>(
        `/web/api/tools/${encodeURIComponent(toolName)}`,
      );
      setContent(detail.content);
    } catch (error) {
      setSelected(null);
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setDetailLoading(false);
    }
  };
  const create = async () => {
    if (!name.trim() || !description.trim()) return;
    try {
      const created = await post<ToolSummary>("/web/api/tools", {
        name: name.trim(),
        description: description.trim(),
      });
      setName("");
      setDescription("");
      await tools.reload();
      await open(created.name);
      setMessage(
        "Tool candidate created. Implement and test it before activation.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const save = async () => {
    if (!selected) return;
    try {
      await api(`/web/api/tools/${encodeURIComponent(selected)}`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      setMessage(
        "Tool saved as a candidate. Validate and test it before activation.",
      );
      await tools.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const remove = async () => {
    if (
      !selected ||
      selectedTool?.source !== "learned" ||
      !(await dialog.confirm(
        `Delete learned tool '${selected}'? This cannot be undone.`,
        { title: "Delete tool?", confirmLabel: "Delete tool", danger: true },
      ))
    )
      return;
    try {
      await del(`/web/api/tools/${encodeURIComponent(selected)}`);
      setSelected(null);
      setContent("");
      setMessage("Learned tool deleted.");
      await tools.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  return (
    <div className="page capability-page">
      <PageHeader
        eyebrow="Capabilities"
        title="Tools"
        subtitle="Inspect the atomic operations North's skills can call, and manage your learned tools."
        actions={
          <button className="primary-button" onClick={startCreating}>
            + New tool
          </button>
        }
      />
      {tools.error && <ErrorNotice message={tools.error} />}{" "}
      {message && <div className="notice">{message}</div>}
      {creating && (
        <section className="capability-create-panel">
          <header>
            <div>
              <span>New capability</span>
              <h2>Create tool</h2>
              <p>Tools are atomic Python operations called by skills.</p>
            </div>
            <button className="ghost-button" onClick={() => setCreating(false)}>
              Cancel
            </button>
          </header>
          <div className="tool-create">
            <div className="tool-create-grid">
              <label>
                Name
                <input
                  autoFocus
                  value={name}
                  placeholder="search_jobs"
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <label>
                Description
                <input
                  value={description}
                  placeholder="What should this operation do?"
                  onChange={(event) => setDescription(event.target.value)}
                />
              </label>
            </div>
            <div className="tool-create-footer">
              <small>
                North creates a non-runnable candidate stub. Activation requires
                a successful test.
              </small>
              <button
                className="primary-button"
                disabled={!name.trim() || !description.trim()}
                onClick={() => void create()}
              >
                Create tool
              </button>
            </div>
          </div>
        </section>
      )}
      <section className="capability-registry">
        <div className="capability-registry-head">
          <span>Tool</span>
          <span>Description</span>
          <span>Operation</span>
          <span>Status</span>
          <span />
        </div>
        <div className="capability-registry-list">
          {tools.loading ? (
            <Loading />
          ) : (
            (tools.data || []).map((tool) => (
              <button
                className="capability-registry-row"
                key={tool.name}
                aria-haspopup="dialog"
                onClick={() => void open(tool.name)}
              >
                <span className="capability-registry-name">
                  <b>{tool.name}</b>
                  <small>{tool.source}</small>
                </span>
                <span className="capability-registry-description">
                  {tool.description}
                </span>
                <span className="capability-registry-meta">
                  {tool.mutating ? "Mutating" : "Read-only"}
                </span>
                <Status value={tool.status} />
                <span className="capability-registry-open">Open</span>
              </button>
            ))
          )}
          {!tools.loading && !tools.data?.length && (
            <Empty>No tools are registered yet.</Empty>
          )}
        </div>
      </section>
      <Modal
        open={Boolean(selected)}
        onClose={closeEditor}
        title={selected || "Tool"}
        eyebrow={selectedTool?.source || "Capability"}
        actions={
          selected ? (
            <>
              <button
                className="primary-button"
                disabled={detailLoading}
                onClick={() => void save()}
              >
                Save tool
              </button>
              {selectedTool?.source === "learned" && (
                <button className="danger-button" onClick={() => void remove()}>
                  Delete tool
                </button>
              )}
            </>
          ) : undefined
        }
      >
        {detailLoading ? (
          <Loading />
        ) : (
          <section className="tool-preview-layout">
            <div className="skill-editor-pane tool-editor">
              <div className="editor-label">
                <span>{selected}.py</span>
                <span>Python implementation · editable</span>
              </div>
              <ContentTextarea
                aria-label={`${selected} Python implementation`}
                value={content}
                onChange={(event) => setContent(event.target.value)}
              />
            </div>
            <aside className="tool-preview-pane">
              <div className="editor-label">Python preview</div>
              <pre>
                <code>{content || "# Nothing implemented yet."}</code>
              </pre>
            </aside>
          </section>
        )}
      </Modal>
    </div>
  );
}

