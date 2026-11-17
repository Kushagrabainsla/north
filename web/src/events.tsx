// One row for an event on north's ledger, the same on Tasks and in a chat turn's
// details: what happened, who did it, when, and how it ended.

import { Link } from "react-router-dom";
import { formatDateTime } from "./components";
import { ListRow, Status } from "./kit";
import type { LedgerEntry } from "./types";

// `showTask` is on in the unscoped stream and off inside a task, where saying
// which task you are looking at for the fortieth time is noise; there it is a
// link, so nobody copies an id to another page by hand.
export function EventRow({ entry, showTask = false }: { entry: LedgerEntry; showTask?: boolean }) {
  const body = entry.output || entry.input || "";
  return <ListRow
    title={entry.action?.replaceAll("_", " ") || entry.source}
    status={entry.status ? <Status value={entry.status}/> : undefined}
    meta={[
      entry.agent || entry.source,
      // north's own events - startup, cron ticks, recovery sweeps - belong to no
      // run; "system" keeps the column from reading as a missing value.
      showTask && (entry.task_id ? <Link to={`/tasks/${entry.task_id}`}>{entry.task_id}</Link> : "system"),
      formatDateTime(entry.timestamp),
      entry.model_used,
      entry.duration_ms ? `${(entry.duration_ms / 1000).toFixed(1)}s` : "",
    ]}
    description={body ? body.slice(0, 600) : undefined}
  />;
}

export function EventList({ entries, showTask = false }: { entries: LedgerEntry[]; showTask?: boolean }) {
  return <div>{entries.map(entry => <EventRow key={entry.id} entry={entry} showTask={showTask}/>)}</div>;
}
