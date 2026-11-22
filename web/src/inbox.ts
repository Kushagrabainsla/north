import { useResource } from "./hooks";
import type { Approval, Notice } from "./types";

/** A flow run as the Inbox needs it: one that stopped and waits for you to resume or discard it. */
export interface StoppedRun {
  run_id: string;
  flow: string;
  status: string;
  error: string;
  started_at: string;
  updated_at: string;
  current_step: number;
  total_steps: number;
}

/** A flow run stops for you when it is paused or waiting; it then needs a Resume or a Discard. */
export const runNeedsYou = (run: { status: string }) => run.status === "paused" || run.status === "waiting";

/** Oldest first: the item that waited longest is the one most likely forgotten. */
export function oldestFirst<T>(items: T[], when: (item: T) => string): T[] {
  return [...items].sort((a, b) => new Date(when(a)).getTime() - new Date(when(b)).getTime());
}

/** How long something has waited, in words: "waiting 2 days". */
export function waitingFor(since: string | number): string {
  const date = typeof since === "number" ? new Date(since * 1000) : new Date(since);
  const hours = Math.max(0, (Date.now() - date.getTime()) / 3_600_000);
  if (hours < 1) return "waiting under 1 h";
  if (hours < 48) return `waiting ${Math.floor(hours)} h`;
  return `waiting ${Math.floor(hours / 24)} days`;
}

/** More than a day is long enough to forget: the badge turns amber. */
export const waitedLong = (since: string | number) => {
  const date = typeof since === "number" ? new Date(since * 1000) : new Date(since);
  return Date.now() - date.getTime() > 86_400_000;
};

/**
 * Everything the Inbox holds, from one place, so the sidebar badge, Home and the Inbox page
 * show the same numbers: cards that wait for a decision, runs that wait for a resume, and
 * notices that only tell you something.
 */
export function useInbox(poll = 10000) {
  const approvals = useResource<Approval[]>("/web/api/approvals", poll);
  const notices = useResource<Notice[]>("/web/api/notices", poll);
  const runs = useResource<StoppedRun[]>("/web/api/flow-runs?limit=100", poll);
  const cards = oldestFirst((approvals.data || []).filter(card => card.status === "pending"), card => card.created_at);
  const stopped = oldestFirst((runs.data || []).filter(runNeedsYou), run => run.updated_at || run.started_at);
  const info = notices.data || [];
  const oldest = [...cards.map(card => card.created_at), ...stopped.map(run => run.updated_at || run.started_at)]
    .sort((a, b) => new Date(a).getTime() - new Date(b).getTime())[0];
  return {
    approvals, notices, runs, cards, stopped, info,
    needsYou: cards.length + stopped.length,
    longWait: oldest ? waitedLong(oldest) : false,
    reload: async () => { await Promise.all([approvals.reload(), notices.reload(), runs.reload()]); },
  };
}
