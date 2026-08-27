import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { NavLink, useNavigate, useParams } from "react-router-dom";
import { api, patch, post } from "../api";
import { Empty, ErrorNotice, Loading, Markdown, PageHeader, Status, timeAgo } from "../components";
import { useResource } from "../hooks";
import type { Conversation, LedgerEntry, TaskDetail, Turn } from "../types";

function useTaskStreams(turns: Turn[], reload: () => Promise<void>) {
  const [live, setLive] = useState<Record<string, string>>({});
  const ids = turns.filter(t => t.task_id && !["completed", "failed", "cancelled"].includes(t.detail?.task.status || "")).map(t => t.task_id!);
  useEffect(() => {
    const streams = ids.map(taskId => {
      const stream = new EventSource(`/orchestrator/stream/${taskId}`);
      stream.addEventListener("token", event => {
        const payload = JSON.parse((event as MessageEvent).data);
        setLive(current => ({ ...current, [taskId]: (current[taskId] || "") + (payload.text || "") }));
      });
      for (const eventName of ["task_completed", "task_failed", "task_cancelled", "task_skipped", "task_rejected"]) {
        stream.addEventListener(eventName, () => { stream.close(); void reload(); });
      }
      return stream;
    });
    return () => streams.forEach(stream => stream.close());
  }, [ids.join("|"), reload]);
  return live;
}

function EventRows({ entries }: { entries: LedgerEntry[] }) {
  return <div className="event-list">{entries.map(entry => <div className="event-row" key={entry.id}>
    <span className={`event-dot ${entry.status || ""}`}/><div><b>{entry.action?.replaceAll("_", " ") || entry.source}</b><small>{entry.agent || entry.source} · {timeAgo(entry.timestamp)}</small>{entry.output && <p>{entry.output.slice(0, 500)}</p>}</div>
  </div>)}</div>;
}

function DetailSection({ title, count, children, open }: { title: string; count?: number; children: React.ReactNode; open?: boolean }) {
  return <details className="detail-section" open={open}><summary><span>{title}</span>{count !== undefined && <em>{count}</em>}<i>⌄</i></summary><div>{children}</div></details>;
}

function TurnBundle({ turn, streamed, expandAll, reload }: { turn: Turn; streamed?: string; expandAll: boolean; reload: () => Promise<void> }) {
  const detail = turn.detail;
  const status = detail?.task.status || "pending";
  const entries = detail?.entries || [];
  const runs = detail?.runs || [];
  const agents = [...new Set(runs.map(run => run.agent))];
  const tools = entries.filter(entry => entry.action?.includes("tool") || (entry.tools_used?.length || 0) > 0);
  const approvals = entries.filter(entry => entry.source === "approval" || entry.action?.includes("approval"));
  const verification = entries.filter(entry => /verify|dod|review|repair/.test(entry.action || ""));
  const cost = runs.reduce((total, run) => total + (run.cost_usd || 0), 0);
  const control = async (action: "pause" | "resume" | "cancel") => {
    if (!turn.task_id) return;
    if (action === "cancel") await api(`/orchestrator/task/${turn.task_id}`, { method: "DELETE" });
    else await api(`/orchestrator/task/${turn.task_id}/${action}`, { method: "POST" });
    await reload();
  };
  return <article className="turn-bundle">
    <div className="user-prompt"><div className="avatar user-avatar">You</div><div><div className="turn-meta">Prompt {turn.position} · {timeAgo(turn.created_at)}</div><p>{turn.prompt}</p></div></div>
    <div className="north-response"><div className="avatar north-avatar">N</div><div className="response-body">
      <div className="response-heading"><div><b>North</b><Status value={status}/></div><div className="turn-actions">
        {status === "paused" && <button onClick={() => control("resume")}>Resume</button>}
        {["pending", "running", "queued"].includes(status) && <button onClick={() => control("pause")}>Pause</button>}
        {["pending", "running", "queued", "paused"].includes(status) && <button className="danger-link" onClick={() => control("cancel")}>Cancel</button>}
      </div></div>
      {(streamed || detail?.output) ? <Markdown>{streamed || detail?.output || ""}</Markdown> : <div className="thinking"><span className="pulse"/>North is working on this prompt</div>}
      <div className="turn-summary"><span>{agents.length || "–"} agents</span><span>{tools.length} tool events</span><span>{runs.reduce((n, r) => n + r.tokens_in + r.tokens_out, 0).toLocaleString()} tokens</span><span>${cost.toFixed(4)}</span></div>
      <div className="execution-details">
        <DetailSection title="Plan and routing" count={entries.filter(e => /classif|rout|plan|north_star/.test(e.action || "")).length} open={expandAll}>
          <EventRows entries={entries.filter(e => /classif|rout|plan|north_star/.test(e.action || ""))}/>
        </DetailSection>
        <DetailSection title="Agent runs" count={runs.length} open={expandAll}>
          {runs.length ? runs.map(run => <div className="run-card" key={run.run_id}><div><b>{run.agent}</b><Status value={run.status}/></div><small>Attempt {run.attempt + 1} · {run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : "running"} · {run.providers_used?.join(", ") || "provider pending"} · {run.models_used.join(", ") || "model pending"}</small>{run.error && <p className="error-text">{run.error}</p>}</div>) : <Empty>No agent runs recorded yet.</Empty>}
        </DetailSection>
        <DetailSection title="Tools and skills" count={tools.length + runs.flatMap(r => r.skills).length} open={expandAll}>
          <EventRows entries={tools}/>{runs.flatMap(run => run.skills.map(skill => <div className="skill-chip" key={`${run.run_id}-${skill.name}`}>{skill.name} <small>v{skill.version}</small></div>))}
        </DetailSection>
        <DetailSection title="Approvals and verification" count={approvals.length + verification.length} open={expandAll}>
          <EventRows entries={[...approvals, ...verification]}/>
        </DetailSection>
        <DetailSection title="Complete timeline" count={entries.length} open={expandAll}>
          <EventRows entries={entries}/>
        </DetailSection>
      </div>
    </div></div>
  </article>;
}

export function Chat() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const chats = useResource<Conversation[]>("/web/api/conversations", 5000);
  const room = useResource<Conversation>(conversationId ? `/web/api/conversations/${conversationId}` : null, 5000);
  const [search, setSearch] = useState("");
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [expandAll, setExpandAll] = useState(false);
  const [recording, setRecording] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const visibleChats = useMemo(() => (chats.data || []).filter(chat => chat.title.toLowerCase().includes(search.toLowerCase())), [chats.data, search]);
  const live = useTaskStreams(room.data?.turns || [], room.reload);
  const createChat = async () => {
    const chat = await post<Conversation>("/web/api/conversations", { title: "New chat" });
    await chats.reload(); navigate(`/chat/${chat.id}`);
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!prompt.trim() || !conversationId) return;
    setSubmitting(true);
    try { await post(`/web/api/conversations/${conversationId}/turns`, { prompt }); setPrompt(""); await Promise.all([room.reload(), chats.reload()]); }
    finally { setSubmitting(false); }
  };
  const toggleMic = () => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SpeechRecognition) { window.alert("Voice dictation is not available in this browser."); return; }
    const recognition = new SpeechRecognition();
    recognition.lang = navigator.language || "en-US";
    recognition.interimResults = false;
    recognition.onstart = () => setRecording(true);
    recognition.onend = () => setRecording(false);
    recognition.onresult = (event: any) => setPrompt(value => `${value}${value ? " " : ""}${event.results[0][0].transcript}`);
    recognition.start();
  };
  const attachFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const text = await file.text();
    setPrompt(value => `${value}${value ? "\n\n" : ""}[Attached: ${file.name}]\n${text}`);
    event.target.value = "";
  };
  const rename = async () => {
    if (!room.data) return;
    const title = window.prompt("Conversation title", room.data.title);
    if (title) { await patch(`/web/api/conversations/${room.data.id}`, { title }); await Promise.all([room.reload(), chats.reload()]); }
  };
  return <div className="chat-page">
    <aside className="chat-list"><div className="chat-list-head"><b>Conversations</b><button onClick={createChat}>+</button></div><input aria-label="Search conversations" placeholder="Search chats" value={search} onChange={e => setSearch(e.target.value)}/>
      <div className="chat-scroll">{visibleChats.map(chat => <NavLink to={`/chat/${chat.id}`} key={chat.id}><span className="chat-icon">◫</span><div><b>{chat.title}</b><small>{timeAgo(chat.updated_at)}</small></div>{chat.pinned && <em>•</em>}</NavLink>)}</div>
    </aside>
    <section className="chat-room">
      {!conversationId ? <div className="chat-welcome"><div className="north-symbol">N</div><h1>What are we working on?</h1><p>Start a new conversation or return to one of your previous rooms.</p><button className="primary-button" onClick={createChat}>New conversation</button></div> : room.loading ? <Loading/> : room.error || !room.data ? <ErrorNotice message={room.error || "Conversation unavailable"}/> : <>
        <PageHeader eyebrow="Conversation" title={room.data.title} subtitle={`${room.data.turns?.length || 0} prompts · Updated ${timeAgo(room.data.updated_at)}`} actions={<><button className="ghost-button" onClick={() => setExpandAll(v => !v)}>{expandAll ? "Collapse all" : "Expand all"}</button><button className="ghost-button" onClick={rename}>Rename</button></>}/>
        <div className="turns">{room.data.turns?.length ? room.data.turns.map(turn => <TurnBundle key={turn.id} turn={turn} streamed={turn.task_id ? live[turn.task_id] : ""} expandAll={expandAll} reload={room.reload}/>) : <div className="empty-room"><span>✦</span><h2>A fresh room</h2><p>Your prompts, North's responses, and every execution detail will stay together here.</p></div>}</div>
        <form className="composer" onSubmit={submit}><textarea value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="Ask North anything…" rows={2} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }}/><div><small>Enter to send · Shift Enter for a new line</small><div className="composer-actions"><input ref={fileInput} type="file" hidden onChange={attachFile}/><button type="button" className="composer-tool" onClick={() => fileInput.current?.click()} aria-label="Attach a file" title="Attach a file">＋</button><button type="button" className={`composer-tool ${recording ? "recording" : ""}`} onClick={toggleMic} aria-label="Use microphone" title="Use microphone">{recording ? "■" : "♩"}</button><button disabled={submitting || !prompt.trim()}>{submitting ? "Starting…" : "Send ↑"}</button></div></div></form>
      </>}
    </section>
  </div>;
}
