const $ = (id) => document.getElementById(id);

function renderStatus(connected) {
  $("dot").className = `dot ${connected ? "on" : "off"}`;
  $("status").textContent = connected
    ? "Connected to the daemon — MCP clients can drive this browser."
    : "Not connected — waiting for the daemon.";
  $("setup").hidden = connected;
}

async function loadShortcuts() {
  const store = await chrome.storage.local.get("shortcuts");
  if (!store.shortcuts) {
    // Seed defaults (same as tools/shortcuts.js does on first use).
    const defaults = {
      screenshot: {
        description: "Screenshot the active tab",
        actions: [{ tool: "computer", params: { action: "screenshot" } }],
      },
      page_text: {
        description: "Extract the active tab's visible text",
        actions: [{ tool: "get_page_text", params: {} }],
      },
    };
    await chrome.storage.local.set({ shortcuts: defaults });
    return defaults;
  }
  return store.shortcuts;
}

async function init() {
  const { token = "", port = 9377, allowAllTabs = false, separateWindow = true } = await chrome.storage.local.get([
    "token", "port", "allowAllTabs", "separateWindow",
  ]);
  $("token").value = token;
  $("port").value = port;
  $("allowAllTabs").checked = allowAllTabs === true;
  $("separateWindow").checked = separateWindow !== false;

  try {
    const res = await chrome.runtime.sendMessage({ type: "taskwindow:getStatus" });
    renderStatus(res?.connected === true);
  } catch {
    renderStatus(false);
  }

  const registry = await loadShortcuts();
  $("shortcuts").value = JSON.stringify(registry, null, 2);
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "taskwindow:status") renderStatus(msg.connected === true);
});

$("guide").addEventListener("click", (event) => {
  event.preventDefault();
  chrome.tabs.create({ url: chrome.runtime.getURL("onboarding/onboarding.html") });
});

// Pairing: exchange the daemon's printed code for the real token.
$("pair").addEventListener("click", async () => {
  const status = $("pairStatus");
  const code = $("pairCode").value.trim().toUpperCase();
  const port = Number($("port").value) || 9377;
  if (!code) {
    status.style.color = "#dc2626";
    status.textContent = "✗ enter the pairing code";
    return;
  }
  status.style.color = "#666";
  status.textContent = "pairing…";
  try {
    const res = await fetch(`http://127.0.0.1:${port}/pair`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
    await chrome.storage.local.set({ token: body.token, port });
    status.style.color = "#16a34a";
    status.textContent = "paired ✓ — connecting…";
    $("token").value = body.token;
    setTimeout(() => (status.textContent = ""), 4000);
  } catch (err) {
    status.style.color = "#dc2626";
    status.textContent = `✗ ${err.message}`;
  }
  setTimeout(() => (status.textContent = ""), 6000);
});

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    token: $("token").value.trim(),
    port: Number($("port").value) || 9377,
    allowAllTabs: $("allowAllTabs").checked,
    separateWindow: $("separateWindow").checked,
  });
  $("saved").textContent = "saved ✓";
  setTimeout(() => ($("saved").textContent = ""), 2000);
});

$("save-shortcuts").addEventListener("click", async () => {
  try {
    const parsed = JSON.parse($("shortcuts").value);
    if (typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("must be a JSON object");
    for (const [name, sc] of Object.entries(parsed)) {
      if (!sc || !Array.isArray(sc.actions)) throw new Error(`"${name}" needs an actions array`);
      for (const a of sc.actions) {
        if (!a || typeof a.tool !== "string") throw new Error(`"${name}" has an action without a tool`);
      }
    }
    await chrome.storage.local.set({ shortcuts: parsed });
    $("saved-shortcuts").textContent = "saved ✓";
  } catch (err) {
    $("saved-shortcuts").textContent = `✗ ${err.message}`;
  }
  setTimeout(() => ($("saved-shortcuts").textContent = ""), 4000);
});

init();
