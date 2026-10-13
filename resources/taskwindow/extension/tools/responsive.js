/**
 * Responsive view: the verify-responsive harness, native.
 *
 * set_viewport renders the page in one fixed-size iframe per requested
 * viewport (an iframe's viewport is its own box — media queries and vw/vh
 * resolve exactly, several breakpoints side by side). Header-stripping DNR
 * rules scoped to the harness tab let most sites frame; viewports whose frame
 * still fails (frame-busters, cookie-partitioned logins, local files without
 * file access) fall back to a dedicated emulated tab. Every frame's real
 * innerWidth is asserted from inside the page — a screenshot alone proves
 * nothing.
 *
 * ONE VIEW PER SESSION. The harness and each viewport's fallback are opened
 * once and then reused: a later call re-points them, it never opens more. The
 * session record therefore tracks tab ids, never window ids — joining the
 * session's tab group moves a tab into that group's window (a group lives in
 * exactly one window), so the window a tab was born in is gone moments later,
 * and closing it would close nothing.
 *
 * Clearing closes the harness tab, the fallback tabs and the DNR rule.
 */
import { resolveTab, ensureTaskGroup, currentTaskName, normalizeToken } from "./tabs.js";
import { ensureAttached, send } from "./cdp.js";

const SESSION_KEY = "responsiveSession"; // per-agent-session suffix below
const HARNESS_MARKER = "responsive.html";
/** How long a frame gets to render before it is declared unframeable. Tests turn it down. */
export const TIMING = { frameDeadlineMs: 8000 };

const NEED_TOKEN =
  "set_viewport is scoped to your agent session: pass the sessionToken that tabs_create returned. " +
  "Without it there is no way to tell which responsive view is yours, so nothing was opened or closed.";

const sizeKey = (v) => `${v.width}x${v.height}`;

function dnrRule(ruleId, tabId) {
  return {
    id: ruleId,
    priority: 1,
    condition: { tabIds: [tabId], resourceTypes: ["sub_frame"] },
    action: {
      type: "modifyHeaders",
      responseHeaders: [
        { header: "X-Frame-Options", operation: "remove" },
        { header: "Content-Security-Policy", operation: "remove" },
      ],
    },
  };
}

/**
 * Session rule ids are one namespace for the whole extension, so a fixed id
 * would mean two concurrent agents silently overwriting each other's rule
 * (and the loser's frames failing for no visible reason). Take the next free
 * one and keep it in the session record.
 */
async function allocateRuleId() {
  const rules = await chrome.declarativeNetRequest.getSessionRules().catch(() => []);
  return rules.reduce((max, r) => Math.max(max, r.id), 0) + 1;
}

async function removeDnrRule(ruleId) {
  if (ruleId == null) return;
  try {
    await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [ruleId] });
  } catch {}
}

function sessionKey(token) {
  return `${SESSION_KEY}:${token}`;
}

async function tabAlive(tabId) {
  try {
    await chrome.tabs.get(tabId);
    return true;
  } catch {
    return false;
  }
}

/**
 * The stored view, minus anything the user has closed in the meantime. Keyed
 * on the harness tab: if that is gone the view is gone, whatever is left of
 * the fallbacks.
 */
async function loadSession(token) {
  const key = sessionKey(token);
  const stored = await chrome.storage.local.get(key);
  const s = stored[key];
  if (!s) return null;
  if (!(await tabAlive(s.tabId))) {
    await removeDnrRule(s.ruleId);
    await chrome.storage.local.remove(key); // the user closed the harness
    return null;
  }
  const fallbacks = [];
  for (const f of s.fallbacks || []) if (await tabAlive(f.tabId)) fallbacks.push(f);
  return { ...s, fallbacks };
}

function harnessUrlFor(targetUrl, vps) {
  return (
    chrome.runtime.getURL("responsive/responsive.html") +
    `?url=${encodeURIComponent(targetUrl)}&widths=${encodeURIComponent(JSON.stringify(vps))}`
  );
}

async function probeFrames(tabId) {
  const inj = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    func: () => ({ href: location.href, w: innerWidth, h: innerHeight, touch: navigator.maxTouchPoints }),
  });
  return inj
    .filter((r) => r.result && !String(r.result.href).includes(HARNESS_MARKER))
    .map((r) => ({ frameId: r.frameId, ...r.result }));
}

async function assertFrames(tabId, viewports) {
  const deadline = Date.now() + TIMING.frameDeadlineMs;
  for (;;) {
    let frames = [];
    try {
      frames = await probeFrames(tabId);
    } catch {}
    const found = viewports.map((v) => {
      const f = frames.find((fr) => fr.w === v.width && fr.h === v.height);
      return f ? { ...v, mode: "iframe", pageWidth: f.w, pageHeight: f.h, touch: f.touch, frameId: f.frameId } : null;
    });
    if (found.every(Boolean)) return found;
    if (Date.now() > deadline) {
      return viewports.map((v, i) => found[i] || { ...v, mode: "failed" });
    }
    await new Promise((r) => setTimeout(r, 400));
  }
}

/**
 * `<all_urls>` does not carry the file:// scheme — that is the per-extension
 * "Allow access to file URLs" toggle, and without it a file:// iframe inside
 * an extension page never loads. Worth asking before spending the frame
 * deadline waiting for something that cannot arrive.
 */
async function canFrame(targetUrl) {
  if (!/^file:/i.test(targetUrl)) return { ok: true };
  const allowed = await chrome.extension.isAllowedFileSchemeAccess().catch(() => false);
  return allowed
    ? { ok: true }
    : {
        ok: false,
        note:
          'this extension cannot read local files yet, so a file:// page will not load in a frame — ' +
          'enable "Allow access to file URLs" on the TaskWindow card in chrome://extensions to use the ' +
          "side-by-side view; each viewport is in its own emulated tab meanwhile",
      };
}

/** A tab in the session's group. Never a window: joining the group would move it out of one anyway. */
async function openInSessionGroup(url, taskName, token) {
  const tab = await chrome.tabs.create({ url, active: false });
  await ensureTaskGroup(tab.id, taskName, token, undefined, tab.windowId);
  return tab;
}

async function emulate(tabId, v) {
  await ensureAttached(tabId);
  await send(tabId, "Emulation.setDeviceMetricsOverride", {
    width: v.width,
    height: v.height,
    deviceScaleFactor: 1,
    mobile: true,
  });
  await send(tabId, "Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
}

export async function setViewport({ viewports, url, tabId, sessionToken } = {}) {
  const token = normalizeToken(sessionToken);
  if (token == null) throw new Error(NEED_TOKEN);

  if (!viewports?.length) {
    const s = await loadSession(token);
    await removeDnrRule(s?.ruleId);
    if (!s) {
      return { text: "nothing to close: this session has no responsive view open" };
    }
    // Tabs, not windows: the harness left the window it was created in the
    // moment it joined the session's group.
    for (const id of [s.tabId, ...s.fallbacks.map((f) => f.tabId)]) {
      try {
        await chrome.tabs.remove(id);
      } catch {}
    }
    await chrome.storage.local.remove(sessionKey(token));
    return { text: `closed the responsive view (${1 + s.fallbacks.length} tabs)` };
  }

  const vps = viewports.map((v) => ({ width: v.width, height: v.height }));
  const taskName = await currentTaskName(token);
  if (!taskName) {
    throw new Error("No task in this session yet — use tabs_create (with a task name) to open a tab first.");
  }

  let s = await loadSession(token);
  // What to render. An explicit url or tabId wins; otherwise an open view
  // keeps showing what it was showing. Re-resolving the session's active tab
  // on every call would eventually resolve to the view's own tabs — they join
  // the same group — and point the harness at itself.
  let targetUrl = url;
  if (!targetUrl && (tabId != null || !s)) {
    const src = await resolveTab(tabId, token);
    targetUrl = src.url || src.pendingUrl || "about:blank";
  }
  if (!targetUrl || String(targetUrl).includes(HARNESS_MARKER)) targetUrl = s?.url || "about:blank";

  if (!s) {
    const tab = await openInSessionGroup(harnessUrlFor(targetUrl, vps), taskName, token);
    s = { tabId: tab.id, url: targetUrl, viewports: vps, ruleId: await allocateRuleId(), fallbacks: [] };
  } else if (s.url !== targetUrl || sizesOf(s.viewports) !== sizesOf(vps)) {
    // The harness renders from its own query string, read once at load — so a
    // changed url or viewport set means re-pointing it, not asserting against
    // what it happens to still be showing.
    await chrome.tabs.update(s.tabId, { url: harnessUrlFor(targetUrl, vps) });
    s = { ...s, url: targetUrl, viewports: vps };
  }

  // Frames may be blocked by the site's framing headers; strip them for this
  // tab only (session rule), re-scoped on every call in case the tab moved.
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [s.ruleId],
    addRules: [dnrRule(s.ruleId, s.tabId)],
  });

  const framing = await canFrame(targetUrl);
  const results = framing.ok
    ? await assertFrames(s.tabId, vps)
    : vps.map((v) => ({ ...v, mode: "failed" }));

  // Each viewport that cannot be framed gets an emulated tab — reused across
  // calls, so iterating on a design re-points tabs instead of breeding them.
  const bySize = new Map(s.fallbacks.map((f) => [sizeKey(f), f]));
  const wanted = new Set();
  for (const r of results) {
    if (r.mode !== "failed") continue;
    const key = sizeKey(r);
    wanted.add(key);
    let fb = bySize.get(key);
    if (fb) await chrome.tabs.update(fb.tabId, { url: targetUrl });
    else {
      const tab = await openInSessionGroup(targetUrl, taskName, token);
      fb = { tabId: tab.id, width: r.width, height: r.height };
      bySize.set(key, fb);
    }
    await emulate(fb.tabId, r);
    Object.assign(r, {
      mode: "emulated tab",
      tabId: fb.tabId,
      pageWidth: r.width,
      pageHeight: r.height,
      touch: 5,
    });
  }
  // A viewport that is no longer asked for (or that frames fine now) keeps no tab.
  for (const [key, fb] of bySize) {
    if (wanted.has(key)) continue;
    try {
      await chrome.tabs.remove(fb.tabId);
    } catch {}
    bySize.delete(key);
  }
  s = { ...s, fallbacks: [...bySize.values()] };
  await chrome.storage.local.set({ [sessionKey(token)]: s });

  const { windowId } = await chrome.tabs.get(s.tabId);
  const lines = results.map(
    (r) => `${r.width}×${r.height} via ${r.mode} — page sees ${r.pageWidth}×${r.pageHeight}, maxTouchPoints=${r.touch}${r.tabId ? ` (tab ${r.tabId})` : ""}`
  );
  return {
    data: { harnessTabId: s.tabId, windowId, results },
    text:
      `responsive view open (${targetUrl}):\n` +
      lines.join("\n") +
      (framing.note ? `\nnote: ${framing.note}` : "") +
      `\nscreenshot the listed tabIds; pass no viewports to close`,
  };
}

/** Viewport sets compare by value: same sizes in the same order is the same view. */
function sizesOf(vps) {
  return (vps || []).map(sizeKey).join(",");
}
