import { useState } from "react";
import { api, post } from "../api";
import { Empty, ErrorNotice, HealthIndicator, PageHeader, Panel, unpricedNote, YoloBadge } from "../components";
import { useDialog } from "../dialog";
import { useResource } from "../hooks";
import { PartChain } from "./Settings";

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
        <button className="primary-button" disabled={waiting || last?.state === "running"} onClick={update}>
          {waiting || last?.state === "running" ? "Updating…" : "Update north"}
        </button>
      }
    >
      <div className="list-row">
        <b>Running</b>
        <span>
          {status.data?.version || "–"}
          {status.data?.commit ? ` · ${status.data.commit}` : ""}
        </span>
      </div>
      <div className="list-row">
        <b>Last update</b>
        <span>{lastUpdateText(last)}</span>
      </div>
      {waiting && (
        <div className="notice">
          Updating. North stops, installs the new version and starts again. This page reloads when it is back.
        </div>
      )}
      {!waiting && last?.state === "failed" && last.detail && <ErrorNotice message={last.detail.slice(-800)} />}
      {error && <ErrorNotice message={error} />}
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
      <div className="metric-cards">
        <div>
          <span>Tasks · 30 days</span>
          <strong>{metrics.data?.total_tasks || 0}</strong>
        </div>
        <div>
          <span>Input tokens</span>
          <strong>
            {Number(metrics.data?.total_tokens_in || 0).toLocaleString()}
          </strong>
        </div>
        <div>
          <span>Output tokens</span>
          <strong>
            {Number(metrics.data?.total_tokens_out || 0).toLocaleString()}
          </strong>
        </div>
        <div>
          <span>Model cost · month</span>
          <strong>${totalCost.toFixed(4)}{unpricedNote(unpriced)}</strong>
        </div>
      </div>
        {/* What actually picks a model. This panel used to show "model pools" -
          capability buckets recomputed for the panel alone, left over from the
          router that was deleted. Grouping is not selection: routing ranks a
          chain per part of a task and walks it in order, which is what this is. */}
        <Panel title="How a model is picked" label="Live chain per part">
          <div>
            {(chains.data || []).map((part) => (
              <div className="model-pool" key={part.part}>
                <button
                  className="model-pool-toggle"
                  onClick={() =>
                    setExpandedPool(
                      expandedPool === part.part ? null : part.part,
                    )
                  }
                >
                  <span>
                    <b>{part.part}</b>
                    <small>
                      {part.eligible} eligible · ranked by{" "}
                      {part.order_by.replace(/_/g, " ")}
                      {part.requires.length
                        ? ` · needs ${part.requires.join(", ")}`
                        : ""}
                    </small>
                  </span>
                  <span className="pool-availability">
                    {expandedPool === part.part
                      ? "Hide"
                      : `1. ${part.models[0]?.model || "—"}`}
                  </span>
                </button>
                {expandedPool === part.part && (
                  <div className="model-list">
                    {part.models.map((model, index) => (
                      <div
                        className={
                          model.available
                            ? "chain-row"
                            : "chain-row chain-skipped"
                        }
                        key={model.model}
                      >
                        <span className="chain-rank">{index + 1}</span>
                        <b>{model.model}</b>
                        <span className="chain-meta">
                          {model.providers.join(", ")}
                          {" · "}
                          {model.price
                            ? `$${model.price.toFixed(6)}/tok`
                            : "free"}
                          {" · "}score {model.score.toFixed(3)}
                        </span>
                        {/* Why north would walk past this rung right now. */}
                        {!model.available && (
                          <em className="chain-why">{model.skipped_because}</em>
                        )}
                      </div>
                    ))}
                    {!part.models.length && (
                      <Empty>Nothing qualifies for this part right now.</Empty>
                    )}
                  </div>
                )}
              </div>
            ))}
            {!chains.loading && !(chains.data || []).length && (
              <Empty>
                Routing is not ready - the model catalog has not loaded yet.
              </Empty>
            )}
          </div>
        </Panel>
      <div className="two-column">
        <Panel title="Cost by model" label="Month to date">
          {Object.entries(costs.data?.by_model || {}).map(([name, value]) => (
            <div className="list-row" key={name}>
              <b>{name}</b>
              <span>${Number(value).toFixed(4)}</span>
            </div>
          ))}
          {!Object.keys(costs.data?.by_model || {}).length && (
            <Empty>No recorded inference costs yet.</Empty>
          )}
        </Panel>
        <Panel title="Runtime configuration">
          <div className="list-row">
            <b>Routing</b>
            <span>{overview.data?.settings?.routing || "–"}</span>
          </div>
          {/* Only shown when it is in force: a remembered pin that auto has released
            would read here as the model north is using, which it is not. */}
          {overview.data?.settings?.model && (
            <div className="list-row">
              <b>Pinned model</b>
              <span>{overview.data.settings.model}</span>
            </div>
          )}
          <div className="list-row">
            <b>Power</b>
            <span>
              {overview.data?.settings?.routing === "manual"
                ? "not in use"
                : overview.data?.settings?.power || "–"}
            </span>
          </div>
          <div className="list-row">
            <b>Autonomy</b>
            <span>
              {overview.data?.settings?.autonomy || "–"}
              <YoloBadge mode={overview.data?.settings?.autonomy} />
            </span>
          </div>
          {/* Depends on a binary north cannot install for you, so the row carries
            what to do about it rather than only that it is missing. */}
          <div className="list-row">
            <b>Browser</b>
            <span title={overview.data?.browser?.detail || ""}>
              {overview.data?.browser?.state || "–"}
              {overview.data?.browser?.state === "unavailable" && (
                <small className="row-hint">
                  {overview.data.browser.detail}
                </small>
              )}
            </span>
          </div>
          {/* MCP servers you configured: the ones that started, and below, anything configured that is not
            working. A server whose command did not exist was only a warning in north.log. */}
          {(overview.data?.mcp_servers || []).map((server: { name: string; tools: number }) => (
            <div className="list-row" key={`mcp-${server.name}`}>
              <b>MCP: {server.name}</b>
              <span>{server.tools} tool{server.tools === 1 ? "" : "s"}</span>
            </div>
          ))}
          {(overview.data?.not_working || []).map((why: string) => (
            <div className="list-row" key={why}>
              <b>Not working</b>
              <span className="row-hint">{why}</span>
            </div>
          ))}
          {/* The one model north runs itself. Worth naming: every stored vector is
            stamped with it, so "which embeddings am I on?" decides which memories
            can still be compared with which. */}
          <div className="list-row">
            <div>
              <b>Embeddings</b>
              <small>
                {overview.data?.embeddings?.local
                  ? "on-device · no data leaves this machine"
                  : overview.data?.embeddings?.provider || "not configured"}
              </small>
            </div>
            <span className="pool-availability">
              {overview.data?.embeddings?.model || "–"}
              {/* Whether they work: the model can be named and still fail to load. */}
              {overview.data?.embeddings?.state === "unavailable" && (
                <small className="row-hint">
                  Not working: {overview.data.embeddings.detail}. Skills
                  cannot be learned, and tool, code and memory search fall
                  back to keywords.
                </small>
              )}
            </span>
          </div>
        </Panel>
      </div>
      <UpdatePanel />
    </div>
  );
}

