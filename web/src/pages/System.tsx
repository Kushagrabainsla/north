import { useEffect, useState } from "react";
import { api, del, post } from "../api";
import { HealthIndicator, PageHeader, Panel, unpricedNote, YoloBadge } from "../components";
import { useDialog } from "../dialog";
import { useResource } from "../hooks";
import { Button, EntityTable, ListRow, Notice, ResourceView, Stat, StatGrid, Status } from "../kit";

type LastUpdate = { state: "waiting" | "running" | "updated" | "failed" | "cancelled"; detail: string; at: string };
export type UpdateStatus = { version: string; commit: string | null; last: LastUpdate | null };

// How the last update went, in a few words. The update restarts north, so this is
// read from what the update wrote down, after the page comes back.
export function lastUpdateText(last: LastUpdate | null): string {
  if (!last) return "never updated from here";
  return {
    waiting: `waiting for north to be idle, since ${last.at}`,
    running: `updating since ${last.at}`,
    updated: `updated ${last.at}`,
    failed: `failed ${last.at}`,
    cancelled: `cancelled ${last.at}`,
  }[last.state];
}

// An install can take a few minutes on a cold cache; past this, something is wrong.
const UPDATE_WAIT_MS = 10 * 60_000;
const UPDATE_POLL_MS = 3000;
// While an update waits for north to be idle, the reason it still waits is read this often.
const STATUS_POLL_MS = 10_000;

function UpdatePanel() {
  const status = useResource<UpdateStatus>("/web/api/update", STATUS_POLL_MS);
  const { confirm } = useDialog();
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState("");
  const last = status.data?.last ?? null;

  // North goes away while it installs, so a failed request here means "not back yet".
  const waitForReturn = async () => {
    setWaiting(true);
    const deadline = Date.now() + UPDATE_WAIT_MS;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, UPDATE_POLL_MS));
      try {
        const now = await api<UpdateStatus>("/web/api/update");
        // A reload, not a refresh: the update may have brought a new dashboard too.
        if (now.last?.state !== "running") return window.location.reload();
      } catch {
        // still restarting
      }
    }
    setWaiting(false);
    setError(
      "North has not come back after 10 minutes. Run north start in a terminal. What the update printed is in update.log in north's home folder.",
    );
  };

  // An update that waited for north to be idle starts on its own: follow it like one started here.
  useEffect(() => {
    if (last?.state === "running" && !waiting) void waitForReturn();
  }, [last?.state]); // eslint-disable-line react-hooks/exhaustive-deps

  const request = async (when: "now" | "idle") => {
    setError("");
    try {
      await post("/web/api/update", { when });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return false;
    }
    return true;
  };

  const updateNow = async () => {
    const yes = await confirm(
      "North installs the latest version from GitHub and restarts now. Anything running now is interrupted. This page reloads by itself when north is back, usually within a few minutes.",
      { title: "Update north now?", confirmLabel: "Update and restart" },
    );
    if (yes && (await request("now"))) await waitForReturn();
  };

  const updateWhenIdle = async () => {
    if (await request("idle")) await status.reload();
  };

  const cancel = async () => {
    setError("");
    try {
      await del("/web/api/update");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    await status.reload();
  };

  const busy = waiting || last?.state === "running";
  const queued = last?.state === "waiting";
  return (
    <Panel
      title="Version"
      label="Updates"
      actions={
        queued ? (
          <Button onClick={() => void cancel()}>Cancel update</Button>
        ) : (
          <>
            <Button disabled={busy} onClick={() => void updateNow()}>
              Update now
            </Button>
            <Button variant="primary" busy={busy} busyLabel="Updating…" onClick={() => void updateWhenIdle()}>
              Update when idle
            </Button>
          </>
        )
      }
    >
      <ListRow
        title="Running"
        description={`${status.data?.version || "–"}${status.data?.commit ? ` · ${status.data.commit}` : ""}`}
      />
      <ListRow title="Last update" description={lastUpdateText(last)} />
      {queued && (
        <Notice>
          {last?.detail || "Waiting for north to be idle for 10 minutes."} North updates when no task, flow or
          scheduled job has run for 10 minutes and none is due in the next 10, so nothing is cut off.
        </Notice>
      )}
      {waiting && (
        <Notice>
          Updating. North stops, installs the new version and starts again. This page reloads when it is back.
        </Notice>
      )}
      {!waiting && last?.state === "failed" && last.detail && <Notice tone="error">{last.detail.slice(-800)}</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
    </Panel>
  );
}


export function SystemPage() {
  const overview = useResource<any>("/web/api/system", 8000);
  const metrics = useResource<any>("/orchestrator/metrics?days=30", 15000);
  const costs = useResource<any>(
    "/orchestrator/inference/costs?period=month",
    15000,
  );
  const totalCost = Number(
    costs.data?.total_cost_usd ?? metrics.data?.total_cost_usd ?? 0,
  );
  const unpriced = costs.data?.unpriced_calls ?? metrics.data?.unpriced_calls;
  const settings = overview.data?.settings;
  const browser = overview.data?.browser;
  const embeddings = overview.data?.embeddings;

  return (
    <div className="page">
      <PageHeader
        title="System"
        subtitle="Health, cost and what north runs on."
      />
      <div className="system-hero">
        <HealthIndicator variant="hero" />
      </div>
      <StatGrid>
        <Stat label="Tasks · 30 days" value={metrics.data?.total_tasks || 0} />
        <Stat label="Input tokens" value={Number(metrics.data?.total_tokens_in || 0).toLocaleString()} />
        <Stat label="Output tokens" value={Number(metrics.data?.total_tokens_out || 0).toLocaleString()} />
        <Stat label="Model cost · month" value={`$${totalCost.toFixed(4)}${unpricedNote(unpriced)}`} />
      </StatGrid>
      {/* Which models north uses, and the power, are on the Settings page only (#80). */}
      <div className="two-column">
        <Panel title="Cost by model" label="Month to date">
          <ResourceView resource={costs}>
            {(data) => <EntityTable
              label="Cost by model"
              columns={[{ key: "model", label: "Model" }, { key: "cost", label: "Cost", width: "auto" }]}
              rows={Object.entries(data?.by_model || {})}
              rowKey={([name]) => name}
              cell={([name, value], key) => (key === "model" ? name : `$${Number(value).toFixed(4)}`)}
              empty="No recorded inference costs yet."
            />}
          </ResourceView>
        </Panel>
        <Panel title="Runtime configuration">
          <ListRow title="Autonomy" description={settings?.autonomy || "–"} badge={<YoloBadge mode={settings?.autonomy} />} />
          {/* Depends on a binary north cannot install for you, so the row carries
            what to do about it rather than only that it is missing. */}
          <ListRow title="Browser" description={browser?.state || "–"}>
            {browser?.state === "unavailable" && <Notice tone="warning">{browser.detail}</Notice>}
          </ListRow>
          {/* MCP servers you configured: the ones that started, and below, anything configured that is not
            working. A server whose command did not exist was only a warning in north.log. */}
          {(overview.data?.mcp_servers || []).map((server: { name: string; tools: number }) => (
            <ListRow key={`mcp-${server.name}`} title={`MCP: ${server.name}`} description={`${server.tools} tool${server.tools === 1 ? "" : "s"}`} />
          ))}
          {(overview.data?.not_working || []).map((why: string) => (
            <ListRow key={why} title={why} status={<Status label="Not working" tone="bad" />} />
          ))}
          {/* The one model north runs itself. Worth naming: every stored vector is
            stamped with it, so "which embeddings am I on?" decides which memories
            can still be compared with which. */}
          <ListRow
            title="Embeddings"
            description={embeddings?.model || "–"}
            meta={[embeddings?.local ? "on-device · no data leaves this machine" : embeddings?.provider || "not configured"]}
          >
            {/* Whether they work: the model can be named and still fail to load. */}
            {embeddings?.state === "unavailable" && (
              <Notice tone="error">
                Not working: {embeddings.detail}. Skills cannot be learned, and tool, code and memory search fall back to keywords.
              </Notice>
            )}
          </ListRow>
        </Panel>
      </div>
      <UpdatePanel />
    </div>
  );
}
