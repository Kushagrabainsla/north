import { FormEvent, useEffect, useMemo, useState } from "react";
import { api, post } from "../api";
import { Empty, ErrorNotice, HealthIndicator, Loading, Markdown, PageHeader, Panel, Status, timeAgo } from "../components";
import { useResource } from "../hooks";
import type { Approval, Artifact, LedgerEntry } from "../types";

export function Tasks() {
  const resource = useResource<LedgerEntry[]>("/orchestrator/ledger?limit=500", 7000);
  if (resource.loading) return <Loading/>;
  const tasks = new Map<string, LedgerEntry[]>();
  for (const entry of resource.data || []) if (entry.task_id) tasks.set(entry.task_id, [...(tasks.get(entry.task_id) || []), entry]);
  return <div className="page"><PageHeader eyebrow="Work" title="Tasks" subtitle="Every task, from prompt to final outcome."/>{resource.error && <ErrorNotice message={resource.error}/>}<div className="table-list">
    {[...tasks].map(([id, entries]) => { const latest = entries[0]; const terminal = entries.find(e => e.action?.startsWith("task_completed") || ["task_failed", "task_cancelled"].includes(e.action || "")); const prompt = [...entries].reverse().find(e => e.action === "task_received")?.input; return <div className="table-row" key={id}><div className="row-main"><b>{prompt || id}</b><small>{id} · {timeAgo(latest.timestamp)}</small></div><span>{[...new Set(entries.map(e => e.agent).filter(Boolean))].join(", ") || "orchestrator"}</span><Status value={terminal?.status || "running"}/></div>; })}
  </div>{!tasks.size && <Empty>No task history yet.</Empty>}</div>;
}

function ArtifactLibrary({ newsOnly = false }: { newsOnly?: boolean }) {
  const resource = useResource<Artifact[]>("/web/api/artifacts", 10000);
  const [selected, setSelected] = useState<Artifact | null>(null);
  const [error, setError] = useState("");
  const files = (resource.data || []).filter(file => !newsOnly || file.kind === "news");
  const open = async (file: Artifact) => { try { setSelected(await api<Artifact>(`/web/api/artifacts/${file.id}`)); } catch (err) { setError(String(err)); } };
  if (resource.loading) return <Loading/>;
  return <>{(resource.error || error) && <ErrorNotice message={resource.error || error}/>}<div className="artifact-grid">{files.map(file => <button className="artifact-card" key={file.id} onClick={() => open(file)}><span>{file.kind === "news" ? "☼" : "◇"}</span><div><b>{file.name}</b><small>{file.kind} · {file.size ? `${Math.ceil(file.size / 1024)} KB` : ""} · {timeAgo(file.updated_at)}</small></div></button>)}</div>{!files.length && <Empty>No files have been generated in this section.</Empty>}{selected && <div className="document-view"><header><div><span>{selected.kind}</span><h2>{selected.name}</h2></div><button onClick={() => setSelected(null)}>Close</button></header><Markdown>{selected.content || ""}</Markdown></div>}</>;
}

export function Artifacts() { return <div className="page"><PageHeader eyebrow="Outputs" title="Artifacts" subtitle="Every report, briefing, note, plan, and file North has produced."/><ArtifactLibrary/></div>; }

export function Approvals() {
  const resource = useResource<Approval[]>("/web/api/approvals", 4000);
  const decide = async (card: Approval, decision: string, chosen_option = "") => { await post("/orchestrator/approval/respond", { card_id: card.id, decision, chosen_option }); await resource.reload(); };
  if (resource.loading) return <Loading/>;
  const pending = (resource.data || []).filter(card => card.status === "pending");
  const history = (resource.data || []).filter(card => card.status !== "pending");
  return <div className="page"><PageHeader eyebrow="Attention" title="Approvals" subtitle="Questions and consequential actions waiting for your decision."/>{resource.error && <ErrorNotice message={resource.error}/>}<div className="approval-stack">{pending.map(card => <article className="approval-card" key={card.id}><div className="approval-type">{card.type}</div><h2>{card.title}</h2><p>{card.message}</p><small>{card.agent} · {timeAgo(card.created_at)}</small><div className="approval-actions">{card.type === "question" ? card.options.map(option => <button key={option} onClick={() => decide(card, "answered", option)}>{option}</button>) : <><button className="primary-button" onClick={() => decide(card, "approved")}>Approve</button><button className="danger-button" onClick={() => decide(card, "rejected")}>Reject</button></>}</div></article>)}</div>{!pending.length && <Empty>Nothing needs your attention.</Empty>}<h2 className="section-title">Resolved</h2><div className="table-list">{history.map(card => <div className="table-row" key={card.id}><div className="row-main"><b>{card.title}</b><small>{card.agent} · {timeAgo(card.created_at)}</small></div><Status value={card.status}/></div>)}</div></div>;
}

interface Job { job_id: string; agent: string; task: string; status: string; scheduled_at: string; }
interface Cron { name: string; agent: string; task: string; hour: number; minute: number; weekday?: number; }
export function Schedule() {
  const jobs = useResource<Job[]>("/orchestrator/jobs?limit=100", 10000);
  const cron = useResource<Cron[]>("/orchestrator/cron", 10000);
  return <div className="page"><PageHeader eyebrow="Automation" title="Schedule" subtitle="Queued work and recurring routines."/><div className="two-column"><Panel title="Agenda" label={`${jobs.data?.length || 0} jobs`}>{jobs.loading ? <Loading/> : jobs.data?.length ? jobs.data.map(job => <div className="list-row" key={job.job_id}><div><b>{job.task}</b><small>{job.agent} · {new Date(job.scheduled_at).toLocaleString()}</small></div><Status value={job.status}/></div>) : <Empty>No queued work.</Empty>}</Panel><Panel title="Recurring" label={`${cron.data?.length || 0} routines`}>{cron.loading ? <Loading/> : cron.data?.length ? cron.data.map(item => <div className="list-row" key={item.name}><div><b>{item.name}</b><small>{item.task} · {String(item.hour).padStart(2,"0")}:{String(item.minute).padStart(2,"0")}</small></div><span>{item.agent}</span></div>) : <Empty>No recurring routines.</Empty>}</Panel></div></div>;
}

const docs = ["user.md", "north_stars.md", "judgement_rules.md", "soul.md"];
interface ContextDoc { document: string; content: string; }
export function Memory() {
  const [doc, setDoc] = useState(docs[0]);
  const resource = useResource<ContextDoc>(`/orchestrator/context/${doc}`);
  const facts = useResource<any[]>("/web/api/memory/facts", 10000);
  const [draft, setDraft] = useState<string | null>(null);
  const [factDraft, setFactDraft] = useState("");
  const [editingFact, setEditingFact] = useState<string | null>(null);
  const content = draft ?? resource.data?.content ?? "";
  const save = async () => { await api(`/orchestrator/context/${doc}`, { method: "PUT", body: JSON.stringify({ content }) }); setDraft(null); await resource.reload(); };
  const saveFact = async () => { if (!factDraft.trim()) return; if (editingFact) await api(`/web/api/memory/facts/${editingFact}`, { method: "PATCH", body: JSON.stringify({ content: factDraft, category: "user" }) }); else await post("/web/api/memory/facts", { content: factDraft, category: "user" }); setFactDraft(""); setEditingFact(null); await facts.reload(); };
  const removeFact = async (id: string) => { if (!window.confirm("Delete this fact?")) return; await api(`/web/api/memory/facts/${id}`, { method: "DELETE" }); await facts.reload(); };
  return <div className="page"><PageHeader eyebrow="Knowledge" title="Memory" subtitle="The durable context North brings into your work." actions={<button className="primary-button" disabled={draft === null} onClick={save}>Save document</button>}/><div className="memory-layout"><aside>{docs.map(name => <button className={doc === name ? "active" : ""} key={name} onClick={() => { setDoc(name); setDraft(null); }}>{name.replaceAll("_", " ")}</button>)}</aside><div className="memory-editor-grid"><section><div className="editor-label">Markdown source</div>{resource.loading ? <Loading/> : <textarea className="document-editor" value={content} onChange={e => setDraft(e.target.value)} />}</section><section className="memory-preview"><div className="editor-label">Rendered preview</div><Markdown>{content || "Nothing written yet."}</Markdown></section></div></div><section className="facts-panel panel"><header><div><span>Durable context</span><h2>Facts</h2></div><span>{facts.data?.length || 0} stored</span></header><div className="panel-content"><div className="fact-editor"><input value={factDraft} onChange={e => setFactDraft(e.target.value)} placeholder="Add one atomic fact…"/><button className="ghost-button" onClick={saveFact}>{editingFact ? "Update" : "Add fact"}</button>{editingFact && <button className="ghost-button" onClick={() => { setEditingFact(null); setFactDraft(""); }}>Cancel</button>}</div><div className="fact-list">{facts.loading ? <Loading/> : (facts.data || []).map(fact => <article className="fact-row" key={fact.id}><div><b>{fact.content}</b><small>{fact.category || "general"} · updated {timeAgo(fact.updated_at)}</small></div><div className="fact-actions"><span>{fact.confidence != null ? `${Math.round(Number(fact.confidence) * 100)}%` : "Active"}</span><button onClick={() => { setEditingFact(fact.id); setFactDraft(fact.content); }}>Edit</button><button onClick={() => removeFact(fact.id)}>Delete</button></div></article>)}{!facts.loading && !facts.data?.length && <Empty>No durable facts have been captured yet.</Empty>}</div></div></section><Bootstrap embedded/></div>;
}

interface Agent { name: string; domain: string; model_pool: string; accepts: string[]; }
interface Confidence { agent: string; tool: string; confidence: number; }
export function Agents() {
  const agents = useResource<Agent[]>("/orchestrator/agents");
  const confidence = useResource<Confidence[]>("/orchestrator/tools/confidence");
  return <div className="page"><PageHeader eyebrow="Capabilities" title="Agents" subtitle="North's specialist team and the tools they trust."/><div className="agent-grid">{agents.data?.map(agent => <article key={agent.name}><div className="agent-avatar">{agent.name.slice(0,1).toUpperCase()}</div><h2>{agent.name}</h2><p>{agent.domain} · {agent.model_pool}</p><div className="tag-list">{agent.accepts.slice(0,5).map(item => <span key={item}>{item}</span>)}</div><h3>Tool confidence</h3>{confidence.data?.filter(item => item.agent === agent.name).slice(0,4).map(item => <div className="confidence" key={item.tool}><span>{item.tool}</span><i><b style={{width: `${item.confidence * 100}%`}}/></i></div>)}</article>)}</div></div>;
}

export function Activity() {
  const resource = useResource<LedgerEntry[]>("/orchestrator/ledger?limit=300", 5000);
  const [query, setQuery] = useState("");
  const rows = useMemo(() => (resource.data || []).filter(item => JSON.stringify(item).toLowerCase().includes(query.toLowerCase())), [resource.data, query]);
  return <div className="page"><PageHeader eyebrow="Audit trail" title="Activity" subtitle="Every important action and state transition across North." actions={<input className="header-search" placeholder="Filter events" value={query} onChange={e => setQuery(e.target.value)}/>}/>{resource.loading ? <Loading/> : <div className="event-list verbose-events">{rows.map(entry => <div className="event-row" key={entry.id}><span className={`event-dot ${entry.status || ""}`}/><div><b>{entry.action?.replaceAll("_", " ") || entry.source}</b><small>{entry.agent || entry.source} · {entry.task_id || "system"} · {new Date(entry.timestamp).toLocaleString()}</small>{(entry.output || entry.input) && <p>{(entry.output || entry.input || "").slice(0,600)}</p>}</div><Status value={entry.status}/></div>)}</div>}</div>;
}

export function Insights() {
  const metrics = useResource<Record<string, any>>("/orchestrator/metrics?days=30", 15000);
  const costs = useResource<Record<string, any>>("/orchestrator/inference/costs?period=month", 15000);
  const models = useResource<Record<string, any>>("/orchestrator/inference/models", 15000);
  return <div className="page"><PageHeader eyebrow="Performance" title="Insights" subtitle="Usage, cost, reliability, and model availability."/><div className="metric-cards"><div><span>Tasks · 30 days</span><strong>{metrics.data?.total_tasks || 0}</strong></div><div><span>Input tokens</span><strong>{Number(metrics.data?.total_tokens_in || 0).toLocaleString()}</strong></div><div><span>Output tokens</span><strong>{Number(metrics.data?.total_tokens_out || 0).toLocaleString()}</strong></div><div><span>Model cost</span><strong>${Number(costs.data?.total_cost_usd || 0).toFixed(4)}</strong></div></div><div className="two-column"><Panel title="Cost by model">{Object.entries(costs.data?.by_model || {}).map(([name,value]) => <div className="list-row" key={name}><b>{name}</b><span>${Number(value).toFixed(4)}</span></div>)}</Panel><Panel title="Model pools">{Object.entries(models.data || {}).map(([name, pool]: [string, any]) => <div className="list-row" key={name}><div><b>{name}</b><small>{pool.models?.length || 0} models available</small></div><span className="pool-availability">Available</span></div>)}</Panel></div></div>;
}

interface SettingsData { power: string; autonomy: string; }
export function SettingsPage() {
  const resource = useResource<SettingsData>("/orchestrator/settings");
  const [typeScale, setTypeScale] = useState(() => localStorage.getItem("north-type-scale") || "comfortable");
  useEffect(() => { document.documentElement.dataset.typeScale = typeScale; localStorage.setItem("north-type-scale", typeScale); }, [typeScale]);
  const update = async (body: Partial<SettingsData>) => { await post("/orchestrator/settings", body); await resource.reload(); };
  return <div className="page"><PageHeader eyebrow="Configuration" title="Settings" subtitle="Control how North balances capability, cost, autonomy, and readability."/><div className="settings-grid"><Panel title="Power" label="Model strategy"><div className="segmented">{["eco","cruise","sport"].map(value => <button className={resource.data?.power === value ? "active" : ""} onClick={() => update({ power:value })} key={value}>{value}</button>)}</div><p className="muted">Choose how aggressively North selects capable models.</p></Panel><Panel title="Autonomy" label="Approval behavior"><div className="segmented">{["interactive","auto","autonomous"].map(value => <button className={resource.data?.autonomy === value ? "active" : ""} onClick={() => update({ autonomy:value })} key={value}>{value}</button>)}</div><p className="muted">Consequential and destructive actions remain governed by North's safety policy.</p></Panel><Panel title="Text size" label="Personal preference"><div className="segmented text-scale-selector">{[["compact","Compact"],["comfortable","Comfortable"],["large","Large"]].map(([value,label]) => <button className={typeScale === value ? "active" : ""} onClick={() => setTypeScale(value)} key={value}>{label}</button>)}</div><p className="muted">Choose the reading scale used throughout the web interface.</p></Panel></div></div>;
}

export function SystemPage() {
  const overview = useResource<any>("/web/api/system", 8000); const metrics = useResource<any>("/orchestrator/metrics?days=30", 15000); const costs = useResource<any>("/orchestrator/inference/costs?period=month", 15000); const models = useResource<Record<string, any>>("/orchestrator/inference/models", 15000);
  const [keys, setKeys] = useState<Record<string, string>>({}); const [providerMessage, setProviderMessage] = useState(""); const [expandedPool, setExpandedPool] = useState<string | null>(null);
  const providers = overview.data?.providers || []; const totalCost = Number(costs.data?.total_cost_usd ?? metrics.data?.total_cost_usd ?? 0);
  const saveProvider = async (id: string) => { try { await post(`/web/api/providers/${id}`, { api_key: keys[id] }); setKeys(current => ({ ...current, [id]: "" })); setProviderMessage("Provider credentials saved and runtime refreshed."); await overview.reload(); } catch (error) { setProviderMessage(String(error)); } };
  return <div className="page"><PageHeader eyebrow="Runtime" title="System" subtitle="Every detail about North's runtime, providers, model pools, costs, and health."/><div className="system-hero"><HealthIndicator variant="hero"/></div><div className="metric-cards"><div><span>Tasks · 30 days</span><strong>{metrics.data?.total_tasks || 0}</strong></div><div><span>Input tokens</span><strong>{Number(metrics.data?.total_tokens_in || 0).toLocaleString()}</strong></div><div><span>Output tokens</span><strong>{Number(metrics.data?.total_tokens_out || 0).toLocaleString()}</strong></div><div><span>Model cost · month</span><strong>${totalCost.toFixed(4)}</strong></div></div><div className="two-column"><Panel title="Providers" label={`${providers.filter((p: any) => p.configured).length} configured`}>{providerMessage && <div className="notice">{providerMessage}</div>}<div className="provider-list">{providers.map((provider: any) => <div className="provider-row" key={provider.id}><div><b>{provider.name}</b><small>{provider.description} · {provider.auth_kind === "oauth_pkce" ? "Browser login" : provider.env_key}</small></div><div className="provider-controls"><span className={provider.configured ? "provider-state configured" : "provider-state"}>{provider.configured ? `Ready ${provider.credential_hint}` : "Not configured"}</span>{provider.env_key && <div className="provider-edit"><input type="password" placeholder="API key" value={keys[provider.id] || ""} onChange={event => setKeys(current => ({ ...current, [provider.id]: event.target.value }))}/><button className="ghost-button" disabled={!keys[provider.id]} onClick={() => saveProvider(provider.id)}>Save</button></div>}</div></div>)}</div></Panel><Panel title="Model pools" label="Available now"><div>{Object.entries(models.data || {}).map(([name, pool]: [string, any]) => <div className="model-pool" key={name}><button className="model-pool-toggle" onClick={() => setExpandedPool(expandedPool === name ? null : name)}><span><b>{name}</b><small>{pool.models?.length || 0} models available</small></span><span className="pool-availability">{expandedPool === name ? "Hide" : "Inspect"}</span></button>{expandedPool === name && <div className="model-list">{(pool.models || []).map((model: any) => <div className="model-row" key={`${model.provider}-${model.id}`}><b>{model.id}</b><span>{model.provider}</span></div>)}</div>}</div>)}</div></Panel></div><div className="two-column"><Panel title="Cost by model" label="Month to date">{Object.entries(costs.data?.by_model || {}).map(([name,value]) => <div className="list-row" key={name}><b>{name}</b><span>${Number(value).toFixed(4)}</span></div>)}{!Object.keys(costs.data?.by_model || {}).length && <Empty>No recorded inference costs yet.</Empty>}</Panel><Panel title="Runtime configuration"><div className="list-row"><b>Power</b><span>{overview.data?.settings?.power || "–"}</span></div><div className="list-row"><b>Autonomy</b><span>{overview.data?.settings?.autonomy || "–"}</span></div><div className="list-row"><b>Bootstrap</b><span>{overview.data?.bootstrap?.status || "–"}</span></div></Panel></div></div>;
}

export function Bootstrap({ embedded = false }: { embedded?: boolean }) {
  const resource = useResource<any>("/web/api/system", 5000); const [message, setMessage] = useState(""); const bootstrap = resource.data?.bootstrap; const completed = new Set((bootstrap?.completed || []).map((item: any) => item.path)); const candidates = bootstrap?.candidates || [];
  const start = async () => { setMessage("Starting bootstrap…"); try { const result = await post<any>("/web/api/bootstrap", { paths: [] }); setMessage(`${result.selected || 0} document(s) queued for bootstrap.`); await resource.reload(); } catch (error) { setMessage(String(error)); } };
  const content = <><div className="bootstrap-heading"><div><div className="eyebrow">Bootstrap coverage</div><h2>Documents North has read</h2><p>Track eligible documents and refresh durable context in one batch.</p></div><button className="primary-button" onClick={start}>Run bootstrap</button></div><div className="metric-cards"><div><span>Status</span><strong>{bootstrap?.status || "–"}</strong></div><div><span>Eligible documents</span><strong>{bootstrap?.candidate_count || 0}</strong></div><div><span>Completed documents</span><strong>{completed.size}</strong></div></div>{message && <div className="notice">{message}</div>}<Panel title="Document coverage" label="Bootstrap sources"><div className="bootstrap-list">{candidates.map((path: string) => <div className="bootstrap-row" key={path}><span><b>{path.split("/").pop()}</b><small>{path}</small></span><Status value={completed.has(path) ? "completed" : "pending"}/></div>)}</div>{!candidates.length && <Empty>No eligible bootstrap documents found.</Empty>}</Panel></>;
  return embedded ? <section className="bootstrap-embedded">{content}</section> : <div className="page">{content}</div>;
}
