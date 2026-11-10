// North's UI kit: one component for each kind of thing a page shows, so the
// same kind of thing looks and behaves the same everywhere (CODING_STYLE §5.1).
//
// Every page builds its lists, rows, buttons, tabs, forms and notices from
// these. They are styled only by kit.css, with classes that start with `kit-`,
// so nothing else in the stylesheet can change them. The same set is the
// catalog north's own views are drawn from: a view can use only what is here.

import { cloneElement, isValidElement, useId, useState, type FormEvent, type ReactElement, type ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";
import type { useDialog } from "./dialog";

const cx = (...names: (string | false | null | undefined)[]) => names.filter(Boolean).join(" ");

// ── Buttons ─────────────────────────────────────────────────────────────────

// `icon-danger` is an icon-only remove or delete in a row: red, like `danger`, without a border.
// `link` is an action inside a line of text: "Stop", "Thought for 24s", "Show source".
export type ButtonVariant = "primary" | "secondary" | "danger" | "quiet" | "icon" | "icon-danger" | "link";

export interface ButtonProps {
  variant?: ButtonVariant;
  size?: "sm" | "md";
  /** While true the button is disabled and reads `busyLabel`. */
  busy?: boolean;
  busyLabel?: string;
  icon?: ReactNode;
  /** A link that looks like a button. */
  to?: string;
  type?: "button" | "submit";
  disabled?: boolean;
  title?: string;
  "aria-label"?: string;
  /** A choice or a toggle: true gives the chosen look (accent border), on any variant. */
  "aria-pressed"?: boolean;
  "aria-expanded"?: boolean;
  "aria-haspopup"?: "dialog" | "menu";
  onClick?: () => void;
  children?: ReactNode;
}

/** The one button. `primary` is a page's main action, `danger` deletes or
 *  removes something, `quiet` is a text-weight action inside a row. */
export function Button({ variant = "secondary", size = "md", busy = false, busyLabel, icon, to, type = "button", disabled, title, onClick, children, ...aria }: ButtonProps) {
  const className = cx("kit-button", `kit-button-${variant}`, `kit-button-${size}`);
  const content = <>{icon}{children !== undefined && <span>{busy && busyLabel ? busyLabel : children}</span>}</>;
  // A plain link: a button that goes somewhere is not the current page.
  if (to) return <Link className={className} to={to} title={title} {...aria}>{content}</Link>;
  return <button type={type} className={className} disabled={disabled || busy} title={title} onClick={onClick} {...aria}>{content}</button>;
}

/** Actions in one fixed order: `danger` alone at the start, then the others,
 *  the primary one last. Every form, card and dialog uses it. */
export function ActionBar({ danger, children, align = "end" }: { danger?: ReactNode; children?: ReactNode; align?: "start" | "end" }) {
  return <div className={cx("kit-action-bar", `kit-action-bar-${align}`)}>
    {danger && <div className="kit-action-bar-danger">{danger}</div>}
    <div className="kit-action-bar-main">{children}</div>
  </div>;
}

type Dialog = ReturnType<typeof useDialog>;

/** Ask before deleting, always the same way: what goes, what that means, a red "Delete …". */
export function confirmDelete(dialog: Dialog, { noun, name, consequence, verb = "Delete" }: { noun: string; name: string; consequence?: string; verb?: string }): Promise<boolean> {
  return dialog.confirm(consequence || "This cannot be undone.", {
    title: `${verb} ${noun} "${name}"?`,
    confirmLabel: `${verb} ${noun}`,
    danger: true,
  });
}

// ── Tabs, search, toolbar ───────────────────────────────────────────────────

export interface TabItem<T extends string> { value: T; label: string; count?: number; disabled?: boolean; title?: string }

/** One tab style for every page: views of the same list, or sections of one thing. */
export function Tabs<T extends string>({ items, value, onChange, label, disabled = false }: { items: TabItem<T>[]; value: T; onChange: (value: T) => void; label: string; disabled?: boolean }) {
  return <div className="kit-tabs" role="tablist" aria-label={label}>
    {items.map(item => <button key={item.value} type="button" role="tab" aria-selected={item.value === value}
      disabled={disabled || item.disabled} title={item.title}
      className={cx("kit-tab", item.value === value && "kit-tab-active")} onClick={() => onChange(item.value)}>
      {item.label}{item.count !== undefined && <span className="kit-tab-count">{item.count}</span>}
    </button>)}
  </div>;
}

export function SearchField({ value, onChange, label, placeholder, count }: { value: string; onChange: (value: string) => void; label: string; placeholder?: string; count?: string }) {
  return <label className="kit-search">
    <span className="kit-visually-hidden">{label}</span>
    <input type="search" value={value} placeholder={placeholder || label} aria-label={label} onChange={event => onChange(event.target.value)}/>
    {count && <small>{count}</small>}
  </label>;
}

/** The line above a list: its tabs, its search, and actions on the list itself. */
export function Toolbar({ tabs, search, actions }: { tabs?: ReactNode; search?: ReactNode; actions?: ReactNode }) {
  return <div className="kit-toolbar">{tabs}{search}{actions && <div className="kit-toolbar-actions">{actions}</div>}</div>;
}

// ── Sections ────────────────────────────────────────────────────────────────

/** A few lines saying what a panel or step is for, under its title. */
export function Note({ children }: { children: ReactNode }) {
  return <div className="kit-note">{children}</div>;
}

/** The name bar on top of an editor or a preview pane, with an optional second part at the right. */
export function PaneLabel({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return <div className="kit-pane-label"><span>{children}</span>{aside && <span>{aside}</span>}</div>;
}

/** Blocks one above the other with the kit's one gap: a header, a form, a list. */
export function Stack({ gap = "md", children }: { gap?: "sm" | "md" | "lg"; children: ReactNode }) {
  return <div className={cx("kit-stack", `kit-stack-${gap}`)}>{children}</div>;
}

/** Facts about one thing as label and value: a run's details, a read-only card field. */
export function KeyValues({ items }: { items: [string, ReactNode][] }) {
  return <dl className="kit-key-values">{items.filter(([, value]) => value !== undefined && value !== null && value !== "").map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>;
}

/** The title of a block: a panel, a form, a dialog or a page section. */
export function SectionHeader({ title, label, description, actions, level = 2 }: { title: string; label?: string; description?: ReactNode; actions?: ReactNode; level?: 2 | 3 }) {
  const Heading = level === 2 ? "h2" : "h3";
  return <header className="kit-section-header">
    <div>{label && <span className="kit-eyebrow">{label}</span>}<Heading>{title}</Heading>{description && <p>{description}</p>}</div>
    {actions && <div className="kit-section-actions">{actions}</div>}
  </header>;
}

// ── Status ──────────────────────────────────────────────────────────────────

export type Tone = "neutral" | "good" | "busy" | "warn" | "bad";

// What each status word means, by colour. Running is not "done", and an active
// skill is not a warning: both used to share the wrong colour.
const STATUS_TONES: Record<string, Tone> = {
  completed: "good", success: "good", active: "good", approved: "good", online: "good", ready: "good",
  answered: "good", response_ready: "good", connected: "good", enabled: "good", passed: "good", set: "good",
  running: "busy", retrying: "busy", working: "busy", pending: "busy", queued: "busy", starting: "busy", testing: "busy",
  waiting: "warn", waiting_for_approval: "warn", waiting_for_user: "warn", paused: "warn", candidate: "warn", draft: "warn",
  needs_attention: "warn", degraded: "warn", interrupted: "warn", stale: "warn", not_set: "warn",
  failed: "bad", error: "bad", rejected: "bad", cancelled: "bad", blocked: "bad", offline: "bad", yolo: "bad",
};

export const statusTone = (value: string): Tone => STATUS_TONES[value] || "neutral";

/** A state, as a word with the colour that word means everywhere. */
export function Status({ value, label, tone }: { value?: string; label?: string; tone?: Tone }) {
  const normalized = value || "unknown";
  return <span className={cx("kit-status", `kit-tone-${tone || statusTone(normalized)}`)}>{label || normalized.replaceAll("_", " ")}</span>;
}

/** A tag that is not a state: where something came from, or what kind it is. */
export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "accent" }) {
  return <span className={cx("kit-badge", tone === "accent" && "kit-badge-accent")}>{children}</span>;
}

// ── Figures ────────────────────────────────────────────────────────────────

/** A few headline numbers side by side: tasks this week, cost, what is running. */
export function StatGrid({ children }: { children: ReactNode }) {
  return <div className="kit-stat-grid">{children}</div>;
}

/** One figure: what it counts, the number or word, and an optional line under it. */
export function Stat({ label, value, detail }: { label: string; value: ReactNode; detail?: ReactNode }) {
  return <div className="kit-stat"><span>{label}</span><strong>{value}</strong>{detail && <small>{detail}</small>}</div>;
}

// ── Notices, loading, empty ─────────────────────────────────────────────────

export type NoticeTone = "info" | "success" | "warning" | "error";

/** A message about the page: what happened, what went wrong, what to know. */
export function Notice({ tone = "info", onDismiss, children }: { tone?: NoticeTone; onDismiss?: () => void; children: ReactNode }) {
  return <div className={cx("kit-notice", `kit-notice-${tone}`)} role={tone === "error" ? "alert" : "status"}>
    <div>{children}</div>
    {onDismiss && <Button variant="quiet" size="sm" onClick={onDismiss} aria-label="Dismiss">Dismiss</Button>}
  </div>;
}

/** Waiting for data: a line in the panel or list it fills. `inline={false}` only
 *  where the whole page has nothing to show yet. */
export function Loading({ inline = true, label = "Loading" }: { inline?: boolean; label?: string }) {
  return <div className={cx("kit-loading", inline && "kit-loading-inline")} role="status"><span className="kit-pulse"/>{label}</div>;
}

export function Empty({ children = "Nothing here yet." }: { children?: ReactNode }) {
  return <div className="kit-empty">{children}</div>;
}

interface Resource<T> { data: T | null | undefined; loading: boolean; error?: string | null }

/** A list's three states and its content, the same on every page: loading, a
 *  failure that says why, an empty list that says what would go here. */
export function ResourceView<T>({ resource, isEmpty, empty, inline = true, children }: { resource: Resource<T>; isEmpty?: (data: T) => boolean; empty?: ReactNode; inline?: boolean; children: (data: T) => ReactNode }) {
  if (resource.error && !resource.data) return <Notice tone="error">{resource.error}</Notice>;
  if (resource.loading && !resource.data) return <Loading inline={inline}/>;
  if (resource.data === null || resource.data === undefined) return <Loading inline={inline}/>;
  if (isEmpty?.(resource.data)) return <Empty>{empty}</Empty>;
  return <>{children(resource.data)}</>;
}

// ── Lists ───────────────────────────────────────────────────────────────────

export interface Column { key: string; label: string; width?: string }

/** A list of things of one kind with the same facts for each: skills, tools,
 *  flows, sessions, tasks. A row opens the thing, by `onOpen` or link `to`. */
export function EntityTable<R>({ columns, rows, rowKey, cell, onOpen, to, rowActions, empty, label }: {
  columns: Column[];
  rows: R[];
  rowKey: (row: R) => string;
  cell: (row: R, key: string) => ReactNode;
  onOpen?: (row: R) => void;
  to?: (row: R) => string;
  rowActions?: (row: R) => ReactNode;
  empty?: ReactNode;
  label: string;
}) {
  const template = [...columns.map(column => column.width || "minmax(0, 1fr)"), rowActions ? "auto" : ""].filter(Boolean).join(" ");
  return <div className="kit-table" role="table" aria-label={label} style={{ "--kit-columns": template } as React.CSSProperties}>
    <div className="kit-table-head" role="row">{columns.map(column => <span role="columnheader" key={column.key}>{column.label}</span>)}{rowActions && <span/>}</div>
    {rows.length === 0 ? <Empty>{empty}</Empty> : rows.map(row => {
      const cells = <>{columns.map(column => <span role="cell" className="kit-table-cell" key={column.key}>{cell(row, column.key)}</span>)}</>;
      const link = to?.(row);
      return <div className={cx("kit-table-row", (link || onOpen) && "kit-table-row-open")} role="row" key={rowKey(row)}>
        {link ? <NavLink className="kit-table-open" to={link}>{cells}</NavLink>
          : onOpen ? <button type="button" className="kit-table-open" onClick={() => onOpen(row)}>{cells}</button>
          : <div className="kit-table-open">{cells}</div>}
        {rowActions && <span className="kit-table-actions" role="cell">{rowActions(row)}</span>}
      </div>;
    })}
  </div>;
}

// Stable, so React calls it once when the row mounts, not on every poll.
const scrollIntoView = (element: HTMLElement | null) => element?.scrollIntoView({ block: "center" });

/** One row for a thing in a short list: what it is, a line about it, its state
 *  and what you can do with it. Replaces every page's own row. `found` marks the
 *  row a link landed on: scrolled into view and outlined. */
export function ListRow({ title, meta, description, status, badge, actions, onOpen, to, muted = false, found = false, children }: {
  title: ReactNode;
  meta?: ReactNode[];
  description?: ReactNode;
  status?: ReactNode;
  badge?: ReactNode;
  actions?: ReactNode;
  onOpen?: () => void;
  to?: string;
  muted?: boolean;
  found?: boolean;
  children?: ReactNode;
}) {
  const main = <div className="kit-row-main">
    <div className="kit-row-title"><b>{title}</b>{badge}</div>
    {description && <p>{description}</p>}
    {meta && meta.filter(Boolean).length > 0 && <small>{meta.filter(Boolean).map((item, index) => <span key={index}>{item}</span>)}</small>}
  </div>;
  return <div className={cx("kit-row", muted && "kit-row-muted", found && "kit-row-found")} ref={found ? scrollIntoView : undefined}>
    <div className="kit-row-line">
      {to ? <NavLink className="kit-row-open" to={to}>{main}</NavLink>
        : onOpen ? <button type="button" className="kit-row-open" onClick={onOpen}>{main}</button>
        : main}
      {(status || actions) && <div className="kit-row-side">{status}{actions}</div>}
    </div>
    {children && <div className="kit-row-body">{children}</div>}
  </div>;
}

/** Things shown as cards: files, sessions, devices. */
export function CardGrid({ children }: { children: ReactNode }) {
  return <div className="kit-card-grid">{children}</div>;
}

export function EntityCard({ icon, title, meta, onOpen, to, children }: { icon?: ReactNode; title: ReactNode; meta?: ReactNode; onOpen?: () => void; to?: string; children?: ReactNode }) {
  const head = <>{icon && <span className="kit-card-icon">{icon}</span>}<b>{title}</b>{meta && <small>{meta}</small>}</>;
  return <div className="kit-card">
    {to ? <NavLink className="kit-card-head" to={to}>{head}</NavLink>
      : onOpen ? <button type="button" className="kit-card-head" onClick={onOpen}>{head}</button>
      : <div className="kit-card-head">{head}</div>}
    {children && <div className="kit-card-body">{children}</div>}
  </div>;
}

// ── Waiting on you ──────────────────────────────────────────────────────────

/** An option as a title and its explanation: "North-native: flows that…" → both parts. */
export function optionParts(option: string): [string, string] {
  const at = option.indexOf(": ");
  return at > 0 ? [option.slice(0, at), option.slice(at + 2)] : [option, ""];
}

/** Answers to pick from, one row each: the part before ": " as the title, the rest under it. */
export function ChoiceList({ options, chosen, onPick }: { options: string[]; chosen?: unknown; onPick: (option: string) => void }) {
  if (!options.length) return null;
  return <div className="kit-choices">{options.map(option => {
    const [title, detail] = optionParts(option);
    return <button type="button" key={option} className="kit-choice" aria-pressed={chosen === option} onClick={() => onPick(option)}>
      <span>{title}</span>{detail && <small>{detail}</small>}
    </button>;
  })}</div>;
}

/** An answer in your own words. Behind "Something else…" when there are choices above it. */
export function OtherAnswer({ collapsed, value, onChange, onSubmit, submitLabel }: { collapsed: boolean; value: string; onChange: (value: string) => void; onSubmit?: () => void; submitLabel: string }) {
  const [open, setOpen] = useState(!collapsed || Boolean(value));
  if (!open) return <button type="button" className="kit-choice kit-choice-other" onClick={() => setOpen(true)}><span>Something else…</span></button>;
  const submit = () => { if (value.trim() && onSubmit) onSubmit(); };
  return <div className="kit-other-answer">
    <input aria-label="Your answer" autoFocus={collapsed} value={value} placeholder="Type your answer" onChange={event => onChange(event.target.value)}
      onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); submit(); } }}/>
    {onSubmit && <Button variant="primary" disabled={!value.trim()} onClick={submit}>{submitLabel}</Button>}
  </div>;
}

/** Something waiting on you: a question or an action to approve. `source` is what it is
 *  judged against: beside the work where there is room, else behind "Review source". */
export function WaitingCard({ label, aside, title, source, sourceBeside = false, meta, actions, children }: {
  label: string;
  aside?: ReactNode;
  title?: ReactNode;
  source?: string;
  sourceBeside?: boolean;
  meta?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  const beside = Boolean(source) && sourceBeside;
  return <article className="kit-waiting">
    <div className="kit-waiting-top"><span>{label}</span>{aside}</div>
    {title && <div className="kit-waiting-title">{title}</div>}
    {beside ? <div className="kit-waiting-split">
      <div className="kit-waiting-body">{children}</div>
      <aside className="kit-waiting-source"><span className="kit-field-label">Source</span><pre>{source}</pre></aside>
    </div> : <div className="kit-waiting-body">
      {children}
      {source && <details className="kit-waiting-details"><summary>Review source</summary><pre>{source}</pre></details>}
    </div>}
    {meta && <small className="kit-waiting-meta">{meta}</small>}
    {actions && <div className="kit-waiting-foot">{actions}</div>}
  </article>;
}

// ── Forms ───────────────────────────────────────────────────────────────────

const INPUTS = new Set(["input", "select", "textarea"]);

/** A labelled control. The control is the child: an input, select or textarea
 *  gets the kit's one input style from kit.css. A single input is tied to the
 *  label, so clicking the label focuses it; anything else (several choice
 *  buttons) is a group the label names. */
export function Field({ label, hint, error, wide = false, children }: { label: string; hint?: ReactNode; error?: string; wide?: boolean; children: ReactNode }) {
  const id = useId();
  const input = isValidElement(children) && typeof children.type === "string" && INPUTS.has(children.type)
    ? children as ReactElement<{ id?: string }> : null;
  const inputId = input?.props.id || `${id}-input`;
  return <div className={cx("kit-field", wide && "kit-field-wide")}>
    {input ? <label className="kit-field-label" id={id} htmlFor={inputId}>{label}</label> : <span className="kit-field-label" id={id}>{label}</span>}
    {input ? <div className="kit-field-control">{cloneElement(input, { id: inputId })}</div>
      : <div className="kit-field-control" role="group" aria-labelledby={id}>{children}</div>}
    {error ? <small className="kit-field-error">{error}</small> : hint && <small>{hint}</small>}
  </div>;
}

/** Creating or changing a thing: a title, fields, and Cancel then Save at the
 *  end. A real form, so Enter submits. */
export function EntityForm({ title, label, description, onSubmit, onCancel, submitLabel, busy = false, busyLabel = "Saving…", error, hint, canSubmit = true, level = 2, children }: {
  title: string;
  /** 3 when the form sits inside a panel that has its own h2. */
  level?: 2 | 3;
  label?: string;
  description?: ReactNode;
  onSubmit: () => void | Promise<void>;
  onCancel?: () => void;
  submitLabel: string;
  busy?: boolean;
  busyLabel?: string;
  error?: string;
  hint?: ReactNode;
  canSubmit?: boolean;
  children: ReactNode;
}) {
  const submit = (event: FormEvent) => { event.preventDefault(); if (canSubmit && !busy) void onSubmit(); };
  return <form className="kit-form" onSubmit={submit}>
    <SectionHeader title={title} label={label} description={description} level={level}/>
    <div className="kit-form-fields">{children}</div>
    {error && <Notice tone="error">{error}</Notice>}
    <div className="kit-form-foot">
      {hint && <small>{hint}</small>}
      <ActionBar>
        {onCancel && <Button onClick={onCancel}>Cancel</Button>}
        <Button variant="primary" type="submit" busy={busy} busyLabel={busyLabel} disabled={!canSubmit}>{submitLabel}</Button>
      </ActionBar>
    </div>
  </form>;
}
