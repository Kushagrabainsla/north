import { resolveTab } from "./tabs.js";
import { withDebugger, getBuffers } from "./cdp.js";

const LEVEL_ORDER = { log: 0, info: 1, warning: 2, error: 3 };

export async function readConsoleMessages({ tabId, sessionToken, pattern, level, limit = 200 }) {
  const tab = await resolveTab(tabId, sessionToken);
  await withDebugger(tab.id, async () => {}); // ensure capture domains are on
  const b = getBuffers(tab.id);

  let re = null;
  if (pattern) {
    try {
      re = new RegExp(pattern, "i");
    } catch (err) {
      throw new Error(`invalid pattern: ${err.message}`);
    }
  }
  const minLevel = LEVEL_ORDER[level || "log"] ?? 0;

  const messages = b.console.filter((m) => (LEVEL_ORDER[m.level] ?? 0) >= minLevel && (!re || re.test(m.text) || (m.url && re.test(m.url))));
  const trimmed = messages.slice(-limit);
  return {
    data: trimmed,
    text: trimmed.length
      ? trimmed.map((m) => `[${m.level}] ${m.text}${m.url ? `  (${m.url})` : ""}`).join("\n")
      : `no console messages captured yet for tab ${tab.id}${pattern ? ` matching ${pattern}` : ""} (capture covers the time TaskWindow has been driving this tab)`,
  };
}

export async function readNetworkRequests({ tabId, sessionToken, pattern, limit = 200 }) {
  const tab = await resolveTab(tabId, sessionToken);
  await withDebugger(tab.id, async () => {});
  const b = getBuffers(tab.id);

  let re = null;
  if (pattern) {
    try {
      re = new RegExp(pattern, "i");
    } catch (err) {
      throw new Error(`invalid pattern: ${err.message}`);
    }
  }

  const requests = [];
  for (const id of b.networkOrder) {
    const r = b.network.get(id);
    if (!r) continue;
    if (re && !re.test(r.url)) continue;
    requests.push(r);
  }
  const trimmed = requests.slice(-limit);
  return {
    data: trimmed.map(({ requestId, ...rest }) => rest),
    text: trimmed.length
      ? trimmed
          .map((r) => `${r.method} ${r.url} -> ${r.status ?? (r.error ? `FAILED (${r.error})` : "?")}${r.type ? ` [${r.type}]` : ""}`)
          .join("\n")
      : `no network requests captured yet for tab ${tab.id}${pattern ? ` matching ${pattern}` : ""}`,
  };
}
