import { useState } from "react";
import { api, del, post } from "../api";
import { MarkdownPane, Modal, PageHeader } from "../components";
import { useResource } from "../hooks";
import { useDialog } from "../dialog";
import { ActionBar, Button, confirmDelete, EntityForm, EntityTable, Field, Loading, Notice, ResourceView, Status, PaneLabel } from "../kit";
import {
  CapabilityName,
  capabilityColumns,
  ContentTextarea,
  failure,
  skillRunsAs,
  sourceLabel,
  type PageMessage,
  type SkillDetail,
  type SkillSummary,
  type ToolSummary,
} from "./capabilities";

export interface SkillCreateDraft {
  name: string;
  description: string;
  domains: string;
  instructions: string;
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

const SKILL_COLUMNS = capabilityColumns("Skill", "Runs as");

export function Skills() {
  const dialog = useDialog();
  const skills = useResource<SkillSummary[]>("/web/api/skills", 10000);
  const tools = useResource<ToolSummary[]>("/web/api/tools", 10000);
  const [selected, setSelected] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [creating, setCreating] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [draft, setDraft] = useState<SkillCreateDraft>(emptySkill());
  const [message, setMessage] = useState<PageMessage | null>(null);
  const closeEditor = () => {
    setSelected(null);
    setDetailLoading(false);
  };
  const startCreating = () => {
    setSelected(null);
    setDraft(emptySkill());
    setMessage(null);
    setCreating(true);
  };
  const open = async (name: string) => {
    setSelected(name);
    setCreating(false);
    setDetailLoading(true);
    setContent("");
    setMessage(null);
    try {
      const detail = await api<SkillDetail>(
        `/web/api/skills/${encodeURIComponent(name)}`,
      );
      setContent(detail.content);
    } catch (error) {
      setSelected(null);
      setMessage(failure(error));
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
      setMessage({
        tone: "success",
        text: "Skill candidate created. Validate its selection before activation.",
      });
    } catch (error) {
      setMessage(failure(error));
    }
  };
  const save = async () => {
    if (!selected) return;
    try {
      await api(`/web/api/skills/${selected}`, {
        method: "PUT",
        body: JSON.stringify({ content }),
      });
      setMessage({
        tone: "success",
        text: "Skill saved as a candidate. Validate it before activation.",
      });
      await skills.reload();
    } catch (error) {
      setMessage(failure(error));
    }
  };
  const remove = async () => {
    if (
      !selected ||
      selectedSkill?.source !== "learned" ||
      !(await confirmDelete(dialog, { noun: "skill", name: selected }))
    )
      return;
    try {
      await del(`/web/api/skills/${encodeURIComponent(selected)}`);
      setSelected(null);
      setContent("");
      setMessage({ tone: "success", text: "Learned skill deleted." });
      await skills.reload();
    } catch (error) {
      setMessage(failure(error));
    }
  };
  const canCreate = Boolean(
    draft.name.trim() &&
      draft.description.trim() &&
      draft.instructions.trim() &&
      draft.successCriteria.trim(),
  );
  const addableTools = (tools.data || []).filter(
    (tool) => tool.status === "active" && !draft.tools.includes(tool.name),
  );
  return (
    <div className="page capability-page">
      <PageHeader
        title="Skills"
        subtitle="Inspect and edit the playbooks North follows."
        actions={
          <Button variant="primary" onClick={startCreating}>
            + New skill
          </Button>
        }
      />
      {skills.data && skills.error && <Notice tone="error">{skills.error}</Notice>}
      {message && <Notice tone={message.tone}>{message.text}</Notice>}
      {creating && (
        <EntityForm
          title="Create skill"
          label="New procedure"
          description="Define the procedure and its executable contract once. Flows will depend only on this skill."
          submitLabel="Create skill"
          canSubmit={canCreate}
          onSubmit={create}
          onCancel={() => setCreating(false)}
          hint="Use a lowercase, hyphenated name. Descriptions start with “Use when”."
        >
          <Field label="Name">
            <input
              autoFocus
              value={draft.name}
              placeholder="review-job-match"
              onChange={(event) =>
                setDraft({ ...draft, name: event.target.value })
              }
            />
          </Field>
          <Field label="Domains">
            <input
              value={draft.domains}
              placeholder="general, research"
              onChange={(event) =>
                setDraft({ ...draft, domains: event.target.value })
              }
            />
          </Field>
          <Field label="Description" wide>
            <input
              value={draft.description}
              placeholder="Use when North should review a job match."
              onChange={(event) =>
                setDraft({ ...draft, description: event.target.value })
              }
            />
          </Field>
          <Field label="Minimum approval">
            <select
              value={draft.approval}
              onChange={(event) =>
                setDraft({
                  ...draft,
                  approval: event.target.value as SkillCreateDraft["approval"],
                })
              }
            >
              <option value="never">No mutations</option>
              <option value="on_mutation">Ask before each mutation</option>
              <option value="before_step">Ask once before the step</option>
            </select>
          </Field>
          <Field
            label="Allowed tools"
            wide
            hint={
              draft.tools.length
                ? "Click a tool to take it off the list."
                : "No tools. This skill can reason only."
            }
          >
            <select
              value=""
              aria-label="Add a tool"
              onChange={(event) => {
                const name = event.target.value;
                if (name && !draft.tools.includes(name))
                  setDraft({ ...draft, tools: [...draft.tools, name] });
              }}
            >
              <option value="">Add an atomic tool</option>
              {addableTools.map((tool) => (
                <option key={tool.name} value={tool.name}>
                  {tool.name}
                  {tool.mutating ? " · makes changes" : ""}
                </option>
              ))}
            </select>
            {draft.tools.length > 0 && (
              <ActionBar align="start">
                {draft.tools.map((name) => (
                  <Button
                    key={name}
                    size="sm"
                    aria-label={`Remove ${name}`}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        tools: draft.tools.filter((item) => item !== name),
                      })
                    }
                  >
                    {name} ×
                  </Button>
                ))}
              </ActionBar>
            )}
          </Field>
          <Field label="Required input fields" hint="Comma-separated names.">
            <input
              value={draft.inputs}
              placeholder="resume, job_url"
              onChange={(event) =>
                setDraft({ ...draft, inputs: event.target.value })
              }
            />
          </Field>
          <Field label="Required output fields" hint="Comma-separated names.">
            <input
              value={draft.outputs}
              placeholder="result, evidence"
              onChange={(event) =>
                setDraft({ ...draft, outputs: event.target.value })
              }
            />
          </Field>
          <Field label="Success criteria" wide>
            <textarea
              rows={3}
              value={draft.successCriteria}
              placeholder="One verifiable condition per line."
              onChange={(event) =>
                setDraft({ ...draft, successCriteria: event.target.value })
              }
            />
          </Field>
          <Field label="Markdown instructions" wide>
            <textarea
              rows={10}
              value={draft.instructions}
              placeholder="Write the ordered procedure North should follow."
              onChange={(event) =>
                setDraft({ ...draft, instructions: event.target.value })
              }
            />
          </Field>
        </EntityForm>
      )}
      <ResourceView resource={skills}>
        {(rows) => (
          <EntityTable
            label="Skills"
            columns={SKILL_COLUMNS}
            rows={rows}
            rowKey={(skill) => skill.name}
            onOpen={(skill) => void open(skill.name)}
            empty="No skills have been created yet."
            cell={(skill, key) =>
              key === "name" ? (
                <CapabilityName name={skill.name} source={skill.source} />
              ) : key === "description" ? (
                skill.description
              ) : key === "fact" ? (
                skillRunsAs(skill.execution)
              ) : (
                <Status value={skill.status} />
              )
            }
          />
        )}
      </ResourceView>
      <Modal
        open={Boolean(selected)}
        onClose={closeEditor}
        title={selected || "Skill"}
        eyebrow={selectedSkill ? sourceLabel(selectedSkill.source) : "Procedure"}
        actions={
          selected ? (
            <>
              {selectedSkill?.source === "learned" && (
                <Button variant="danger" onClick={() => void remove()}>
                  Delete
                </Button>
              )}
              <Button
                variant="primary"
                disabled={detailLoading}
                onClick={() => void save()}
              >
                Save skill
              </Button>
            </>
          ) : undefined
        }
      >
        {detailLoading ? (
          <Loading />
        ) : (
          <section className="skill-preview-layout">
            <div className="skill-editor-pane">
              <PaneLabel aside={<>
                  {selectedSkill?.source === "builtin"
                    ? "Built-in baseline · editable override"
                    : "Markdown instructions · editable"}
                </>}>{selected}/SKILL.md</PaneLabel>
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
