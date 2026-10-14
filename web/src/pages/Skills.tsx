import { useState } from "react";
import { api, del, post } from "../api";
import { Empty, ErrorNotice, Loading, MarkdownPane, Modal, PageHeader, Status } from "../components";
import { useResource } from "../hooks";
import { useDialog } from "../dialog";
import { Agent } from "./Agents";
import { ContentTextarea, SkillDetail, SkillSummary, ToolSummary } from "./capabilities";

export interface SkillCreateDraft {
  name: string;
  description: string;
  domains: string;
  instructions: string;
  executor: string;
  tools: string[];
  approval: "never" | "on_mutation" | "before_step";
  inputs: string;
  outputs: string;
  successCriteria: string;
}

export const emptySkill = (): SkillCreateDraft => ({
  name: "",
  description: "",
  domains: "general",
  instructions: "",
  executor: "general",
  tools: [],
  approval: "never",
  inputs: "",
  outputs: "result",
  successCriteria:
    "The requested procedure completed and returned verifiable evidence.",
});
export const fieldSchema = (value: string) => {
  const names = value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    type: "object",
    properties: Object.fromEntries(
      names.map((name) => [name, { type: "string" }]),
    ),
    required: names,
    additionalProperties: true,
  };
};

export function Skills() {
  const dialog = useDialog();
  const skills = useResource<SkillSummary[]>("/web/api/skills", 10000);
  const tools = useResource<ToolSummary[]>("/web/api/tools", 10000);
  const agents = useResource<Agent[]>("/orchestrator/agents", 10000);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [creating, setCreating] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [draft, setDraft] = useState<SkillCreateDraft>(emptySkill());
  const [message, setMessage] = useState("");
  const closeEditor = () => {
    setSelected(null);
    setDetailLoading(false);
  };
  const startCreating = () => {
    setSelected(null);
    setDraft(emptySkill());
    setMessage("");
    setCreating(true);
  };
  const open = async (name: string) => {
    setSelected(name);
    setCreating(false);
    setDetailLoading(true);
    setContent("");
    setMessage("");
    try {
      const detail = await api<SkillDetail>(
        `/web/api/skills/${encodeURIComponent(name)}`,
      );
      setContent(detail.content);
    } catch (error) {
      setSelected(null);
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setDetailLoading(false);
    }
  };
  const selectedSkill = skills.data?.find((skill) => skill.name === selected);
  const create = async () => {
    if (
      !draft.name.trim() ||
      !draft.description.trim() ||
      !draft.instructions.trim()
    )
      return;
    try {
      const created = await post<SkillDetail>("/web/api/skills", {
        name: draft.name.trim(),
        description: draft.description.trim(),
        instructions: draft.instructions.trim(),
        domains: draft.domains
          .split(",")
          .map((domain) => domain.trim())
          .filter(Boolean),
        executor: draft.executor,
        tools: draft.tools,
        approval: draft.approval,
        inputs: fieldSchema(draft.inputs),
        outputs: fieldSchema(draft.outputs),
        success_criteria: draft.successCriteria
          .split("\n")
          .map((item) => item.trim())
          .filter(Boolean),
      });
      setDraft(emptySkill());
      await skills.reload();
      await open(created.name);
      setMessage(
        "Skill candidate created. Validate its selection before activation.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const save = async () => {
    if (!selected) return;
    try {
      await api(`/web/api/skills/${selected}`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      setMessage("Skill saved as a candidate. Validate it before activation.");
      await skills.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const remove = async () => {
    if (
      !selected ||
      selectedSkill?.source !== "learned" ||
      !(await dialog.confirm(
        `Delete learned skill '${selected}'? This cannot be undone.`,
        { title: "Delete skill?", confirmLabel: "Delete skill", danger: true },
      ))
    )
      return;
    try {
      await del(`/web/api/skills/${encodeURIComponent(selected)}`);
      setSelected(null);
      setContent("");
      setMessage("Learned skill deleted.");
      await skills.reload();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const canCreate =
    draft.name.trim() &&
    draft.description.trim() &&
    draft.instructions.trim() &&
    draft.executor &&
    draft.successCriteria.trim();
  return (
    <div className="page capability-page">
      <PageHeader
        eyebrow="Procedures"
        title="Skills"
        subtitle="Inspect and edit the playbooks North injects into specialist work."
        actions={
          <button className="primary-button" onClick={startCreating}>
            + New skill
          </button>
        }
      />
      {skills.error && <ErrorNotice message={skills.error} />}{" "}
      {message && <div className="notice">{message}</div>}
      {creating && (
        <section className="capability-create-panel">
          <header>
            <div>
              <span>New procedure</span>
              <h2>Create skill</h2>
              <p>
                Define the procedure and its executable contract once. Flows
                will depend only on this skill.
              </p>
            </div>
            <button className="ghost-button" onClick={() => setCreating(false)}>
              Cancel
            </button>
          </header>
          <div className="tool-create skill-create">
            <div className="skill-create-grid">
              <label>
                Name
                <input
                  autoFocus
                  value={draft.name}
                  placeholder="review-job-match"
                  onChange={(event) =>
                    setDraft({ ...draft, name: event.target.value })
                  }
                />
              </label>
              <label>
                Domains
                <input
                  value={draft.domains}
                  placeholder="general, research"
                  onChange={(event) =>
                    setDraft({ ...draft, domains: event.target.value })
                  }
                />
              </label>
              <label className="skill-create-wide">
                Description
                <input
                  value={draft.description}
                  placeholder="Use when North should review a job match."
                  onChange={(event) =>
                    setDraft({ ...draft, description: event.target.value })
                  }
                />
              </label>
              <label>
                Executor
                <select
                  value={draft.executor}
                  onChange={(event) =>
                    setDraft({ ...draft, executor: event.target.value })
                  }
                >
                  {(agents.data || []).map((agent) => (
                    <option key={agent.name} value={agent.name}>
                      {agent.name} · {agent.domain}
                    </option>
                  ))}
                </select>
                <small>The agent this skill always uses.</small>
              </label>
              <label>
                Minimum approval
                <select
                  value={draft.approval}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      approval: event.target
                        .value as SkillCreateDraft["approval"],
                    })
                  }
                >
                  <option value="never">No mutations</option>
                  <option value="on_mutation">Ask before each mutation</option>
                  <option value="before_step">Ask once before the step</option>
                </select>
              </label>
              <label className="skill-create-wide">
                Allowed tools
                <select
                  value=""
                  onChange={(event) => {
                    const name = event.target.value;
                    if (name && !draft.tools.includes(name))
                      setDraft({ ...draft, tools: [...draft.tools, name] });
                  }}
                >
                  <option value="">Add an atomic tool</option>
                  {(tools.data || [])
                    .filter(
                      (tool) =>
                        tool.status === "active" &&
                        !draft.tools.includes(tool.name),
                    )
                    .map((tool) => (
                      <option key={tool.name} value={tool.name}>
                        {tool.name}
                        {tool.mutating ? " · mutating" : ""}
                      </option>
                    ))}
                </select>
                <div className="contract-chips">
                  {draft.tools.length ? (
                    draft.tools.map((name) => (
                      <button
                        type="button"
                        key={name}
                        onClick={() =>
                          setDraft({
                            ...draft,
                            tools: draft.tools.filter((item) => item !== name),
                          })
                        }
                      >
                        {name} ×
                      </button>
                    ))
                  ) : (
                    <small>No tools. This skill can reason only.</small>
                  )}
                </div>
              </label>
              <label>
                Required input fields
                <input
                  value={draft.inputs}
                  placeholder="resume, job_url"
                  onChange={(event) =>
                    setDraft({ ...draft, inputs: event.target.value })
                  }
                />
                <small>Comma-separated names.</small>
              </label>
              <label>
                Required output fields
                <input
                  value={draft.outputs}
                  placeholder="result, evidence"
                  onChange={(event) =>
                    setDraft({ ...draft, outputs: event.target.value })
                  }
                />
                <small>Comma-separated names.</small>
              </label>
              <label className="skill-create-wide">
                Success criteria
                <textarea
                  rows={3}
                  value={draft.successCriteria}
                  placeholder="One verifiable condition per line."
                  onChange={(event) =>
                    setDraft({ ...draft, successCriteria: event.target.value })
                  }
                />
              </label>
              <label className="skill-create-wide">
                Markdown instructions
                <textarea
                  rows={10}
                  value={draft.instructions}
                  placeholder="Write the ordered procedure North should follow."
                  onChange={(event) =>
                    setDraft({ ...draft, instructions: event.target.value })
                  }
                />
              </label>
            </div>
            <div className="tool-create-footer">
              <small>
                Use a lowercase, hyphenated name. Descriptions start with “Use
                when”.
              </small>
              <button
                className="primary-button"
                disabled={!canCreate}
                onClick={() => void create()}
              >
                Create skill
              </button>
            </div>
          </div>
        </section>
      )}
      <section className="capability-registry">
        <div className="capability-registry-head">
          <span>Skill</span>
          <span>Description</span>
          <span>Definition</span>
          <span>Status</span>
          <span />
        </div>
        <div className="capability-registry-list">
          {skills.loading ? (
            <Loading />
          ) : (
            (skills.data || []).map((skill) => (
              <button
                className="capability-registry-row"
                key={skill.name}
                aria-haspopup="dialog"
                onClick={() => void open(skill.name)}
              >
                <span className="capability-registry-name">
                  <b>{skill.name}</b>
                  <small>{skill.source}</small>
                </span>
                <span className="capability-registry-description">
                  {skill.description}
                </span>
                <span className="capability-registry-meta">
                  {skill.execution
                    ? `${skill.execution.agent} · ${skill.execution.tools.length} tools`
                    : "advisory only"}
                </span>
                <Status value={skill.status} />
                <span className="capability-registry-open">Open</span>
              </button>
            ))
          )}
          {!skills.loading && !skills.data?.length && (
            <Empty>No skills have been created yet.</Empty>
          )}
        </div>
      </section>
      <Modal
        open={Boolean(selected)}
        onClose={closeEditor}
        title={selected || "Skill"}
        eyebrow={selectedSkill?.source || "Procedure"}
        actions={
          selected ? (
            <>
              <button
                className="primary-button"
                disabled={detailLoading}
                onClick={() => void save()}
              >
                Save skill
              </button>
              {selectedSkill?.source === "learned" && (
                <button className="danger-button" onClick={() => void remove()}>
                  Delete skill
                </button>
              )}
            </>
          ) : undefined
        }
      >
        {detailLoading ? (
          <Loading />
        ) : (
          <section className="skill-preview-layout">
            <div className="skill-editor-pane">
              <div className="editor-label">
                <span>{selected}/SKILL.md</span>
                <span>
                  {selectedSkill?.source === "builtin"
                    ? "Built-in baseline · editable override"
                    : "Markdown instructions · editable"}
                </span>
              </div>
              <ContentTextarea
                aria-label={`${selected} Markdown skill instructions`}
                value={content}
                onChange={(event) => setContent(event.target.value)}
              />
            </div>
            <aside className="skill-preview-pane">
              <MarkdownPane label="Rendered Markdown">{content || "Nothing written yet."}</MarkdownPane>
            </aside>
          </section>
        )}
      </Modal>
    </div>
  );
}

