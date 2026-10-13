/**
 * Extension-defined macro registry: named sequences of tool actions stored in
 * chrome.storage.local, seeded with defaults on first use. shortcuts_execute
 * runs each action through the same dispatcher browser_batch uses.
 */
const STORAGE_KEY = "shortcuts";

const DEFAULTS = {
  screenshot: {
    description: "Screenshot the active tab",
    actions: [{ tool: "computer", params: { action: "screenshot" } }],
  },
  page_text: {
    description: "Extract the active tab's visible text",
    actions: [{ tool: "get_page_text", params: {} }],
  },
};

async function load() {
  const store = await chrome.storage.local.get(STORAGE_KEY);
  if (store[STORAGE_KEY]) return store[STORAGE_KEY];
  await chrome.storage.local.set({ [STORAGE_KEY]: DEFAULTS });
  return DEFAULTS;
}

export async function shortcutsList() {
  const registry = await load();
  const names = Object.keys(registry);
  return {
    data: names.map((name) => ({ name, description: registry[name].description, steps: registry[name].actions?.length ?? 0 })),
    text: names.length
      ? names.map((n) => `${n}: ${registry[n].description} (${registry[n].actions?.length ?? 0} steps) — edit in the extension options page`).join("\n")
      : "no shortcuts defined",
  };
}

export async function shortcutsExecute({ name, sessionToken }, dispatch) {
  const registry = await load();
  const shortcut = registry[name];
  if (!shortcut) {
    throw new Error(`no shortcut named "${name}". Available: ${Object.keys(registry).join(", ") || "(none)"}`);
  }
  const results = [];
  for (let i = 0; i < shortcut.actions.length; i++) {
    const { tool, params } = shortcut.actions[i];
    try {
      const result = await dispatch(tool, { ...(params || {}), sessionToken });
      results.push({ step: i + 1, tool, ok: true, result });
    } catch (err) {
      throw new Error(`shortcut "${name}" failed at step ${i + 1} (${tool}): ${err.message}`);
    }
  }
  return {
    data: results.map((r) => ({ step: r.step, tool: r.tool, result: r.result.data ?? r.result.text })),
    text: results
      .map((r) => `${r.step}. ${r.tool}:\n${r.result.text ?? JSON.stringify(r.result.data ?? null, null, 2)}`)
      .join("\n\n"),
  };
}
