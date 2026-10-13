/**
 * Tab access policy.
 *
 * Every tab the agent creates lands in a tab group named after the task it
 * belongs to (e.g. "Research", or "Research competitors" at most — one word
 * if possible, two at most) — the task name is required and is
 * the human-readable label of the group. Groups are scoped per agent
 * SESSION: tabs_create mints (or takes) a secret sessionToken and namespaces
 * that session's groups under it, so concurrent agents never share tabs even
 * when they pick the same task name. The token is the capability: tools see
 * and act only on the session's own groups. The user can widen access to all
 * tabs from the options page (allowAllTabs); the agent cannot change it.
 *
 * ONE SESSION, ONE GROUP. The group exists to hold every tab of one job, so
 * the name is asked for once and then fixed: a later tabs_create that names a
 * task joins the session's group anyway and says so. An agent calling the tool
 * has its attention on the sub-task in front of it ("check the popover"), not
 * on the job the user asked for, so any per-call naming path turns the group
 * into a group of one. Sessions still hold a map of groups because sessions
 * predating this rule (and the legacy namespace) have several; the read paths
 * keep serving all of them.
 */
const GROUPS_KEY = "agentGroups"; // { [sessionToken]: { [taskNameLower]: {groupId, lastUsed, longRunning} } }
const GROUP_TTL_MS = 30 * 24 * 60 * 60 * 1000; // reap long-running groups unused for 30 days
// A task the agent said would take under an hour is done an hour after its
// last use: the group closes itself instead of lingering for 30 days.
const SHORT_TASK_TTL_MS = 60 * 60 * 1000;
/** Idle time after which a group is reaped. Entries without the flag predate it and keep the long TTL. */
function groupTtlMs(entry) {
  return entry.longRunning === false ? SHORT_TASK_TTL_MS : GROUP_TTL_MS;
}
const REAP_ALARM = "taskwindow-reap-groups";
const CURRENT_TASK_KEY = "currentTask"; // { [sessionToken]: taskNameLower }
const LEGACY_GROUP_KEY = "agentTabGroupId";
const ALLOW_ALL_KEY = "allowAllTabs";
const SEPARATE_WINDOW_KEY = "separateWindow";
const DEFAULT_TASK = "TaskWindow"; // title of the pre-task-era fixed group (migration only)
/** Pinned anchor tab that keeps the shared agent window alive; outside every group, so no agent can close it. */
const workspaceUrl = () => chrome.runtime.getURL("workspace/workspace.html");

// chrome.storage has no transactions: serialize every read-modify-write so
// concurrent agent sessions can't clobber each other's registrations.
let storeQueue = Promise.resolve();
function serialized(fn) {
  const run = storeQueue.then(fn);
  storeQueue = run.then(
    () => {},
    () => {}
  );
  return run;
}

/**
 * Chrome's tab and tab-group calls can stall for tens of seconds right after
 * the machine wakes. Every tool passes through the serialized store queue, so
 * one stalled call there would hold every session's tools; bound them and say
 * what stalled instead. Timeouts are marked so callers never mistake one for
 * "the group is gone" and prune or recreate it.
 */
const CHROME_CALL_TIMEOUT_MS = 10_000;
function bounded(promise, what) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      const err = new Error(
        `Chrome did not answer ${what} within ${CHROME_CALL_TIMEOUT_MS / 1000}s — it may still be waking up; retry in a moment`
      );
      err.chromeTimeout = true;
      reject(err);
    }, CHROME_CALL_TIMEOUT_MS);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function policyAllowsAll() {
  const stored = await chrome.storage.local.get(ALLOW_ALL_KEY);
  return stored[ALLOW_ALL_KEY] === true;
}

/** Default on: agent tabs open in their own window, not the user's. */
async function policySeparateWindow() {
  const stored = await chrome.storage.local.get(SEPARATE_WINDOW_KEY);
  return stored[SEPARATE_WINDOW_KEY] !== false;
}

function normalizeToken(sessionToken) {
  return typeof sessionToken === "string" && sessionToken.trim()
    ? sessionToken.trim().slice(0, 100)
    : null;
}

// Formats an already-chosen task name (fresh or recalled from a stored group
// title) without re-judging its word count — recalling an existing group
// must never fail just because it predates the word-count rule below.
function normalizeTask(name) {
  const trimmed = String(name || "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 60);
  if (!trimmed) {
    throw new Error(
      'A task name is required: pass "task" describing what the tab group is about — one word if possible, two at most (e.g. "Research" or "Research competitors").'
    );
  }
  return trimmed;
}

// Enforced only where the caller is naming a NEW task group, not when
// recalling one that already exists (ensureTaskGroup, rememberedTask).
function requireShortTask(name) {
  const task = normalizeTask(name);
  const wordCount = task.split(" ").length;
  if (wordCount > 2) {
    throw new Error(
      `Task name "${task}" has ${wordCount} words — use one word if possible, two at most (e.g. "Research" or "Research competitors").`
    );
  }
  return task;
}

function isGroupEntry(v) {
  return v != null && typeof v === "object" && typeof v.groupId === "number";
}

/**
 * Read all session namespaces. Read-only: migrations are normalized in memory
 * and persisted by the next serialized write. v1 shapes (pre-sessions flat
 * group map, scalar current task) migrate into the "" legacy namespace,
 * which tools can't reach — the groups stay visible to the user until reaped.
 */
async function agentGroups() {
  const stored = await chrome.storage.local.get(GROUPS_KEY);
  const raw = stored[GROUPS_KEY] || {};
  const map = {};
  const values = Object.values(raw);
  if (values.length > 0 && values.every((v) => isGroupEntry(v) || typeof v === "number")) {
    map[""] = {};
    for (const [name, entry] of Object.entries(raw)) {
      map[""][name] =
        typeof entry === "number" ? { groupId: entry, lastUsed: Date.now() } : entry;
    }
  } else {
    for (const [token, tasks] of Object.entries(raw)) {
      if (tasks == null || typeof tasks !== "object") continue;
      const inner = {};
      for (const [name, entry] of Object.entries(tasks)) {
        if (typeof entry === "number") inner[name] = { groupId: entry, lastUsed: Date.now() };
        else if (isGroupEntry(entry)) inner[name] = entry;
      }
      if (Object.keys(inner).length) map[token] = inner;
    }
  }
  // Migrate the pre-task-era fixed group, if one still exists.
  if (Object.keys(map).length === 0) {
    const legacy = await chrome.storage.local.get(LEGACY_GROUP_KEY);
    const id = legacy[LEGACY_GROUP_KEY];
    if (id != null) {
      try {
        await chrome.tabGroups.get(id);
        map[""] = { [DEFAULT_TASK.toLowerCase()]: { groupId: id, lastUsed: Date.now() } };
      } catch {}
    }
  }
  return map;
}

async function saveAgentGroups(map) {
  await chrome.storage.local.set({ [GROUPS_KEY]: map });
}

async function currentTaskMap() {
  const stored = await chrome.storage.local.get(CURRENT_TASK_KEY);
  const raw = stored[CURRENT_TASK_KEY];
  if (raw == null) return {};
  if (typeof raw === "string") return { "": raw }; // v1 scalar → legacy "" session
  return raw && typeof raw === "object" ? raw : {};
}

async function currentTaskName(sessionToken) {
  const token = normalizeToken(sessionToken);
  if (token == null) return null;
  const map = await currentTaskMap();
  return map[token] || null;
}

/** Must run inside serialized() — reads, mutates, and writes the task map. */
async function writeCurrentTask(token, taskLower) {
  const map = await currentTaskMap();
  map[token] = taskLower;
  await chrome.storage.local.set({ [CURRENT_TASK_KEY]: map });
}

/** Record that a session's task group was just used (throttled write). */
async function touchGroup(token, nameLower) {
  if (token == null) return;
  await serialized(async () => {
    const all = await agentGroups();
    const entry = all[token]?.[nameLower];
    if (!entry) return;
    const now = Date.now();
    if (now - entry.lastUsed > 30_000) {
      entry.lastUsed = now;
      await saveAgentGroups(all);
    }
  });
}

/** Group ids this session may touch. Dead ids are pruned lazily. */
async function allowedGroupIds(sessionToken) {
  const token = normalizeToken(sessionToken);
  if (token == null) return { ids: [], map: {} };
  return serialized(async () => {
    const all = await agentGroups();
    const session = { ...(all[token] || {}) };
    const ids = [];
    for (const [name, entry] of Object.entries(session)) {
      try {
        await bounded(chrome.tabGroups.get(entry.groupId), "tabGroups.get");
        ids.push(entry.groupId);
      } catch (err) {
        if (err?.chromeTimeout) throw err;
        delete session[name];
      }
    }
    if (Object.keys(session).length !== Object.keys(all[token] || {}).length) {
      if (Object.keys(session).length) all[token] = session;
      else delete all[token];
      await saveAgentGroups(all);
    }
    return { ids, map: session };
  });
}

const NEED_SESSION =
  "Browser tools are scoped per agent session. Call tabs_create first — it returns a " +
  "sessionToken — and pass that sessionToken in every subsequent browser tool call to " +
  "act on your session's tabs.";

/**
 * Deliberately says nothing about *why* the tab is out of reach: it may belong
 * to another session, to the user, or not exist at all. Distinguishing those
 * would let an agent map the browser by probing tab ids, and the remedy is the
 * same in every case.
 */
function deniedError(tabId) {
  return new Error(
    `Access denied: tab ${tabId} is not in your session's tab groups. ` +
      `Use tabs_list to see your tabs, or tabs_create (with a task name and your sessionToken) ` +
      `to open one in your own session; the user can allow all tabs from the extension's options page.`
  );
}

/**
 * Record use of whichever session group holds `tab`, if any. Independent of
 * the access policy: with allowAllTabs the agent may address a group tab
 * without its token (or with one), and the reaper's idle clock must still
 * see that use — a short task's group closes an hour after its last use.
 */
async function touchGroupOfTab(tab) {
  if (tab.groupId == null || tab.groupId < 0) return;
  const all = await agentGroups();
  for (const [token, session] of Object.entries(all)) {
    for (const [name, entry] of Object.entries(session)) {
      if (entry.groupId === tab.groupId) return touchGroup(token, name);
    }
  }
}

async function assertAllowedTab(tab, sessionToken) {
  await touchGroupOfTab(tab);
  if (await policyAllowsAll()) return;
  const token = normalizeToken(sessionToken);
  if (token == null) throw new Error(NEED_SESSION);
  const { ids } = await allowedGroupIds(token);
  if (!ids.includes(tab.groupId)) throw deniedError(tab.id);
}

export async function resolveTab(tabId, sessionToken) {
  const token = normalizeToken(sessionToken);
  if (tabId != null) {
    let tab;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch (err) {
      // Under the default policy a missing tab and someone else's tab must look
      // identical, or the difference between the two errors reveals which ids
      // are live. With allowAllTabs there is nothing to hide, so say it plainly.
      if (!(await policyAllowsAll())) throw deniedError(tabId);
      throw err;
    }
    await assertAllowedTab(tab, token);
    return tab;
  }
  // Default target: the active tab of this session's current-task group, then
  // its other groups, then (if the user widened the policy) the active tab.
  if (token == null) {
    if (await policyAllowsAll()) {
      const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
      if (active) return active;
    }
    throw new Error(NEED_SESSION);
  }
  const { ids, map } = await allowedGroupIds(token);
  const current = await currentTaskName(token);
  const candidates = [];
  if (current && map[current] != null) candidates.push({ name: current, groupId: map[current].groupId });
  for (const [name, entry] of Object.entries(map)) {
    if (!candidates.some((c) => c.groupId === entry.groupId)) candidates.push({ name, groupId: entry.groupId });
  }
  for (const { name, groupId } of candidates) {
    const groupTabs = await chrome.tabs.query({ groupId });
    const active = groupTabs.find((t) => t.active);
    if (active) {
      await touchGroup(token, name);
      return active;
    }
    if (groupTabs.length > 0) {
      await touchGroup(token, name);
      return groupTabs[groupTabs.length - 1];
    }
  }
  if (await policyAllowsAll()) {
    const [active] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (active) return active;
  }
  throw new Error("No tabs in this session's tab groups yet — use tabs_create (with a task name) to open one.");
}

/**
 * Put a tab into the session's group for `task`, reusing an existing group
 * of the same name within the session (never a duplicate). Records the
 * session's current task. `longRunning` (boolean) sets the group's lifetime
 * (see groupTtlMs); when undefined the group keeps what it has.
 */
async function ensureTaskGroup(tabId, taskName, sessionToken, longRunning, windowId) {
  const token = normalizeToken(sessionToken);
  if (token == null) throw new Error(NEED_SESSION);
  const task = normalizeTask(taskName);
  return serialized(async () => {
    const all = await agentGroups();
    const session = { ...(all[token] || {}) };
    const key = task.toLowerCase();
    let groupId = session[key]?.groupId;
    const lifetime = (existing) => ({
      longRunning: typeof longRunning === "boolean" ? longRunning : existing?.longRunning,
    });
    if (groupId != null) {
      try {
        await bounded(chrome.tabGroups.get(groupId), "tabGroups.get");
        await bounded(chrome.tabs.group({ tabIds: [tabId], groupId }), "tabs.group");
        await bounded(chrome.tabGroups.update(groupId, { color: "blue" }), "tabGroups.update");
        session[key] = { groupId, lastUsed: Date.now(), ...lifetime(session[key]) };
        all[token] = session;
        await saveAgentGroups(all);
        await writeCurrentTask(token, key);
        return { groupId, task, longRunning: session[key].longRunning !== false };
      } catch (err) {
        if (err?.chromeTimeout) throw err;
        groupId = null; // group was closed; recreate below
      }
    }
    // A new group is born in Chrome's *current* window unless createProperties
    // says otherwise — and for a service worker that is the last-focused
    // window, i.e. the user's — and Chrome moves the tab there to join it.
    // Pin the group to the window the tab is already in.
    // Every caller has just created the tab and knows its window; ask Chrome
    // only if one didn't say. This runs inside the store queue, which every
    // session's tools share — one fewer call that can stall there after a wake.
    const wid = windowId ?? (await bounded(chrome.tabs.get(tabId), "tabs.get")).windowId;
    groupId = await bounded(
      chrome.tabs.group({ tabIds: [tabId], createProperties: { windowId: wid } }),
      "tabs.group"
    );
    await bounded(chrome.tabGroups.update(groupId, { title: task, color: "blue" }), "tabGroups.update");
    session[key] = { groupId, lastUsed: Date.now(), ...lifetime(null) };
    all[token] = session;
    await saveAgentGroups(all);
    await writeCurrentTask(token, key);
    return { groupId, task, longRunning: session[key].longRunning !== false };
  });
}

/** Surface a task group: focus its window and highlight its active tab. */
export async function focusGroup(name, sessionToken) {
  const key = String(name || "").toLowerCase();
  const token = normalizeToken(sessionToken);
  const all = await agentGroups();
  const ns =
    token != null && all[token]?.[key] != null
      ? token
      : Object.keys(all).find((t) => all[t][key] != null);
  if (ns == null) return false;
  const tabs = await chrome.tabs.query({ groupId: all[ns][key].groupId });
  if (tabs.length === 0) return false;
  const active = tabs.find((t) => t.active) ?? tabs[tabs.length - 1];
  await chrome.tabs.update(active.id, { active: true });
  await chrome.windows.update(tabs[0].windowId, { focused: true });
  return true;
}

/**
 * Make `windowId` the agent's home window: move every session's task groups
 * into it (several groups can share one window). Future tabs follow their
 * group, so they land here too.
 */
export async function adoptWindow(windowId) {
  const all = await agentGroups();
  let moved = 0;
  for (const [token, session] of Object.entries(all)) {
    for (const [name, entry] of Object.entries(session)) {
      const gid = entry.groupId;
      let tabs = [];
      try {
        tabs = await chrome.tabs.query({ groupId: gid });
      } catch {
        continue;
      }
      if (tabs.length === 0) continue;
      const tabIds = tabs.map((t) => t.id);
      // index: -1 = append at the end of the destination window (Chrome requires
      // an explicit index when moving an array of tabs).
      await chrome.tabs.move(tabIds, { windowId, index: -1 });
      try {
        // Moving between windows can dissolve the group — re-form it here.
        await chrome.tabs.group({ tabIds, groupId: gid });
      } catch {
        const fresh = await chrome.tabs.group({ tabIds, createProperties: { windowId } });
        await chrome.tabGroups.update(fresh, { title: name, color: "blue" });
        session[name].groupId = fresh;
      }
      await touchGroup(token, name);
      moved++;
    }
  }
  if (moved > 0) await saveAgentGroups(all);
  await moveAnchorTo(windowId);
  return moved;
}

/**
 * Put the anchor in `windowId`, so the window the user picked is the one new
 * tasks open in. Moving it empties the old agent window, which Chrome then
 * closes — the point of adopting. A stray anchor left over elsewhere (the user
 * reopened a closed window) loses the tie in agentWindowId: the groups are here.
 */
async function moveAnchorTo(windowId) {
  const anchored = await anchorTabs();
  if (anchored.some((t) => t.windowId === windowId)) return;
  const anchor = anchored[0];
  if (anchor) {
    await chrome.tabs.move(anchor.id, { windowId, index: -1 });
  } else {
    const tab = await chrome.tabs.create({ url: workspaceUrl(), windowId, active: false });
    anchored.push(tab);
    await chrome.tabs.update(tab.id, { pinned: true });
    return;
  }
  // A cross-window move can drop the pin; the anchor is only an anchor pinned.
  await chrome.tabs.update(anchor.id, { pinned: true });
}

/** Summary for the toolbar popover: agent task groups that still have tabs. */
export async function groupsSummary() {
  const all = await agentGroups();
  const currentMap = await currentTaskMap();
  let recentToken = null;
  let recentTs = -1;
  for (const [token, session] of Object.entries(all)) {
    for (const entry of Object.values(session)) {
      if (entry.lastUsed > recentTs) {
        recentTs = entry.lastUsed;
        recentToken = token;
      }
    }
  }
  const currentTask = recentToken != null ? currentMap[recentToken] : null;
  const groups = [];
  for (const [token, session] of Object.entries(all)) {
    for (const [name, entry] of Object.entries(session)) {
      try {
        const tabs = await chrome.tabs.query({ groupId: entry.groupId });
        if (tabs.length > 0) {
          groups.push({
            name,
            token,
            tabCount: tabs.length,
            windowId: tabs[0].windowId,
            current: token === recentToken && name === currentTask,
          });
        }
      } catch {}
    }
  }
  return groups;
}

/**
 * Reap task groups no session has used in their TTL (an hour for tasks the
 * agent said take under an hour, 30 days otherwise). Safety rails: only
 * group ids in our own registry are ever touched (never the user's or
 * another extension's groups), and a group is skipped while any of its tabs
 * is the active tab in its window — the user may be reading it.
 */
export async function reapIdleGroups() {
  return serialized(async () => {
    const all = await agentGroups();
    const now = Date.now();
    let reaped = 0;
    for (const session of Object.values(all)) {
      for (const [name, entry] of Object.entries(session)) {
        let groupTabs = [];
        try {
          groupTabs = await chrome.tabs.query({ groupId: entry.groupId });
        } catch {
          delete session[name];
          continue;
        }
        if (groupTabs.length === 0) {
          delete session[name];
          continue;
        }
        const userReadingIt = groupTabs.some((t) => t.active);
        if (userReadingIt || now - entry.lastUsed < groupTtlMs(entry)) continue;
        try {
          await chrome.tabs.remove(groupTabs.map((t) => t.id));
          reaped++;
        } catch {}
        delete session[name];
      }
    }
    for (const token of Object.keys(all)) {
      if (Object.keys(all[token]).length === 0) delete all[token];
    }
    await saveAgentGroups(all);
    return reaped;
  });
}

export function initGroupReaper() {
  chrome.alarms.create(REAP_ALARM, { periodInMinutes: 5 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === REAP_ALARM) reapIdleGroups().catch(() => {});
  });
}

export { ensureTaskGroup, currentTaskName, normalizeToken };

export async function tabsList({ sessionToken } = {}) {
  const token = normalizeToken(sessionToken);
  const allowAll = await policyAllowsAll();
  if (!allowAll && token == null) throw new Error(NEED_SESSION);

  const { ids, map } = await allowedGroupIds(token);
  const tabs = allowAll
    ? await chrome.tabs.query({})
    : ids.length
      ? (await Promise.all(ids.map((gid) => chrome.tabs.query({ groupId: gid })))).flat()
      : [];

  const titleFor = (t) => {
    if (allowAll && t.groupId == null) return "";
    const entry = Object.entries(map).find(([, e]) => e.groupId === t.groupId);
    return entry ? entry[0] : "";
  };

  return {
    data: tabs.map((t) => ({
      id: t.id,
      title: t.title,
      url: t.url || t.pendingUrl || "",
      active: t.active,
      windowId: t.windowId,
      task: titleFor(t) || undefined,
    })),
    text:
      tabs.length > 0
        ? tabs
            .map((t) => {
              const task = titleFor(t);
              return `${t.id}. ${t.title || "(untitled)"} — ${t.url || t.pendingUrl || "about:blank"}${t.active ? "  [active]" : ""}${task ? `  [task: ${task}]` : ""}`;
            })
            .join("\n")
        : "no tabs in this session's groups yet — use tabs_create (with a task name) to open one",
  };
}

/**
 * Chrome sometimes raises a window as a side effect of tab creation, even for
 * background tabs. If focus moved and we didn't ask for it, hand it back.
 * Best effort: works between Chrome windows; can't restore focus to a
 * non-Chrome app the user was working in. Only when Chrome is frontmost
 * (`focused`): if the user is in another app, nothing visible moved, and
 * focusing a Chrome window then would pull them out of that app.
 */
async function restoreFocusIfStolen(previousWindowId) {
  if (previousWindowId == null) return;
  try {
    const { id, focused } = await chrome.windows.getLastFocused();
    if (focused && id !== previousWindowId) await chrome.windows.update(previousWindowId, { focused: true });
  } catch {}
}


/**
 * The window already hosting the agent's work, or null if there is none.
 *
 * A Chrome window holds many tab groups, so every task joins one shared agent
 * window rather than opening its own — concurrent agents included.
 *
 * The pinned workspace anchor is what identifies that window. Where a group
 * happens to sit is only an inference, and a bad one: anything that moves a
 * group (Chrome moving a tab to join a group in another window, a drag, a
 * window merge on restore) silently redefined which window was the agent's,
 * and every later task followed it there — into the user's window. The anchor
 * is a fact, and adoptWindow moves it when the user picks a window instead.
 *
 * Groups still speak when the anchor cannot: they break a tie between several
 * anchored windows (the agent's is the one with live work, most recently used
 * first) and stand in when no anchor is left at all.
 */
async function agentWindowId() {
  const anchored = await anchorTabs();
  if (anchored.length === 1) return anchored[0].windowId;

  const all = await agentGroups();
  const entries = Object.values(all)
    .flatMap((session) => Object.values(session))
    .sort((a, b) => b.lastUsed - a.lastUsed);
  for (const { groupId } of entries) {
    try {
      const tabs = await chrome.tabs.query({ groupId });
      if (tabs.length === 0) continue;
      const { windowId } = tabs[0];
      // With no anchor anywhere, live work is the best evidence there is.
      if (anchored.length === 0) return windowId;
      if (anchored.some((t) => t.windowId === windowId)) return windowId;
    } catch {}
  }
  return anchored[0]?.windowId ?? null;
}

/**
 * The pinned workspace tabs, one per window that has ever anchored the agent.
 * Not query({url}): match patterns only cover http(s)/file, so a
 * chrome-extension URL there matches nothing. `pinned` is a plain filter;
 * compare URLs ourselves.
 */
async function anchorTabs() {
  try {
    const pinned = await chrome.tabs.query({ pinned: true });
    return pinned.filter((t) => t.url === workspaceUrl());
  } catch {
    return [];
  }
}

/**
 * A tabs_create whose result never made it back (the daemon gave up waiting
 * while Chrome was slow to wake) still opened its tab — and the agent, having
 * no result, calls again. Without a token a retry and a second agent look the
 * same, so the daemon mints the session token on the first call and names it
 * in the timeout error: the retry with the same token, task and url is then
 * provably the same caller and gets the tab the first call opened — whether
 * that call is still in flight or finished within the last minute — instead
 * of a second tab in a second group. The token is the capability: without one
 * the key is unique per call and nothing is coalesced.
 */
const createsInFlight = new Map(); // key -> Promise<result>
const createsDone = new Map(); // key -> { result, at }
const RETRY_WINDOW_MS = 60_000;

function replayed(result) {
  return {
    ...result,
    data: { ...result.data, replayed: true },
    text:
      `${result.text}\nnote: an identical tabs_create from this session already opened this tab within the last minute ` +
      "(its result may not have reached you) — no second tab was opened",
  };
}

/**
 * The task group a session is working in, by display title, or null if it has
 * none yet. The current-task map stores the lowercased key, so the title comes
 * from the group itself.
 */
async function rememberedTask(token) {
  const current = await currentTaskName(token);
  const entry = current ? (await agentGroups())[token]?.[current] : null;
  if (!entry) return null;
  const title = await chrome.tabGroups.get(entry.groupId).then((g) => g?.title, () => null);
  return normalizeTask(title || current);
}

export async function tabsCreate({ url, task, longRunning, sessionToken } = {}) {
  const token = normalizeToken(sessionToken) || crypto.randomUUID();
  // The task names the session's one group, so it is read once and then fixed:
  // once the session has a group, every later tab joins it and a `task` given
  // anyway is reported back, not applied (see the ONE SESSION, ONE GROUP note
  // at the top). Only the name that will actually be used is validated.
  const remembered = await rememberedTask(token);
  const named = String(task ?? "").trim();
  const creating = remembered == null; // this call names the session's group
  if (creating && !named) {
    throw new Error(
      'This session has no task group yet, so "task" is required: pass a name for the whole job you are doing for the user — not the page you are about to open — one word if possible, two at most (e.g. "Research" or "Research competitors"). ' +
        "Every later tab of this session joins that group; omit it from now on."
    );
  }
  // Naming the group is the moment the agent knows how long the job will take,
  // so the estimate is required right there. Later calls need none, and may
  // pass one on its own to revise the group (a job that turned out longer).
  if (creating && typeof longRunning !== "boolean") {
    throw new Error(
      '"longRunning" is required when you pass "task": true if this task might need more than an hour to complete, false if not. ' +
        "A group for a task under an hour closes itself once it has been idle for an hour."
    );
  }
  if (longRunning !== undefined && typeof longRunning !== "boolean") {
    throw new Error('"longRunning" must be true or false');
  }
  const taskUsed = remembered ?? requireShortTask(task);
  // Re-passing the group's own name is not an ignored name: it is the name.
  const renamed = !creating && named ? normalizeTask(task) : null;
  const ignoredTask = renamed != null && renamed.toLowerCase() !== taskUsed.toLowerCase() ? renamed : null;
  // An ignored name takes its answer with it: it was given for the sub-task the
  // agent thought it was starting, so honouring it would let a stray name cut
  // the whole job's group down to the one-hour lifetime. `longRunning` alone
  // still revises the group — that one is unambiguous.
  const droppedLongRunning = ignoredTask != null && typeof longRunning === "boolean";
  const longRunningUsed = droppedLongRunning ? undefined : longRunning;
  const key = [token, taskUsed.toLowerCase(), String(url || "")].join("\n");

  // The note is about THIS call, so it is added outside the coalescing cache:
  // a replayed result must not inherit the previous caller's naming.
  const withNote = (result) => {
    if (ignoredTask == null) return result;
    return {
      ...result,
      data: { ...result.data, ignoredTask },
      text:
        `${result.text}\nnote: this session's tab group is "${taskUsed}" and holds every tab of this job, ` +
        `so "${ignoredTask}" was not used as a name and no second group was made — omit "task" from now on` +
        (droppedLongRunning
          ? `; "longRunning" was not applied either, since it answered for "${ignoredTask}" rather than this group — pass it without "task" to change this group's answer`
          : ""),
    };
  };

  const inFlight = createsInFlight.get(key);
  if (inFlight) return withNote(replayed(await inFlight));
  const done = createsDone.get(key);
  if (done && Date.now() - done.at < RETRY_WINDOW_MS && (await chrome.tabs.get(done.result.data.id).then(() => true, () => false))) {
    return withNote(replayed(done.result));
  }
  createsDone.delete(key);

  const run = openInTaskGroup({ url, taskUsed, token, longRunning: longRunningUsed });
  createsInFlight.set(key, run);
  try {
    const result = await run;
    for (const [k, v] of createsDone) if (Date.now() - v.at >= RETRY_WINDOW_MS) createsDone.delete(k);
    createsDone.set(key, { result, at: Date.now() });
    return withNote(result);
  } finally {
    createsInFlight.delete(key);
  }
}

async function openInTaskGroup({ url, taskUsed, token, longRunning }) {
  const startedAt = Date.now();
  const separateWindow = await policySeparateWindow();
  const all = await agentGroups();
  const existingGroupId = all[token]?.[taskUsed.toLowerCase()]?.groupId;

  // Always a background tab: `tabs.create` defaults to active, which would
  // switch what the window shows. A background tab still renders and takes
  // input, so nothing ever needs to bring it forward.
  let tab;
  let createdNewWindow = false;
  const previouslyFocused = (await chrome.windows.getLastFocused().catch(() => null))?.id ?? null;

  if (separateWindow) {
    // Work in the agent's window, never the user's: this task's own group if it
    // already has one, else whatever window the agent is already using, else a
    // new window on first use.
    let windowId = null;
    if (existingGroupId != null) {
      try {
        const groupTabs = await chrome.tabs.query({ groupId: existingGroupId });
        if (groupTabs.length > 0) windowId = groupTabs[0].windowId;
      } catch {}
    }
    if (windowId == null) windowId = await agentWindowId();
    if (windowId != null) {
      tab = await chrome.tabs.create({ url, active: false, windowId });
    } else {
      // First use: a fresh agent window, anchored by a pinned tab outside any
      // group. Chrome drops a window with its last tab, and one task finishing
      // must not take the shared window (and wherever the user put it) away
      // from every other agent — and no agent can close a tab it can't reach.
      // Sized so a 1:1 CSS-pixel screenshot stays under the vision models'
      // native resolution (~1568px long edge): a wider viewport is downscaled
      // by the model API, and the coordinates it reads off the image drift.
      // Chrome clamps to the screen; the user may resize the window later.
      const win = await chrome.windows.create({ url: workspaceUrl(), focused: false, width: 1280, height: 900 });
      const anchor = win.tabs?.[0];
      if (!anchor) throw new Error("window was created but Chrome returned no tab");
      await chrome.tabs.update(anchor.id, { pinned: true });
      tab = await chrome.tabs.create({ url, active: false, windowId: win.id });
      createdNewWindow = true;
    }
    // The agent window never takes focus. The user is in their own window or
    // another app entirely; Chrome sometimes raises a window on tab creation
    // regardless, so hand focus straight back.
    await restoreFocusIfStolen(previouslyFocused);
  } else {
    tab = await chrome.tabs.create({ url, active: false });
  }
  const createdAt = Date.now();

  // A tab is only ever the agent's inside a task group: outside one it is
  // reachable by no session and shows in no popover list. If grouping fails
  // (or stalls, see bounded), close the tab again rather than strand it.
  let groupId, taskName, isLongRunning;
  try {
    ({ groupId, task: taskName, longRunning: isLongRunning } = await ensureTaskGroup(tab.id, taskUsed, token, longRunning, tab.windowId));
  } catch (err) {
    await chrome.tabs.remove(tab.id).catch(() => {});
    throw new Error(`could not put the new tab in the "${taskUsed}" group (${err.message}); closed it again — retry`);
  }
  const timing = { createMs: createdAt - startedAt, groupMs: Date.now() - createdAt };

  // Agents open the same page again when they've lost track of the tab they
  // already have, and nothing told them. Don't dedupe silently (two tabs of one
  // URL is sometimes deliberate) — point at the existing tab instead.
  const sameUrl = (u) => String(u || "").split("#")[0];
  const duplicates = (await chrome.tabs.query({ groupId }))
    .filter((t) => t.id !== tab.id && sameUrl(t.url || t.pendingUrl) === sameUrl(url))
    .map((t) => t.id);

  return {
    data: {
      id: tab.id,
      title: tab.title,
      url: tab.url || url,
      groupId,
      task: taskName,
      longRunning: isLongRunning,
      sessionToken: token,
      newWindow: createdNewWindow,
      alreadyOpenIn: duplicates,
      timing,
    },
    text:
      `opened tab ${tab.id} in the "${taskName}" group${createdNewWindow ? " in a new window" : ""}: ${tab.url || url}` +
      ` — sessionToken ${token}: pass it as "sessionToken" in every subsequent browser tool call` +
      (isLongRunning ? "" : " (short task: this group closes itself after an hour idle)") +
      (duplicates.length
        ? `\nnote: this group already had ${duplicates.length === 1 ? "tab" : "tabs"} ${duplicates.join(", ")} at this URL — next time use reload or navigate on that tab instead of opening another`
        : ""),
  };
}

export async function tabsClose({ tabId, sessionToken } = {}) {
  // Via resolveTab, not a bare tabs.get: it is the one place that looks a tab
  // up and checks it, so closing cannot leak which ids exist.
  const tab = await resolveTab(tabId, sessionToken);
  await chrome.tabs.remove(tab.id);
  return { text: `closed tab ${tab.id}` };
}

/** Resolve true once the tab fires its load event, false after `ms` — so agents don't race page loads. */
function waitForLoad(tabId, ms = 10_000) {
  return new Promise((resolve) => {
    const timeout = setTimeout(() => cleanup(false), ms);
    function listener(id, info) {
      if (id === tabId && info.status === "complete") cleanup(true);
    }
    function cleanup(didLoad) {
      clearTimeout(timeout);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(didLoad);
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

/** Shared tail of navigate/reload: the tab's state once the load settled (or the tab vanished). */
async function loadOutcome(tabId, settled, verb) {
  let fresh;
  try {
    fresh = await chrome.tabs.get(tabId);
  } catch {
    return { text: `tab ${tabId} closed during ${verb}` };
  }
  return {
    data: { tabId, url: fresh.url, title: fresh.title, loadTimedOut: !settled },
    text: `tab ${tabId} now at ${fresh.url}${fresh.title ? ` — "${fresh.title}"` : ""}${!settled ? " (load event did not fire within 10s; the page may still be loading)" : ""}`,
  };
}

export async function navigate({ url, tabId, sessionToken } = {}) {
  const tab = await resolveTab(tabId, sessionToken);
  await chrome.tabs.update(tab.id, { url });
  const settled = await waitForLoad(tab.id);
  return loadOutcome(tab.id, settled, `navigation to ${url}`);
}

export async function reload({ tabId, sessionToken, bypassCache = false } = {}) {
  const tab = await resolveTab(tabId, sessionToken);
  await chrome.tabs.reload(tab.id, { bypassCache: !!bypassCache });
  const settled = await waitForLoad(tab.id);
  return loadOutcome(tab.id, settled, "reload");
}
