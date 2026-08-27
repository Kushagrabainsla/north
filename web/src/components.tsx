import type { ReactNode } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useState } from "react";
import { post } from "./api";
import type { Conversation } from "./types";

const nav = [
  ["/", "Dashboard", "⌘"], ["/chat", "Chat", "◫"], ["/tasks", "Tasks", "✓"],
  ["/artifacts", "Artifacts", "◇"],
  ["/schedule", "Schedule", "◷"], ["/approvals", "Approvals", "!"],
  ["/memory", "Memory", "◎"], ["/agents", "Agents", "△"],
  ["/activity", "Activity", "≋"], ["/bootstrap", "Bootstrap", "↥"],
  ["/system", "System", "◉"], ["/settings", "Settings", "⚙"]
];

export function Layout() {
  const navigate = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const newChat = async () => {
    const chat = await post<Conversation>("/web/api/conversations", { title: "New chat" });
    navigate(`/chat/${chat.id}`);
  };
  return <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}>
    <aside className="sidebar">
      <div className="brand"><img className="brand-logo" src="https://repository-images.githubusercontent.com/1221207908/a9516630-e5f6-475f-ab80-44b2dd6dc9c8" alt="North logo"/><div><b>north</b><small>personal operating system</small></div></div>
      <button className="sidebar-toggle" onClick={() => setCollapsed(value => !value)} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}>{collapsed ? "›" : "‹"}</button>
      <button className="new-chat" onClick={newChat}>+ New conversation</button>
      <nav>{nav.map(([to, label, icon]) => <NavLink key={to} to={to} end={to === "/"}>
        <span className="nav-icon">{icon}</span><span>{label}</span>
      </NavLink>)}</nav>
      <div className="server-state"><span className="online-dot"/><div><b>North is online</b><small>Local server · live</small></div></div>
    </aside>
    <main className="workspace"><Outlet /></main>
  </div>;
}

export function PageHeader({ eyebrow, title, subtitle, actions }: { eyebrow?: string; title: string; subtitle?: string; actions?: ReactNode }) {
  return <header className="page-header"><div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}<h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div><div className="header-actions">{actions}</div></header>;
}

export function Panel({ title, label, children, to, className = "" }: { title: string; label?: string; children: ReactNode; to?: string; className?: string }) {
  return <section className={`panel ${className}`}><header><div>{label && <span>{label}</span>}<h2>{title}</h2></div>{to && <NavLink to={to}>View all <span>→</span></NavLink>}</header><div className="panel-content">{children}</div></section>;
}

export function Status({ value }: { value?: string }) {
  const normalized = value || "unknown";
  return <span className={`status status-${normalized}`}>{normalized.replaceAll("_", " ")}</span>;
}

export function Empty({ children = "Nothing here yet." }: { children?: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Loading() { return <div className="loading"><span className="pulse"/>Loading</div>; }
export function ErrorNotice({ message }: { message: string }) { return <div className="error-notice">{message}</div>; }

export function Markdown({ children }: { children: string }) {
  const lines = children.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [], list: string[] = [], ordered = false, code: string[] | null = null;
  const flushParagraph = () => { if (paragraph.length) { blocks.push(<p key={`p-${blocks.length}`}>{renderInline(paragraph.join("\n"))}</p>); paragraph = []; } };
  const flushList = () => { if (list.length) { const Tag = ordered ? "ol" : "ul"; blocks.push(<Tag key={`l-${blocks.length}`}>{list.map((item, i) => <li key={i}>{renderInline(item)}</li>)}</Tag>); list = []; } };
  lines.forEach((line, index) => {
    if (line.trim().startsWith("```")) { flushParagraph(); flushList(); if (code) { blocks.push(<pre key={`c-${index}`}><code>{code.join("\n")}</code></pre>); code = null; } else code = []; return; }
    if (code) { code.push(line); return; }
    const heading = line.match(/^(#{1,3})\s+(.+)/); const bullet = line.match(/^\s*[-*+]\s+(.+)/); const number = line.match(/^\s*\d+[.)]\s+(.+)/);
    if (heading) { flushParagraph(); flushList(); const Tag = `h${heading[1].length}` as "h1" | "h2" | "h3"; blocks.push(<Tag key={`h-${index}`}>{renderInline(heading[2])}</Tag>); return; }
    if (bullet || number) { flushParagraph(); if (list.length && ordered !== Boolean(number)) flushList(); ordered = Boolean(number); list.push((bullet || number)![1]); return; }
    if (!line.trim()) { flushParagraph(); flushList(); return; }
    paragraph.push(line);
  });
  flushParagraph(); flushList();
  const finalCode = code as string[] | null;
  if (finalCode) blocks.push(<pre key="c-final"><code>{finalCode.join("\n")}</code></pre>);
  return <div className="markdown">{blocks}</div>;
}

function renderInline(value: string): ReactNode[] {
  const parts = value.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*]+\*)/g);
  return parts.map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("*") && part.endsWith("*")) return <em key={index}>{part.slice(1, -1)}</em>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={index}>{part.slice(1, -1)}</code>;
    const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
    if (link) return <a key={index} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>;
    return <span key={index}>{part.split("\n").map((line, i) => <span key={i}>{line}{i < part.split("\n").length - 1 && <br/>}</span>)}</span>;
  });
}

export function timeAgo(value?: string | number) {
  if (!value) return "";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
