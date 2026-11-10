import { useMemo } from "react";
import { useResource } from "../hooks";
import { EntityTable, ListRow, ResourceView, Status } from "../kit";
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

/** One line of the walk's table: a provider, how many endpoints, and what came back. */
export interface EndpointLine { key: string; provider: string; outcome: string; reason: string; detail: string }

/** The roll-ups as table lines, each provider's calls before what it skipped. */
export function endpointLines(rolls: ProviderRoll[]): EndpointLine[] {
  return rolls.flatMap((roll) => [
    ...roll.tried.map((row) => ({ key: `${roll.provider}:t:${row.label}`, provider: roll.provider, outcome: `${row.count} called`, reason: row.label, detail: row.detail })),
    ...roll.skipped.map((row) => ({ key: `${roll.provider}:s:${row.label}`, provider: roll.provider, outcome: `${row.count} skipped`, reason: row.label, detail: "" })),
  ]);
}

const COLUMNS = [
  { key: "provider", label: "Provider", width: "minmax(110px, 150px)" },
  { key: "outcome", label: "Endpoints", width: "90px" },
  { key: "reason", label: "Why" },
  { key: "detail", label: "What came back" },
];

export function DecisionCard({ decision }: { decision: RoutingDecision }) {
  const lines = useMemo(
    () => endpointLines(rollUp(decision.skipped || [])),
    [decision.skipped],
  );
  // The stored skip list is capped, so the honest total comes from the row's own
  // counters rather than from counting what happens to have been kept.
  const shown = (decision.skipped || []).length;
  const hidden = Math.max(0, decision.endpoints - shown);
  return (
    <ListRow
      title={decision.part}
      description={decision.chosen_model ? `answered by ${decision.chosen_model} · ${decision.chosen_provider}` : undefined}
      status={decision.chosen_model ? <Status value="answered" /> : <Status label="no model answered" tone="bad" />}
      meta={[
        `${decision.considered} models · ${decision.endpoints} endpoints · ${decision.attempted} called`,
        hidden > 0 && `…and ${hidden} more endpoints not listed`,
      ]}
    >
      {lines.length > 0 && <EntityTable
        label={`Endpoints for ${decision.part}`}
        columns={COLUMNS}
        rows={lines}
        rowKey={(line) => line.key}
        cell={(line, key) => {
          const text = line[key as keyof EndpointLine];
          return <span title={text}>{text}</span>;
        }}
      />}
    </ListRow>
  );
}

export function RoutingAttempts({ taskId }: { taskId: string }) {
  const resource = useResource<RoutingDecision[]>(
    `/web/api/routing/decisions?task_id=${encodeURIComponent(taskId)}`,
  );
  return (
    <ResourceView resource={resource} isEmpty={(decisions) => !decisions.length} empty="No routing record for this task.">
      {(decisions) => <div>{decisions.map((d) => (
        <DecisionCard decision={d} key={d.id} />
      ))}</div>}
    </ResourceView>
  );
}
