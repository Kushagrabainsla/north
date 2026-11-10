// The Views page: pages north made for itself, and each one drawn from the kit.
//
// North adds a view through an approval card; until then it is a draft you reach by the
// card's link. Removing one is yours: a red Delete that asks first.

import { useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { del, post } from "../api";
import { PageHeader, timeAgo } from "../components";
import { useDialog } from "../dialog";
import { useResource } from "../hooks";
import { Button, confirmDelete, EntityTable, Loading, Notice, ResourceView, Stack, type Column } from "../kit";
import { ViewSurface, type ViewMessage } from "../views";

interface About { kind: "flow" | "skill" | "tool"; name: string }
interface ViewListing { id: string; title: string; description: string; about: About | null; updated_at: string; reads: string[]; actions: number }
interface OpenView extends ViewListing {
  draft: boolean;
  problems: string[];
  failed: Record<string, string>;
  action_labels: Record<string, string>;
  messages: ViewMessage[];
}

const ABOUT_PAGES: Record<About["kind"], string> = { flow: "/flows", skill: "/skills", tool: "/tools" };

const COLUMNS: Column[] = [
  { key: "view", label: "View", width: "minmax(0, 2fr)" },
  { key: "about", label: "About" },
  { key: "reads", label: "Reads" },
  { key: "updated", label: "Updated", width: "120px" },
];

export function Views() {
  const resource = useResource<ViewListing[]>("/web/api/views");
  return <div className="page">
    <PageHeader eyebrow="Made by north" title="Views" subtitle="Pages north made for itself, from the same parts as every page here."/>
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

  const run = async (name: string) => {
    setBusy(name);
    setSaid(null);
    try {
      const started = await post<{ run_id: string }>(`/web/api/views/${encodeURIComponent(viewId)}/actions/${encodeURIComponent(name)}`, {});
      setSaid({ tone: "success", text: `Started: ${view?.action_labels[name] || name} (run ${started.run_id}). Its steps ask as usual; follow it on Flows.` });
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
    <PageHeader eyebrow={draft ? "Draft view" : "View"} title={view?.title || "View"} subtitle={view?.description || undefined}
      actions={<>
        {about && <Button to={ABOUT_PAGES[about.kind]}>{`The ${about.kind} ${about.name}`}</Button>}
        <Button onClick={() => void resource.reload()}>Refresh</Button>
        {view && !draft && <Button variant="danger" onClick={() => void remove()}>Delete</Button>}
      </>}/>
    {resource.loading && !view ? <Loading/> : resource.error && !view ? <Notice tone="error">{resource.error}</Notice> : view && <Stack>
      {draft && <Notice tone="warning">A draft. It joins the Views page once you approve the card that asks to add it; until then its buttons do nothing.</Notice>}
      {view.problems.length > 0 && <Notice tone="error">This view cannot be drawn: {view.problems.join("; ")}</Notice>}
      {Object.entries(view.failed).map(([name, why]) => <Notice key={name} tone="warning">Could not read {name}: {why}</Notice>)}
      {said && <Notice tone={said.tone} onDismiss={() => setSaid(null)}>{said.text}</Notice>}
      {view.messages.length > 0 && <ViewSurface messages={view.messages} onAction={draft ? undefined : name => void run(name)} busyAction={busy}/>}
    </Stack>}
  </div>;
}
