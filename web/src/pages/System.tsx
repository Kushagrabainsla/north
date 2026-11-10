import { useState } from "react";
import { api, post } from "../api";
import { HealthIndicator, PageHeader, Panel, unpricedNote, YoloBadge } from "../components";
import { useDialog } from "../dialog";
import { useResource } from "../hooks";
import { Button, EntityTable, ListRow, Notice, ResourceView, Stat, StatGrid, Status } from "../kit";
import type { ChainModel, PartChain } from "./Settings";

type LastUpdate = { state: "running" | "updated" | "failed" | "cancelled"; detail: string; at: string };
export type UpdateStatus = { version: string; commit: string | null; last: LastUpdate | null };

// How the last update went, in a few words. The update restarts north, so this is
// read from what the update wrote down, after the page comes back.
export function lastUpdateText(last: LastUpdate | null): string {
  if (!last) return "never updated from here";
  return {
    running: `updating since ${last.at}`,
    updated: `updated ${last.at}`,
    failed: `failed ${last.at}`,
    cancelled: `cancelled ${last.at}`,
  }[last.state];
}

/** The facts about one rung of a model chain: who serves it, its price, its score. */
export function chainModelFacts(model: ChainModel): string[] {
  return [
    model.providers.join(", "),
    model.price ? `$${model.price.toFixed(6)}/tok` : "free",
    `score ${model.score.toFixed(3)}`,
  ];
}

// An install can take a few minutes on a cold cache; past this, something is wrong.
const UPDATE_WAIT_MS = 10 * 60_000;
const UPDATE_POLL_MS = 3000;

function UpdatePanel() {
  const status = useResource<UpdateStatus>("/web/api/update");
  const { confirm } = useDialog();
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState("");
  const last = status.data?.last ?? null;

  const update = async () => {
    const yes = await confirm(
      "North installs the latest version from GitHub and restarts. Anything running now is interrupted. This page reloads by itself when north is back, usually within a few minutes.",
      { title: "Update north?", confirmLabel: "Update and restart" },
    );
    if (!yes) return;
    setError("");
    try {
      await post("/web/api/update", {});
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return;
    }
    setWaiting(true);
    // North goes away while it installs, so a failed request here means "not back yet".
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

  return (
    <Panel
      title="Version"
      label="Updates"
      actions={
        <Button variant="primary" busy={waiting || last?.state === "running"} busyLabel="Updating…" onClick={update}>
          Update north
        </Button>
      }
    >
      <ListRow
        title="Running"
        description={`${status.data?.version || "–"}${status.data?.commit ? ` · ${status.data.commit}` : ""}`}
      />
      <ListRow title="Last update" description={lastUpdateText(last)} />
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
  const chains = useResource<PartChain[]>(
    "/orchestrator/inference/chains",
    20000,
  );
  const [expandedPool, setExpandedPool] = useState<string | null>(null);
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
        eyebrow="Runtime"
        title="System"
        subtitle="Whether North is healthy, how a model gets picked, and what it costs. Providers are in Settings."
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
      {/* What actually picks a model. This panel used to show "model pools" -
        capability buckets recomputed for the panel alone, left over from the
        router that was deleted. Grouping is not selection: routing ranks a
        chain per part of a task and walks it in order, which is what this is. */}
      <Panel title="How a model is picked" label="Live chain per part">
        <ResourceView resource={chains} isEmpty={(data) => !data.length}
          empty="Routing is not ready - the model catalog has not loaded yet.">
          {(data) => <div>{data.map((part) => {
            const open = expandedPool === part.part;
            return (
              <ListRow
                key={part.part}
                title={part.part}
                description={`${part.eligible} eligible · ranked by ${part.order_by.replace(/_/g, " ")}${part.requires.length ? ` · needs ${part.requires.join(", ")}` : ""}`}
                meta={[`1. ${part.models[0]?.model || "—"}`]}
                actions={
                  <Button size="sm" variant="quiet" aria-pressed={open} onClick={() => setExpandedPool(open ? null : part.part)}>
                    {open ? "Hide models" : "Show models"}
                  </Button>
                }
              >
                {open && (part.models.length
                  ? <div className="model-list">{part.models.map((model, index) => (
                    // Why north would walk past this rung right now.
                    <ListRow
                      key={model.model}
                      title={`${index + 1}. ${model.model}`}
                      muted={!model.available}
                      description={model.available ? undefined : `Skipped now: ${model.skipped_because}`}
                      meta={chainModelFacts(model)}
                      status={!model.available && <Status label="Skipped" tone="warn" />}
                    />
                  ))}</div>
                  : <Notice tone="warning">Nothing qualifies for this part right now.</Notice>)}
              </ListRow>
            );
          })}</div>}
        </ResourceView>
      </Panel>
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
          <ListRow title="Routing" description={settings?.routing || "–"} />
          {/* Only shown when it is in force: a remembered pin that auto has released
            would read here as the model north is using, which it is not. */}
          {settings?.model && <ListRow title="Pinned model" description={settings.model} />}
          <ListRow
            title="Power"
            description={settings?.routing === "manual" ? "not in use" : settings?.power || "–"}
          />
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
