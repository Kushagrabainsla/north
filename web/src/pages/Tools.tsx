import { useState } from "react";
import { ViewsAbout } from "./Views";
import { api, del, post } from "../api";
import { Modal, PageHeader } from "../components";
import { useResource } from "../hooks";
import { useDialog } from "../dialog";
import { Button, confirmDelete, EntityForm, EntityTable, Field, Loading, Notice, ResourceView, PaneLabel } from "../kit";
import {
  CapabilityName,
  CapabilityStatus,
  capabilityColumns,
  ContentTextarea,
  failure,
  mcpServer,
  sourceLabel,
  type PageMessage,
  type ToolDetail,
  type ToolSummary,
} from "./capabilities";

const TOOL_COLUMNS = capabilityColumns("Tool", "Operation");

/** An MCP tool: what it does and which server runs it. Its code is the server's, so it is not shown or edited. */
export function McpToolDetail({ tool, server }: { tool: ToolSummary; server: string }) {
  return (
    <section className="mcp-tool-detail">
      <p>{tool.description}</p>
      <p className="muted">
        {tool.mutating ? "Makes changes" : "Read-only"} · provided by the MCP server <b>{server}</b>. Its code runs
        in that server, so north cannot show or edit it here.
      </p>
    </section>
  );
}

export function Tools() {
  const dialog = useDialog();
  const tools = useResource<ToolSummary[]>("/web/api/tools", 10000);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [creating, setCreating] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [message, setMessage] = useState<PageMessage | null>(null);
  const selectedTool = tools.data?.find((tool) => tool.name === selected);
  const server = selectedTool ? mcpServer(selectedTool.source) : "";
  const closeEditor = () => {
    setSelected(null);
    setDetailLoading(false);
  };
  const startCreating = () => {
    setSelected(null);
    setName("");
    setDescription("");
    setMessage(null);
    setCreating(true);
  };
  const open = async (toolName: string) => {
    setSelected(toolName);
    setCreating(false);
    setContent("");
    setMessage(null);
    // An MCP tool's code runs in its server: there is no file to fetch, and asking for one was a 404.
    const tool = tools.data?.find((row) => row.name === toolName);
    if (tool && mcpServer(tool.source)) return;
    setDetailLoading(true);
    try {
      const detail = await api<ToolDetail>(
        `/web/api/tools/${encodeURIComponent(toolName)}`,
      );
      setContent(detail.content);
    } catch (error) {
      setSelected(null);
      setMessage(failure(error));
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
      setMessage({
        tone: "success",
        text: "Tool candidate created. Implement and test it before activation.",
      });
    } catch (error) {
      setMessage(failure(error));
    }
  };
  const save = async () => {
    if (!selected) return;
    try {
      await api(`/web/api/tools/${encodeURIComponent(selected)}`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      setMessage({
        tone: "success",
        text: "Tool saved as a candidate. Validate and test it before activation.",
      });
      await tools.reload();
    } catch (error) {
      setMessage(failure(error));
    }
  };
  const remove = async () => {
    if (
      !selected ||
      selectedTool?.source !== "learned" ||
      !(await confirmDelete(dialog, { noun: "tool", name: selected }))
    )
      return;
    try {
      await del(`/web/api/tools/${encodeURIComponent(selected)}`);
      setSelected(null);
      setContent("");
      setMessage({ tone: "success", text: "Learned tool deleted." });
      await tools.reload();
    } catch (error) {
      setMessage(failure(error));
    }
  };
  return (
    <div className="page capability-page">
      <PageHeader
        title="Tools"
        subtitle="What North's skills can do."
        actions={
          <Button variant="primary" onClick={startCreating}>
            + New tool
          </Button>
        }
      />
      {tools.data && tools.error && <Notice tone="error">{tools.error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {creating && (
        <EntityForm
          title="Create tool"
          label="New capability"
          description="Tools are atomic Python operations called by skills."
          submitLabel="Create tool"
          canSubmit={Boolean(name.trim() && description.trim())}
          onSubmit={create}
          onCancel={() => setCreating(false)}
          hint="North creates a non-runnable candidate stub. Activation requires a successful test."
        >
          <Field label="Name">
            <input
              autoFocus
              value={name}
              placeholder="search_jobs"
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <Field label="Description">
            <input
              value={description}
              placeholder="What should this operation do?"
              onChange={(event) => setDescription(event.target.value)}
            />
          </Field>
        </EntityForm>
      )}
      <ResourceView resource={tools}>
        {(rows) => (
          <EntityTable
            label="Tools"
            columns={TOOL_COLUMNS}
            rows={rows}
            rowKey={(tool) => tool.name}
            onOpen={(tool) => void open(tool.name)}
            empty="No tools are registered yet."
            cell={(tool, key) =>
              key === "name" ? (
                <CapabilityName name={tool.name} source={tool.source} />
              ) : key === "description" ? (
                tool.description
              ) : key === "fact" ? (
                tool.mutating ? "Makes changes" : "Read-only"
              ) : (
                <CapabilityStatus value={tool.status} />
              )
            }
          />
        )}
      </ResourceView>
      <Modal
        open={Boolean(selected)}
        onClose={closeEditor}
        title={selected || "Tool"}
        eyebrow={selectedTool ? sourceLabel(selectedTool.source) : "Capability"}
        actions={
          selected && !server ? (
            <>
              {selectedTool?.source === "learned" && (
                <Button variant="danger" onClick={() => void remove()}>
                  Delete
                </Button>
              )}
              <Button
                variant="primary"
                disabled={detailLoading}
                onClick={() => void save()}
              >
                Save tool
              </Button>
            </>
          ) : undefined
        }
      >
        {selected && <ViewsAbout kind="tool" name={selected}/>}
        {server && selectedTool ? (
          <McpToolDetail tool={selectedTool} server={server}/>
        ) : detailLoading ? (
          <Loading />
        ) : (
          <section className="tool-preview-layout">
            <div className="skill-editor-pane tool-editor">
              <PaneLabel aside={<>Python implementation · editable</>}>{selected}.py</PaneLabel>
              <ContentTextarea
                aria-label={`${selected} Python implementation`}
                value={content}
                onChange={(event) => setContent(event.target.value)}
              />
            </div>
            <aside className="tool-preview-pane">
              <PaneLabel>Python preview</PaneLabel>
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
