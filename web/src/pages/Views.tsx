// The Views page: custom panels north made, each one drawn from the kit.
//
// North adds a view through an approval card; until then it is a draft you reach by the
// card's link, and its parts do nothing. Using a part of a live view runs its action as you;
// a call that sends, spends, deletes, pushes or runs a command shows its exact values first.
// Removing a view is yours: a red Delete that asks first.

import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { del, post } from "../api";
import { PageHeader, timeAgo } from "../components";
import { useDialog } from "../dialog";
import { useResource } from "../hooks";
import { Button, confirmDelete, EntityTable, Loading, Menu, Notice, RelatedLinks, ResourceView, Stack, type Column } from "../kit";
import { ViewSurface, type ActionBindings, type ViewMessage } from "../views";

interface About { kind: "flow" | "skill" | "tool"; name: string }
interface ViewListing { id: string; title: string; description: string; about: About | null; updated_at: string; reads: string[]; actions: number }
interface OpenView extends ViewListing {
  draft: boolean;
  problems: string[];
  failed: Record<string, string>;
  action_labels: Record<string, string>;
  bindings: ActionBindings;
  refresh: number;
  messages: ViewMessage[];
}
/** What a part's action did: a flow started, or a tool call made, or a call that waits for you to confirm it. */
interface ActionResult {
  run_id?: string;
  done?: boolean;
  error?: string;
  confirm?: { title: string; message: string; fingerprint: string };
}

const ABOUT_PAGES: Record<About["kind"], string> = { flow: "/flows", skill: "/skills", tool: "/tools" };

const COLUMNS: Column[] = [
  { key: "view", label: "View", width: "minmax(0, 2fr)" },
  { key: "about", label: "About" },
  { key: "reads", label: "Reads" },
  { key: "updated", label: "Updated", width: "120px" },
];

/** The views north made about one flow, skill or tool. */
export const viewsAbout = (views: ViewListing[], kind: About["kind"], name: string) =>
  views.filter(view => view.about?.kind === kind && view.about.name === name);

/** A line of links to the views about a flow, skill or tool, at the top of its dialog; nothing when it has none. */
export function ViewsAbout({ kind, name }: { kind: About["kind"]; name: string }) {
  const views = useResource<ViewListing[]>(name ? "/web/api/views" : null);
  return <RelatedLinks label="Views" links={viewsAbout(views.data || [], kind, name).map(view => ({ label: view.title, to: `/views/${view.id}` }))}/>;
}

export function Views() {
  const resource = useResource<ViewListing[]>("/web/api/views");
  return <div className="page">
    <PageHeader title="Views" subtitle="Pages North made for itself."/>
    <ResourceView resource={resource} isEmpty={data => !data.length}
      empty="North has made no views yet. Ask it in a session: “make me a page that shows …”.">
      {data => <EntityTable label="Views" columns={COLUMNS} rows={data} rowKey={view => view.id} to={view => `/views/${view.id}`}
        cell={(view, key) => {
          if (key === "view") return <><b>{view.title}</b>{view.description && <small>{view.description}</small>}</>;
          if (key === "about") return view.about ? `${view.about.kind} ${view.about.name}` : "—";
          if (key === "reads") return view.reads.join(", ") || "nothing";
          return timeAgo(view.updated_at);
        }}/>}
    </ResourceView>
  </div>;
}

export function ViewPage() {
  const { viewId = "" } = useParams();
  const [params] = useSearchParams();
  const draft = params.get("draft") === "1";
  const navigate = useNavigate();
  const dialog = useDialog();
  const resource = useResource<OpenView>(`/web/api/views/${encodeURIComponent(viewId)}${draft ? "?draft=1" : ""}`);
  const [busy, setBusy] = useState("");
  const [said, setSaid] = useState<{ tone: "success" | "error"; text: string } | null>(null);
  const view = resource.data;

  // Read the data again every `refresh` seconds while the view is open; a draft stays as it is.
  const { reload } = resource;
  const every = draft ? 0 : view?.refresh || 0;
  useEffect(() => {
    if (!every) return;
    const timer = window.setInterval(() => void reload(), every * 1000);
    return () => window.clearInterval(timer);
  }, [every, reload]);

  const call = (name: string, values: Record<string, unknown>, confirm = "") =>
    post<ActionResult>(`/web/api/views/${encodeURIComponent(viewId)}/actions/${encodeURIComponent(name)}`, { values, confirm });

  const run = async (name: string, values: Record<string, unknown>, live = false) => {
    const label = view?.action_labels[name] || name;
    if (live) {
      // The Microphone's stream: sent quietly. A failure stops nothing, and says so once.
      const result = await call(name, values).catch((error: unknown) => ({ done: false, error: String(error) }) as ActionResult);
      if (result.error) setSaid({ tone: "error", text: `${label}: ${result.error}` });
      return;
    }
    setBusy(name);
    setSaid(null);
    try {
      let result = await call(name, values);
      if (result.confirm) {
        // North wrote this panel: a send, a spend, a delete, a push or a command shows its exact values first.
        // The tool writes its card's message in Markdown; the dialog shows plain text, so code fences go.
        const shown = result.confirm.message.replace(/```[\w-]*\n?/g, "").trim();
        const yes = await dialog.confirm(shown, { title: result.confirm.title, confirmLabel: "Do it", danger: true });
        if (!yes) return;
        result = await call(name, values, result.confirm.fingerprint);
      }
      if (result.run_id) setSaid({ tone: "success", text: `Started: ${label} (run ${result.run_id}). Its steps ask as usual; follow it on Flows.` });
      else if (result.error) setSaid({ tone: "error", text: `${label}: ${result.error}` });
      await reload();
    } catch (error) {
      setSaid({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy("");
    }
  };

  const remove = async () => {
    if (!view || !(await confirmDelete(dialog, { noun: "view", name: view.title, consequence: "It leaves the Views page. North can make it again if you ask." }))) return;
    try {
      await del(`/web/api/views/${encodeURIComponent(viewId)}`);
      navigate("/views");
    } catch (error) {
      setSaid({ tone: "error", text: error instanceof Error ? error.message : String(error) });
    }
  };

  const about = view?.about;
  return <div className="page">
    <PageHeader title={view?.title || "View"} subtitle={view?.description || undefined}
      actions={<>
        {about && <Button to={ABOUT_PAGES[about.kind]}>{`The ${about.kind} ${about.name}`}</Button>}
        <Menu items={[{ label: "Refresh", onSelect: () => void resource.reload() }, ...(view && !draft ? [{ label: "Delete", danger: true, onSelect: () => void remove() }] : [])]}/>
      </>}/>
    {resource.loading && !view ? <Loading/> : resource.error && !view ? <Notice tone="error">{resource.error}</Notice> : view && <Stack>
      {draft && <Notice tone="warning">A draft. It joins the Views page once you approve the card that asks to add it; until then its buttons do nothing.</Notice>}
      {view.problems.length > 0 && <Notice tone="error">This view cannot be drawn: {view.problems.join("; ")}</Notice>}
      {Object.entries(view.failed).map(([name, why]) => <Notice key={name} tone="warning">Could not read {name}: {why}</Notice>)}
      {said && <Notice tone={said.tone} onDismiss={() => setSaid(null)}>{said.text}</Notice>}
      {view.messages.length > 0 && <ViewSurface messages={view.messages} bindings={view.bindings || {}} busyAction={busy}
        onAction={draft ? undefined : (name, values, live) => run(name, values, live)}/>}
    </Stack>}
  </div>;
}
