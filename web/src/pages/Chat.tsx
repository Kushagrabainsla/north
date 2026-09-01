import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { NavLink, useNavigate, useParams } from "react-router-dom";
import { api, patch, post } from "../api";
import { Empty, ErrorNotice, Loading, Markdown, PageHeader, Status, timeAgo } from "../components";
import { useResource } from "../hooks";
import type { Approval, Conversation, LedgerEntry, TaskDetail, Turn } from "../types";

function useTaskStreams(turns: Turn[], reload: () => Promise<void>, reloadApprovals: () => Promise<void>) {
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
      for (const eventName of ["approval_required", "question_required", "approval_responded", "task_paused", "task_resumed"]) {
        stream.addEventListener(eventName, () => { void reload(); void reloadApprovals(); });
      }
      return stream;
    });
    return () => streams.forEach(stream => stream.close());
  }, [ids.join("|"), reload, reloadApprovals]);
  return live;
}

function ApprovalCard({ card, respond }: { card: Approval; respond: (card: Approval, decision: string, answer?: string) => Promise<void> }) {
  const [answer, setAnswer] = useState("");
  const isQuestion = card.type === "question";
  return <div className="inline-approval"><small>{isQuestion ? "North needs your answer" : "Approval required"}</small><b>{card.title}</b><p>{card.message}</p><div className="inline-approval-actions">{isQuestion ? <>{card.options.map(option => <button key={option} onClick={() => respond(card, "answered", option)}>{option}</button>)}<input value={answer} onChange={event => setAnswer(event.target.value)} placeholder="Type your answer"/><button disabled={!answer.trim()} onClick={() => respond(card, "answered", answer)}>Answer</button></> : <><button onClick={() => respond(card, "approved")}>Approve</button><button className="danger-link" onClick={() => respond(card, "rejected")}>Reject</button></>}</div></div>;
}

function EventRows({ entries }: { entries: LedgerEntry[] }) {
  return <div className="event-list">{entries.map(entry => <div className="event-row" key={entry.id}>
    <span className={`event-dot ${entry.status || ""}`}/><div><b>{entry.action?.replaceAll("_", " ") || entry.source}</b><small>{entry.agent || entry.source} · {timeAgo(entry.timestamp)}</small>{entry.output && <p>{entry.output.slice(0, 500)}</p>}</div>
  </div>)}</div>;
}

function DetailSection({ title, count, children, open }: { title: string; count?: number; children: React.ReactNode; open?: boolean }) {
  return <details className="detail-section" open={open}><summary><span>{title}</span>{count !== undefined && <em>{count}</em>}<i>⌄</i></summary><div>{children}</div></details>;
}

function TurnBundle({ turn, streamed, expandAll, reload, pendingApprovals, respondApproval }: { turn: Turn; streamed?: string; expandAll: boolean; reload: () => Promise<void>; pendingApprovals: Approval[]; respondApproval: (card: Approval, decision: string, answer?: string) => Promise<void> }) {
  const detail = turn.detail;
  const status = pendingApprovals.length ? "waiting_for_approval" : (detail?.task.status || "pending");
  const entries = detail?.entries || [];
  const runs = detail?.runs || [];
  const agents = [...new Set(runs.map(run => run.agent))];
  const tools = entries.filter(entry => entry.action?.includes("tool") || (entry.tools_used?.length || 0) > 0);
  const approvals = entries.filter(entry => entry.source === "approval" || entry.action?.includes("approval"));
  const verification = entries.filter(entry => /verify|dod|review|repair/.test(entry.action || ""));
  const cost = runs.reduce((total, run) => total + (run.cost_usd || 0), 0);
  const isActive = ["waiting_for_approval", "paused", "pending", "running", "queued"].includes(status);
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
      {pendingApprovals.map(card => <ApprovalCard key={card.id} card={card} respond={respondApproval}/>)}
      {isActive && <div className={`thinking thinking-${status}`}><span className="pulse"/>{status === "waiting_for_approval" ? "North is waiting for you" : status === "paused" ? "Task paused" : "North is working on this prompt"}</div>}
      {(streamed || detail?.output) && <Markdown>{streamed || detail?.output || ""}</Markdown>}
      {!isActive && !(streamed || detail?.output) && <Empty>No response was recorded for this task.</Empty>}
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
  const approvalResource = useResource<Approval[]>("/web/api/approvals", 2000);
  const [search, setSearch] = useState("");
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [expandAll, setExpandAll] = useState(false);
  const [recording, setRecording] = useState(false);
  const [notice, setNotice] = useState("");
  const [dragging, setDragging] = useState(false);
  const [resizing, setResizing] = useState(false);
  const [chatListWidth, setChatListWidth] = useState(() => Number(localStorage.getItem("north-chat-list-width")) || 250);
  const fileInput = useRef<HTMLInputElement>(null);
  const chatRoom = useRef<HTMLElement>(null);
  const openedAtBottom = useRef<string | null>(null);
  const visibleChats = useMemo(() => (chats.data || []).filter(chat => chat.title.toLowerCase().includes(search.toLowerCase())), [chats.data, search]);
  const live = useTaskStreams(room.data?.turns || [], room.reload, approvalResource.reload);
  useEffect(() => { setPrompt(conversationId ? localStorage.getItem(`north-chat-draft:${conversationId}`) || "" : ""); }, [conversationId]);
  useEffect(() => {
    if (!conversationId) { openedAtBottom.current = null; return; }
    if (room.data?.id !== conversationId || openedAtBottom.current === conversationId) return;
    openedAtBottom.current = conversationId;
    window.requestAnimationFrame(() => chatRoom.current?.scrollTo({ top: chatRoom.current.scrollHeight }));
  }, [conversationId, room.data?.id]);
  useEffect(() => {
    if (!resizing) return;
    const move = (event: PointerEvent) => {
      const sidebar = document.querySelector(".sidebar")?.getBoundingClientRect().width || 0;
      const width = Math.max(190, Math.min(520, event.clientX - sidebar));
      setChatListWidth(width); localStorage.setItem("north-chat-list-width", String(width));
    };
    const stop = () => setResizing(false);
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop, { once: true });
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop); };
  }, [resizing]);
  const updatePrompt = (value: string) => { setPrompt(value); if (conversationId) { if (value) localStorage.setItem(`north-chat-draft:${conversationId}`, value); else localStorage.removeItem(`north-chat-draft:${conversationId}`); } };
  const scrollLatestTurnToTop = () => window.requestAnimationFrame(() => chatRoom.current?.querySelector(".turn-bundle:last-child")?.scrollIntoView({ block: "start", behavior: "smooth" }));
  const createChat = async () => {
    const chat = await post<Conversation>("/web/api/conversations", { title: "New chat" });
    await chats.reload(); navigate(`/chat/${chat.id}`);
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!prompt.trim() || !conversationId) return;
    if (prompt.trim().startsWith("/") && await handleSlash(prompt.trim())) return;
    setSubmitting(true);
    try { await post(`/web/api/conversations/${conversationId}/turns`, { prompt }); updatePrompt(""); setNotice(""); await Promise.all([room.reload(), chats.reload()]); scrollLatestTurnToTop(); }
    catch (error) { setNotice(error instanceof Error ? error.message : String(error)); }
    finally { setSubmitting(false); }
  };
  const handleSlash = async (command: string) => {
    const [name] = command.toLowerCase().split(/\s+/, 1);
    if (name === "/new") { updatePrompt(""); await createChat(); return true; }
    if (name === "/schedule") { updatePrompt(""); navigate("/schedule"); return true; }
    if (name === "/approvals") { updatePrompt(""); navigate("/approvals"); return true; }
    if (name === "/clear") { updatePrompt(""); setNotice("Draft cleared."); return true; }
    if (name === "/help") { setNotice("Commands: /new, /clear, /status, /pause, /resume, /cancel, /schedule, /approvals"); return true; }
    if (name === "/status") { await Promise.all([room.reload(), approvalResource.reload()]); setNotice("Task status refreshed."); updatePrompt(""); return true; }
    if (["/pause", "/resume", "/cancel"].includes(name)) {
      const turn = [...(room.data?.turns || [])].reverse().find(item => item.task_id && !["completed", "failed", "cancelled"].includes(item.detail?.task.status || ""));
      if (!turn?.task_id) { setNotice("No active task in this conversation."); return true; }
      const action = name.slice(1);
      if (action === "cancel") await api(`/orchestrator/task/${turn.task_id}`, { method: "DELETE" });
      else await api(`/orchestrator/task/${turn.task_id}/${action}`, { method: "POST" });
      updatePrompt(""); await room.reload(); return true;
    }
    return false;
  };
  const toggleMic = () => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SpeechRecognition) { window.alert("Voice dictation is not available in this browser."); return; }
    const recognition = new SpeechRecognition();
    recognition.lang = navigator.language || "en-US";
    recognition.interimResults = false;
    recognition.onstart = () => setRecording(true);
    recognition.onend = () => setRecording(false);
    recognition.onresult = (event: any) => updatePrompt(`${prompt}${prompt ? " " : ""}${event.results[0][0].transcript}`);
    recognition.start();
  };
  const appendFiles = async (files: File[]) => {
    let value = prompt;
    let truncated = false;
    for (const file of files) {
      const text = await file.text();
      const prefix = `${value ? "\n\n" : ""}[Attached: ${file.name}]\n`;
      const remaining = Math.max(0, 30_000 - value.length - prefix.length);
      value += prefix + text.slice(0, remaining);
      if (text.length > remaining) truncated = true;
      if (value.length >= 30_000) break;
    }
    updatePrompt(value);
    if (truncated) setNotice("Attachment text was shortened to fit the chat message limit.");
  };
  const attachFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    await appendFiles(Array.from(event.target.files || []));
    event.target.value = "";
  };
  const respondApproval = async (card: Approval, decision: string, answer = "") => { try { await post("/orchestrator/approval/respond", { card_id: card.id, decision, chosen_option: answer }); setNotice("Response received. North is continuing the task."); await Promise.all([approvalResource.reload(), room.reload()]); } catch (error) { setNotice(error instanceof Error ? error.message : String(error)); } };
  const rename = async () => {
    if (!room.data) return;
    const title = window.prompt("Conversation title", room.data.title);
    if (title) { await patch(`/web/api/conversations/${room.data.id}`, { title }); await Promise.all([room.reload(), chats.reload()]); }
  };
  const deleteChat = async (id: string) => {
    if (!window.confirm("Delete this conversation and its turns?")) return;
    await api(`/web/api/conversations/${id}`, { method: "DELETE" });
    await chats.reload();
    if (id === conversationId) navigate("/chat");
  };
  return <div className={`chat-page ${dragging ? "dragging" : ""}`} style={{ "--chat-list-width": `${chatListWidth}px` } as CSSProperties} onDragEnter={event => { event.preventDefault(); setDragging(true); }} onDragOver={event => event.preventDefault()} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }} onDrop={event => { event.preventDefault(); setDragging(false); void appendFiles(Array.from(event.dataTransfer.files)); }}>
    <aside className="chat-list"><div className="chat-list-head"><b>Conversations</b><button onClick={createChat}>+</button></div><input aria-label="Search conversations" placeholder="Search chats" value={search} onChange={e => setSearch(e.target.value)}/>
      <div className="chat-scroll">{visibleChats.map(chat => <NavLink to={`/chat/${chat.id}`} key={chat.id}><span className="chat-icon">◫</span><div><b>{chat.title}</b><small>{timeAgo(chat.updated_at)}</small></div>{chat.pinned && <em>•</em>}<button type="button" className="chat-delete" aria-label={`Delete ${chat.title}`} title="Delete conversation" onClick={event => { event.preventDefault(); event.stopPropagation(); void deleteChat(chat.id); }}>×</button></NavLink>)}</div>
    </aside>
    <div className="chat-resizer" role="separator" aria-orientation="vertical" aria-label="Resize conversation list" tabIndex={0} onPointerDown={() => setResizing(true)} onKeyDown={event => { if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return; event.preventDefault(); const width = Math.max(190, Math.min(520, chatListWidth + (event.key === "ArrowLeft" ? -16 : 16))); setChatListWidth(width); localStorage.setItem("north-chat-list-width", String(width)); }}/>
    <section className="chat-room" ref={chatRoom}>
      {!conversationId ? <div className="chat-welcome"><div className="north-symbol">N</div><h1>What are we working on?</h1><p>Start a new conversation or return to one of your previous rooms.</p><button className="primary-button" onClick={createChat}>New conversation</button></div> : room.loading ? <Loading/> : room.error || !room.data ? <ErrorNotice message={room.error || "Conversation unavailable"}/> : <>
        <PageHeader eyebrow="Conversation" title={room.data.title} subtitle={`${room.data.turns?.length || 0} prompts · Updated ${timeAgo(room.data.updated_at)}`} actions={<><button className="ghost-button" onClick={() => setExpandAll(v => !v)}>{expandAll ? "Collapse all" : "Expand all"}</button><button className="ghost-button" onClick={rename}>Rename</button></>}/>
        <div className="turns">{room.data.turns?.length ? room.data.turns.map(turn => <TurnBundle key={turn.id} turn={turn} streamed={turn.task_id ? live[turn.task_id] : ""} expandAll={expandAll} reload={room.reload} pendingApprovals={(approvalResource.data || []).filter(card => card.task_id === turn.task_id && card.status === "pending")} respondApproval={respondApproval}/>) : <div className="empty-room"><span>✦</span><h2>A fresh room</h2><p>Your prompts, North's responses, and every execution detail will stay together here.</p></div>}</div>
        {notice && <div className="chat-notice" onClick={() => setNotice("")}>{notice}</div>}
        <form className="composer" onSubmit={submit}><textarea value={prompt} onChange={e => updatePrompt(e.target.value)} placeholder="Ask North anything…" rows={2} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }}/><div><small>Enter to send · Shift Enter for a new line · /help for commands</small><div className="composer-actions"><input ref={fileInput} type="file" multiple hidden onChange={attachFile}/><button type="button" className="composer-tool" onClick={() => fileInput.current?.click()} aria-label="Attach files" title="Attach files or drag them here">＋</button><button type="button" className={`composer-tool ${recording ? "recording" : ""}`} onClick={toggleMic} aria-label="Use microphone" title="Use microphone">{recording ? "■" : "♩"}</button><button disabled={submitting || !prompt.trim()}>{submitting ? "Starting…" : "Send ↑"}</button></div></div></form>
      </>}
    </section>
  </div>;
}
