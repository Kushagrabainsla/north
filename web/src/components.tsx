import type { ReactNode } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { post } from "./api";
import type { Conversation } from "./types";

const nav = [
  ["/", "Dashboard", "⌘"], ["/chat", "Chat", "◫"], ["/tasks", "Tasks", "✓"],
  ["/briefings", "Briefings", "☼"], ["/artifacts", "Artifacts", "◇"],
  ["/schedule", "Schedule", "◷"], ["/approvals", "Approvals", "!"],
  ["/memory", "Memory", "◎"], ["/agents", "Agents", "△"],
  ["/activity", "Activity", "≋"], ["/insights", "Insights", "↗"],
  ["/system", "System", "◉"], ["/settings", "Settings", "⚙"]
];

export function Layout() {
  const navigate = useNavigate();
  const newChat = async () => {
    const chat = await post<Conversation>("/web/api/conversations", { title: "New chat" });
    navigate(`/chat/${chat.id}`);
  };
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">N</span><div><b>north</b><small>personal operating system</small></div></div>
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
  const blocks = children.split(/\n{2,}/);
  return <div className="markdown">{blocks.map((block, index) => {
    if (block.startsWith("```")) return <pre key={index}><code>{block.replace(/^```\w*\n?/, "").replace(/```$/, "")}</code></pre>;
    if (block.startsWith("### ")) return <h3 key={index}>{block.slice(4)}</h3>;
    if (block.startsWith("## ")) return <h2 key={index}>{block.slice(3)}</h2>;
    if (block.startsWith("# ")) return <h1 key={index}>{block.slice(2)}</h1>;
    const lines = block.split("\n");
    if (lines.every(line => line.startsWith("- "))) return <ul key={index}>{lines.map(line => <li key={line}>{line.slice(2)}</li>)}</ul>;
    return <p key={index}>{lines.map((line, i) => <span key={i}>{line}{i < lines.length - 1 && <br/>}</span>)}</p>;
  })}</div>;
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
