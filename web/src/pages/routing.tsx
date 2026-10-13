import { useMemo } from "react";
import { api } from "../api";
import { Empty, ErrorNotice, Loading } from "../components";
import { useResource } from "../hooks";
import type { RoutingDecision, RoutingSkip } from "../types";

// One provider's part in a routing walk: what north called and what came back,
// kept apart from what it never called. A walk touches hundreds of endpoints,
// so the rows are only readable once they are folded up this way.
export interface ProviderRoll {
  provider: string;
  tried: { label: string; count: number; detail: string }[];
  skipped: { label: string; count: number }[];
  total: number;
}

export function tally(skips: RoutingSkip[], label: (skip: RoutingSkip) => string) {
  const rows = new Map<
    string,
    { label: string; count: number; detail: string }
  >();
  for (const skip of skips) {
    const key = label(skip);
    const row = rows.get(key) || {
      label: key,
      count: 0,
      detail: skip.detail || "",
    };
    rows.set(key, {
      ...row,
      count: row.count + 1,
      detail: row.detail || skip.detail || "",
    });
  }
  return [...rows.values()].sort((a, b) => b.count - a.count);
}

export function rollUp(skips: RoutingSkip[]): ProviderRoll[] {
  const byProvider = new Map<string, RoutingSkip[]>();
  for (const skip of skips)
    byProvider.set(skip.provider, [
      ...(byProvider.get(skip.provider) || []),
      skip,
    ]);
  return [...byProvider]
    .map(([provider, rows]) => ({
      provider,
      tried: tally(
        rows.filter((r) => r.tried),
        (r) => `${r.status_code || "—"} · ${r.reason}`,
      ),
      skipped: tally(
        rows.filter((r) => !r.tried),
        (r) => r.reason,
      ).map(({ label, count }) => ({ label, count })),
      total: rows.length,
    }))
    .sort((a, b) => b.total - a.total);
}

export function DecisionCard({ decision }: { decision: RoutingDecision }) {
  const rolls = useMemo(
    () => rollUp(decision.skipped || []),
    [decision.skipped],
  );
  // The stored skip list is capped, so the honest total comes from the row's own
  // counters rather than from counting what happens to have been kept.
  const shown = (decision.skipped || []).length;
  const hidden = Math.max(0, decision.endpoints - shown);
  return (
    <div className="routing-card">
      <header>
        <b>{decision.part}</b>
        {decision.chosen_model ? (
          <span>
            answered by {decision.chosen_model} · {decision.chosen_provider}
          </span>
        ) : (
          <span className="routing-exhausted">no model answered</span>
        )}
        <small>
          {decision.considered} models · {decision.endpoints} endpoints ·{" "}
          {decision.attempted} called
        </small>
      </header>
      {rolls.map((roll) => (
        <div className="routing-provider" key={roll.provider}>
          <b>{roll.provider}</b>
          <div>
            {roll.tried.map((row) => (
              <div className="routing-line" key={`t${row.label}`}>
                <span className="routing-count">{row.count} called</span>
                <span title={row.label}>{row.label}</span>
                {row.detail && <em title={row.detail}>{row.detail}</em>}
              </div>
            ))}
            {roll.skipped.map((row) => (
              <div
                className="routing-line routing-untried"
                key={`s${row.label}`}
              >
                <span className="routing-count">{row.count} skipped</span>
                <span title={row.label}>{row.label}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
      {hidden > 0 && (
        <small className="routing-more">
          …and {hidden} more endpoints not listed
        </small>
      )}
    </div>
  );
}

export function RoutingAttempts({ taskId }: { taskId: string }) {
  const resource = useResource<RoutingDecision[]>(
    `/web/api/routing/decisions?task_id=${encodeURIComponent(taskId)}`,
  );
  if (resource.loading) return <Loading />;
  if (resource.error) return <ErrorNotice message={resource.error} />;
  const decisions = resource.data || [];
  if (!decisions.length) return <Empty>No routing record for this task.</Empty>;
  return (
    <div className="routing-detail">
      {decisions.map((d) => (
        <DecisionCard decision={d} key={d.id} />
      ))}
    </div>
  );
}

