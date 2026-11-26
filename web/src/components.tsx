import { useEffect, useRef, type ReactNode } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { post } from "./api";
import { fileName, noticeBody, noticePreview, savedFile } from "./notices";
import { UI_PREFERENCE_KEYS, useHealth, usePersistentState, useResource } from "./hooks";
import { ActionBar, Button, Empty, Notice, Status, PaneLabel } from "./kit";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import type { AgentRun, Conversation, InferenceCategory, Notice as NoticeEntry } from "./types";
import { LayoutDashboard, MessagesSquare, CheckSquare, FileText, Inbox, Brain, Sparkles, GitBranch, Wrench, LayoutTemplate, Cpu, Settings, Search, type LucideIcon } from "lucide-react";
import { useInbox } from "./inbox";
import { CommandPalette } from "./palette";

let displayTimezone = "UTC";

/** Keep every absolute date in the web UI aligned with North's saved zone. */
export function configureDisplayTimezone(timezone: string) {
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: timezone }).format();
    displayTimezone = timezone;
  } catch {
    displayTimezone = "UTC";
  }
}

export function formatDateTime(value: string | number | Date) {
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "–";
  return new Intl.DateTimeFormat(undefined, {
    timeZone: displayTimezone, dateStyle: "medium", timeStyle: "short",
  }).format(date);
}

/** A calendar day ("2026-08-14") as text. A day is not an instant: formatting it
 * through a time zone would show Aug 13 west of UTC, so it is read as UTC. */
export function formatDay(day: string) {
  const date = new Date(`${day}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return day;
  return new Intl.DateTimeFormat(undefined, { timeZone: "UTC", dateStyle: "medium" }).format(date);
}

export function dateInNorthTimezone(date = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: displayTimezone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(date);
  const value = (type: Intl.DateTimeFormatPartTypes) => parts.find(part => part.type === type)?.value || "";
  return `${value("year")}-${value("month")}-${value("day")}`;
}

export function weekdayInNorthTimezone(date = new Date()) {
  const short = new Intl.DateTimeFormat("en-US", { timeZone: displayTimezone, weekday: "short" }).format(date);
  return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].indexOf(short);
}

// The sidebar in four groups, each with a label that says what is in it. Home and the Inbox
// stand on their own at the top: they are where you start and where north reaches you.
type NavItem = [string, string, LucideIcon];
const workNav: NavItem[] = [["/sessions", "Sessions", MessagesSquare], ["/tasks", "Tasks", CheckSquare], ["/flows", "Flows", GitBranch], ["/artifacts", "Files", FileText]];
const knowsNav: NavItem[] = [["/memory", "Memory", Brain], ["/skills", "Skills", Sparkles], ["/tools", "Tools", Wrench], ["/views", "Views", LayoutTemplate]];
const systemNav: NavItem[] = [["/system", "System", Cpu], ["/settings", "Settings", Settings]];

function NavigationLink({ to, label, Icon, badge, dot }: { to: string; label: string; Icon: LucideIcon; badge?: ReactNode; dot?: "bad" | "warn" }) {
  return <NavLink to={to} end={to === "/"} title={label}>
    <span className="nav-icon"><Icon size={16}/>{dot && <i className={`nav-dot nav-dot-${dot}`} aria-hidden="true"/>}</span>
    <span className="nav-label"><span className="nav-text">{label}{dot && <i className={`nav-dot nav-dot-inline nav-dot-${dot}`} aria-label={dot === "bad" ? "a run did not finish" : "something new"}/>}</span>{badge}</span>
  </NavLink>;
}

/** The one number that asks for you, on every page: cards and stopped runs in the Inbox. */
function InboxBadge({ count, longWait }: { count: number; longWait: boolean }) {
  if (!count) return null;
  return <em className={`nav-badge ${longWait ? "nav-badge-long" : ""}`} title={longWait ? "Something has waited more than a day" : `${count} waiting for you`}>{count}</em>;
}

export function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = usePersistentState(UI_PREFERENCE_KEYS.sidebarCollapsed, false, value => typeof value === "boolean");
  // The mobile rail is intentionally horizontal. Keep the current destination
  // visible when a deep link opens or navigation moves into a later group.
  useEffect(() => {
    if (!window.matchMedia("(max-width: 820px)").matches) return;
    document.querySelector<HTMLElement>(".sidebar nav a.active")
      ?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [location.pathname]);
  const newSession = async () => {
    const session = await post<Conversation>("/web/api/conversations", { title: "New session" });
    navigate(`/sessions/${session.id}`);
  };
  const inbox = useInbox(15000);
  const settings = useResource<{ autonomy?: string }>("/orchestrator/settings", 30000);
  const lastRuns = new Map<string, string>();
  for (const run of inbox.runs.data || []) if (!lastRuns.has(run.flow)) lastRuns.set(run.flow, run.status);
  const flowFailed = [...lastRuns.values()].some(status => status === "cancelled" || status === "failed");
  return <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}>
    <aside className="sidebar">
      <div className="brand">
        <NavLink className="brand-wordmark" to="/" aria-label="North home">
          <span className="brand-full">north<span aria-hidden="true">.</span></span>
          <span className="brand-compact" aria-hidden="true">n<span>.</span></span>
        </NavLink>
      </div>
      <button className="new-chat" onClick={newSession}>+ New session</button>
      <button type="button" className="palette-open" onClick={() => window.dispatchEvent(new Event("north:palette"))} title="Search and jump (⌘K)">
        <Search size={14}/><span className="nav-label">Search or jump…</span><kbd>⌘K</kbd>
      </button>
      <nav aria-label="North navigation">
        <div className="nav-section">
          <NavigationLink to="/" label="Home" Icon={LayoutDashboard}/>
          <NavigationLink to="/inbox" label="Inbox" Icon={Inbox} badge={<InboxBadge count={inbox.needsYou} longWait={inbox.longWait}/>}
            dot={!inbox.needsYou && inbox.info.length ? "warn" : undefined}/>
        </div>
        <div className="nav-group"><span className="nav-group-label">Your work</span>
          {workNav.map(([to, label, Icon]) => <NavigationLink key={to} to={to} label={label} Icon={Icon} dot={to === "/flows" && flowFailed ? "bad" : undefined}/>)}</div>
        <div className="nav-group"><span className="nav-group-label">What north knows and can do</span>
          {knowsNav.map(([to, label, Icon]) => <NavigationLink key={to} to={to} label={label} Icon={Icon}/>)}</div>
        <div className="nav-group nav-system"><span className="nav-group-label">North</span>{systemNav.map(([to, label, Icon]) => <NavigationLink key={to} to={to} label={label} Icon={Icon}/>)}</div>
      </nav>
      {settings.data?.autonomy === "yolo" && <NavLink to="/settings" className="yolo-warning" title="Every approval is yes">⚠ yolo: every approval is yes</NavLink>}
      <div className="sidebar-footer"><NavLink to="/system" className="health-link"><HealthIndicator/></NavLink><button className="sidebar-toggle" onClick={() => setCollapsed(value => !value)} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}>{collapsed ? "›" : "‹"}</button></div>
    </aside>
    <main className="workspace"><Outlet /></main>
    <CommandPalette/>
  </div>;
}

// `actions` puts a control in the panel's own header, beside the thing it acts
// on. "+ New routine" lived in the page header instead, a heading away from the
// list it added to and next to nothing it was related to.
// The kit's own state, empty and loading displays (kit.tsx), under the names pages already import.
export { Empty, Loading, PageHeader, Panel, Status } from "./kit";

// Shown beside the approval mode wherever it appears, while that mode says yes to everything.
export function YoloBadge({ mode }: { mode?: string }) {
  if (mode !== "yolo") return null;
  return <span title="Every approval is yes"><Status value="yolo" label="⚠ yolo"/></span>;
}

/** A failure, in the kit's error notice. */
export function ErrorNotice({ message }: { message: string }) { return <Notice tone="error">{message}</Notice>; }

export function PromptTelemetry({ run }: { run: AgentRun }) {
  const profiles = run.prompt_profiles || [];
  if (!profiles.length) return null;
  const latest = profiles.at(-1);
  const sections = Object.entries(latest?.sections || {})
    .filter(([, tokens]) => tokens > 0)
    .map(([name, tokens]) => `${name.replaceAll("_", " ")} ${tokens.toLocaleString()}`)
    .join(" · ");
  const cache = run.cache_summary;
  const cacheText = !cache || cache.telemetry === "unreported"
    ? "cache telemetry not reported"
    : `${cache.warm_turns} warm · ${cache.cold_turns} cold · ${(cache.input_cache_ratio * 100).toFixed(0)}% input cached`;
  return <>
    <small>{profiles.length} model turns · latest prompt: {sections}</small>
    <small>{cacheText}</small>
  </>;
}

/** " + 2 unpriced" after a cost that leaves out calls North has no price for, else nothing. */
export function unpricedNote(count: unknown): string {
  const calls = Number(count || 0);
  return calls ? ` + ${calls} unpriced` : "";
}

/** What north delivered to you, newest first, each until you dismiss it. */
// What north delivered: a reminder, or how a scheduled run ended. `compact` is the dashboard's view, one
// short row each with a link to the rest; without it, each notice reads in full, a long one opened on demand.
export function NoticeList({ notices, onDismissed, compact = false, focus = "" }: { notices: NoticeEntry[]; onDismissed: () => void; compact?: boolean; focus?: string }) {
  const dismiss = async (id: string) => {
    await post(`/web/api/notices/${id}/dismiss`, {});
    onDismissed();
  };
  return <>{notices.map(notice => {
    const file = savedFile(notice.message);
    const body = noticeBody(notice.message);
    const preview = noticePreview(notice.message);
    const when = <small>{timeAgo(notice.created_at)}{file && <> · saved as <span title={file}>{fileName(file)}</span></>}</small>;
    const landed = focus === notice.id;
    return <div className={`list-row notice-row ${compact ? "compact-notice" : ""} ${landed ? "landed" : ""}`} key={notice.id} ref={landed ? reveal : undefined}>
      {compact
        ? <div><b>{notice.title}</b>{preview && <small className="notice-preview">{preview}</small>}{when}{body.length > preview.length && <NavLink className="notice-more" to={`/inbox?tab=info&focus=${notice.id}`}>Read it all →</NavLink>}</div>
        : <div><b>{notice.title}</b>{body && (body.length > 600
            ? <details className="notice-body"><summary>{preview}</summary><Markdown>{body}</Markdown></details>
            : <div className="notice-body"><Markdown>{body}</Markdown></div>)}{when}</div>}
      <Button size="sm" onClick={() => void dismiss(notice.id)}>Dismiss</Button>
    </div>;
  })}</>;
}

export function InferenceCategories({ categories }: { categories: InferenceCategory[] }) {
  if (!categories.length) return <Empty>No model calls were attributed yet.</Empty>;
  return <div className="inference-categories">{categories.map(category =>
    <div className="list-row" key={category.category}>
      <div><b>{category.category}</b><small>{category.components.join(", ")}</small></div>
      <span>{category.calls} call{category.calls === 1 ? "" : "s"} · {(category.tokens_in + category.tokens_out).toLocaleString()} tokens{category.cost_usd ? ` · $${category.cost_usd.toFixed(4)}` : ""}{category.unpriced_calls ? ` · ${category.unpriced_calls} unpriced` : ""}</span>
    </div>
  )}</div>;
}

// What each health state says to the reader. "starting" is North warming up -
// the provider catalogues load after the API is serving - so it must read as
// progress, not as a fault.
const HEALTH_TEXT: Record<string, { label: string; detail: string }> = {
  online: { label: "North is operational", detail: "All core checks passed" },
  starting: { label: "North is starting", detail: "Loading models - this takes a few seconds" },
  degraded: { label: "North needs attention", detail: "One or more core checks failed" },
  offline: { label: "North is offline", detail: "Health endpoint unreachable" },
  checking: { label: "Checking North", detail: "Waiting for readiness check" },
};

export function HealthIndicator({ variant = "compact" }: { variant?: "compact" | "panel" | "hero" }) {
  const health = useHealth();
  const { label, detail } = HEALTH_TEXT[health.state] ?? HEALTH_TEXT.checking;
  const checkTitle = health.data?.checks ? Object.entries(health.data.checks).map(([name, check]) => `${name}: ${check.status}${check.detail ? ` (${check.detail})` : ""}`).join("\n") : detail;
  if (variant === "hero") return <><span className={`online-orb health-${health.state}`} title={checkTitle}/><div><h2>{label}</h2><p>Live readiness check · core services</p></div></>;
  return <div className={`health-indicator health-${health.state}`} title={checkTitle} aria-label={`${label}. ${detail}`}><span className="health-dot"/><div><b>{label}</b><small>{detail}</small></div></div>;
}

/**
 * Markdown, by react-markdown with GitHub's extensions (tables, task lists, strikethrough, autolinks).
 * It replaced north's own parser, which knew headings, lists and code only: a table in a briefing showed
 * as rows of "|" characters. A single line break stays a line break, as replies and cards are written,
 * raw HTML in the text is shown as text, and links open in a new tab.
 */
export function Markdown({ children }: { children: string }) {
  return <div className="markdown">
    <ReactMarkdown remarkPlugins={MARKDOWN_PLUGINS} components={MARKDOWN_COMPONENTS}>{children}</ReactMarkdown>
  </div>;
}

const MARKDOWN_PLUGINS = [remarkGfm, remarkBreaks];
const MARKDOWN_COMPONENTS: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer"/>,
};

export function timeAgo(value?: string | number) {
  if (!value) return "";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { timeZone: displayTimezone, month: "short", day: "numeric" });
}

/** Scroll a row into the middle of the view, for links that land on one item in a list. */
export const reveal = (element: HTMLElement | null) => element?.scrollIntoView({ block: "center" });

/** Rendered Markdown with the label and padding every modal shows it with. */
export function MarkdownPane({ label, children }: { label?: string; children: string }) {
  return <div className="markdown-pane">
    {label && <PaneLabel>{label}</PaneLabel>}
    <div className="markdown-pane-content"><Markdown>{children}</Markdown></div>
  </div>;
}

// One modal for the whole dashboard: a native dialog, closed with Escape, a
// click outside, or its Close button.
export function Modal({
  open,
  title,
  eyebrow,
  actions,
  danger,
  className = "",
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  eyebrow: string;
  actions?: ReactNode;
  /** A delete or discard: shown alone at the start of the actions. */
  danger?: ReactNode;
  className?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
  }, [open]);
  if (!open) return null;
  return (
    <dialog
      className={`capability-modal ${className}`}
      ref={ref}
      aria-label={`${title} ${eyebrow}`}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === ref.current) onClose();
      }}
    >
      <div className="capability-modal-shell">
        <header className="capability-modal-header">
          <div>
            <span className="kit-dialog-label">{eyebrow}</span>
            <h2>{title}</h2>
          </div>
          <div className="capability-modal-actions">
            <ActionBar danger={danger}>
              <Button onClick={onClose}>Close</Button>
              {actions}
            </ActionBar>
          </div>
        </header>
        <div className="capability-modal-content">{children}</div>
      </div>
    </dialog>
  );
}
