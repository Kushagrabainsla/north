import { useEffect, useState } from "react";
import { api, post } from "../api";
import { Empty, Loading, Markdown, PageHeader, Panel, Status, timeAgo, reveal } from "../components";
import { UI_PREFERENCE_KEYS, usePersistentState, useResource } from "../hooks";
import { useDialog } from "../dialog";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

export const docs = ["user.md", "north_stars.md", "judgement_rules.md", "soul.md"];
export const docLabels: Record<string, string> = {
  "user.md": "User facts",
  "north_stars.md": "North stars",
  "judgement_rules.md": "Judgement rules",
  "soul.md": "Soul",
};
export interface ContextDoc {
  document: string;
  content: string;
}
// Everything north has learned, in one place. Four kinds of memory that lived in
// four different states of visibility: the documents were editable here, the
// facts were listed below them, and the episodes and approval decisions had no
// endpoint at all - north was learning things nobody could look at.
export type MemoryTab = "documents" | "facts" | "episodes";

export const MEMORY_TABS: [MemoryTab, string][] = [
  ["documents", "Documents"],
  ["facts", "Facts"],
  ["episodes", "Episodes"],
];

export interface Episode {
  id: string;
  task_id: string;
  domain: string;
  outcome: string;
  summary: string;
  timestamp: string;
}
// A record of what happened, so read-only: the honest way to change an episode
// is to do the thing differently, not to edit the note.
export function EpisodesPanel() {
  const resource = useResource<Episode[]>("/web/api/memory/episodes", 10000);
  const rows = resource.data || [];
  return (
    <Panel title="Episodes" label={`${rows.length} remembered`}>
      <p className="muted memory-note">
        What north remembers of past tasks, used to recognise a situation it has
        been in before.
      </p>
      {resource.loading ? (
        <Loading />
      ) : rows.length ? (
        rows.map((row) => (
          <div className="memory-row" key={row.id}>
            <div className="memory-main">
              <b>{row.summary}</b>
              <small>
                {row.domain} · {row.task_id} · {timeAgo(row.timestamp)}
              </small>
            </div>
            <Status value={row.outcome} />
          </div>
        ))
      ) : (
        <Empty>No episodes recorded yet.</Empty>
      )}
    </Panel>
  );
}

export function Memory() {
  const dialog = useDialog();
  const [tab, setTab] = usePersistentState<MemoryTab>(
    UI_PREFERENCE_KEYS.memoryTab,
    "documents",
    (value) => MEMORY_TABS.some(([name]) => name === value),
  );
  const [doc, setDoc] = usePersistentState(
    UI_PREFERENCE_KEYS.memoryDocument,
    docs[0],
    (value) => typeof value === "string" && docs.includes(value),
  );
  // A link from a decision opens the tab (and document) holding the memory it
  // used, and marks the row.
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const find = params.get("find") || "";
  useEffect(() => {
    const wantedTab = params.get("tab");
    // Approval decisions and safe actions moved to the Approvals page.
    const moved = wantedTab === "approvals" ? "decisions" : wantedTab === "rules" ? "rules" : "";
    if (moved) {
      navigate(`/approvals?tab=${moved}${find ? `&find=${encodeURIComponent(find)}` : ""}`, { replace: true });
      return;
    }
    if (wantedTab && MEMORY_TABS.some(([name]) => name === wantedTab))
      setTab(wantedTab as MemoryTab);
    const wantedDoc = params.get("doc");
    if (wantedDoc && docs.includes(wantedDoc)) setDoc(wantedDoc);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);
  const resource = useResource<ContextDoc>(`/orchestrator/context/${doc}`);
  const facts = useResource<any[]>("/web/api/memory/facts", 10000);
  const [draft, setDraft] = useState<string | null>(null);
  const [factDraft, setFactDraft] = useState("");
  const [editingFact, setEditingFact] = useState<string | null>(null);
  const isUserFacts = doc === "user.md";
  const content = isUserFacts ? "" : (draft ?? resource.data?.content ?? "");
  const save = async () => {
    await api(`/orchestrator/context/${doc}`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
    setDraft(null);
    await resource.reload();
  };
  const removeDocument = async () => {
    if (
      !(await dialog.confirm(
        `This puts ${docLabels[doc]} back to what north ships with. Anything written here is lost.`,
        {
          title: `Reset ${docLabels[doc]}?`,
          confirmLabel: "Reset",
          danger: true,
        },
      ))
    )
      return;
    await api(`/orchestrator/context/${doc}`, { method: "DELETE" });
    setDraft(null);
    await resource.reload();
  };
  const saveFact = async () => {
    if (!factDraft.trim()) return;
    if (editingFact)
      await api(`/web/api/memory/facts/${editingFact}`, {
        method: "PATCH",
        body: JSON.stringify({ content: factDraft, category: "user" }),
      });
    else
      await post("/web/api/memory/facts", {
        content: factDraft,
        category: "user",
      });
    setFactDraft("");
    setEditingFact(null);
    await facts.reload();
  };
  const removeFact = async (id: string) => {
    if (
      !(await dialog.confirm(
        "north will stop using it, and stop recalling it for future tasks.",
        { title: "Forget this fact?", confirmLabel: "Forget", danger: true },
      ))
    )
      return;
    await api(`/web/api/memory/facts/${id}`, { method: "DELETE" });
    await facts.reload();
  };
  const onDocuments = tab === "documents";
  return (
    <div className="page">
      <PageHeader
        eyebrow="Knowledge"
        title="Memory"
        subtitle="Everything north has learned about you, and from you."
        actions={
          onDocuments && !isUserFacts ? (
            <>
              <button onClick={removeDocument}>Reset document</button>
              <button
                className="primary-button"
                disabled={draft === null}
                onClick={save}
              >
                Save document
              </button>
            </>
          ) : null
        }
      />
      <div className="segmented memory-tabs">
        {MEMORY_TABS.map(([value, label]) => (
          <button
            className={tab === value ? "active" : ""}
            key={value}
            onClick={() => setTab(value)}
          >
            {label}
          </button>
        ))}
      </div>

      {onDocuments && (
        <div className="memory-layout">
          <aside>
            {docs.map((name) => (
              <button
                className={doc === name ? "active" : ""}
                key={name}
                onClick={() => {
                  setDoc(name);
                  setDraft(null);
                }}
              >
                {docLabels[name]}
              </button>
            ))}
          </aside>
          <div className="memory-editor-grid">
            <section>
              <div className="editor-label">
                {isUserFacts ? "User facts" : "Markdown source"}
              </div>
              {isUserFacts ? (
                <div className="document-help">
                  User details are managed as individual facts on the Facts tab.
                </div>
              ) : resource.loading ? (
                <Loading />
              ) : (
                <textarea
                  className="document-editor"
                  value={content}
                  onChange={(e) => setDraft(e.target.value)}
                />
              )}
            </section>
            <section
              className={`memory-preview ${isUserFacts ? "user-facts-preview" : ""}`}
            >
              <div className="editor-label">Rendered preview</div>
              {isUserFacts ? (
                <div className="document-help">
                  Your durable user details live on the Facts tab.
                </div>
              ) : (
                <Markdown>{content || "Nothing written yet."}</Markdown>
              )}
            </section>
          </div>
        </div>
      )}

      {tab === "facts" && (
        <section className="facts-panel panel">
          <header>
            <div>
              <span>Durable context</span>
              <h2>Facts</h2>
            </div>
            <span>{facts.data?.length || 0} stored</span>
          </header>
          <div className="panel-content">
            <div className="fact-editor">
              <input
                value={factDraft}
                onChange={(e) => setFactDraft(e.target.value)}
                placeholder="Add one atomic fact…"
              />
              <button className="ghost-button" onClick={saveFact}>
                {editingFact ? "Update" : "Add fact"}
              </button>
              {editingFact && (
                <button
                  className="ghost-button"
                  onClick={() => {
                    setEditingFact(null);
                    setFactDraft("");
                  }}
                >
                  Cancel
                </button>
              )}
            </div>
            <div className="fact-list">
              {facts.loading ? (
                <Loading />
              ) : (
                (facts.data || []).map((fact) => (
                  <article
                    className={`fact-row${fact.content === find ? " memory-found" : ""}`}
                    key={fact.id}
                    ref={fact.content === find ? reveal : undefined}
                  >
                    <div>
                      <b>{fact.content}</b>
                      <small>
                        {fact.category || "general"} · updated{" "}
                        {timeAgo(fact.updated_at)}
                      </small>
                    </div>
                    <div className="fact-actions">
                      <span>
                        {fact.confidence != null
                          ? `${Math.round(Number(fact.confidence) * 100)}%`
                          : "Active"}
                      </span>
                      <button
                        onClick={() => {
                          setEditingFact(fact.id);
                          setFactDraft(fact.content);
                        }}
                      >
                        Edit
                      </button>
                      <button onClick={() => removeFact(fact.id)}>
                        Delete
                      </button>
                    </div>
                  </article>
                ))
              )}
              {!facts.loading && !facts.data?.length && (
                <Empty>No durable facts have been captured yet.</Empty>
              )}
            </div>
          </div>
        </section>
      )}

      {tab === "episodes" && <EpisodesPanel />}
      {onDocuments && (
        <p className="muted memory-note">
          Past approval decisions and safe actions are on{" "}
          <Link to="/approvals?tab=decisions">Approvals</Link>. Reading your
          own files into memory is a step in <Link to="/setup">Setup</Link>.
        </p>
      )}
    </div>
  );
}

