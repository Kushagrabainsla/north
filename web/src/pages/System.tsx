import { useState } from "react";
import { Empty, HealthIndicator, PageHeader, Panel, YoloBadge } from "../components";
import { useResource } from "../hooks";
import { PartChain } from "./Settings";


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
          <strong>${totalCost.toFixed(4)}</strong>
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
            </span>
          </div>
        </Panel>
      </div>
    </div>
  );
}

