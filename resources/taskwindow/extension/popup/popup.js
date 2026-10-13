function render({ connected, version, groups }) {
  document.getElementById("dot").className = `dot ${connected ? "on" : "off"}`;
  document.getElementById("status").textContent = connected
    ? `Connected to the daemon (v${version})`
    : "Not connected — open Settings to repair the connection.";

  const list = document.getElementById("groups");
  list.innerHTML = "";
  if (!groups?.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = connected
      ? "No task groups yet — they appear when the agent creates tabs."
      : "Connect to see the agent's task groups.";
    list.appendChild(li);
    return;
  }
  for (const g of groups) {
    const li = document.createElement("li");
    li.className = "group";
    li.title = "Show this task group";
    li.addEventListener("click", async () => {
      try {
        await chrome.runtime.sendMessage({ type: "taskwindow:focusGroup", name: g.name, token: g.token });
      } catch {}
      window.close(); // focusing the window closes the popover anyway
    });
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = g.name;
    name.title = g.name;
    li.appendChild(name);
    if (g.current) {
      const b = document.createElement("span");
      b.className = "badge";
      b.textContent = "current";
      li.appendChild(b);
    }
    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${g.tabCount} tab${g.tabCount === 1 ? "" : "s"}`;
    li.appendChild(count);
    list.appendChild(li);
  }
}

async function refresh() {
  try {
    const res = await chrome.runtime.sendMessage({ type: "taskwindow:getSummary" });
    render(res || { connected: false, groups: [] });
  } catch {
    render({ connected: false, groups: [] });
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "taskwindow:status") refresh();
});

document.getElementById("options").addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

document.getElementById("adopt").addEventListener("click", async () => {
  const btn = document.getElementById("adopt");
  try {
    const win = await chrome.windows.getCurrent();
    const res = await chrome.runtime.sendMessage({ type: "taskwindow:adoptWindow", windowId: win.id });
    btn.textContent = res?.ok
      ? res.moved > 0
        ? `moved ${res.moved} task group${res.moved === 1 ? "" : "s"} here ✓`
        : "this window is the agent's now ✓"
      : `failed: ${res?.error || "unknown error"}`;
  } catch (err) {
    btn.textContent = `failed: ${err.message}`;
  }
  setTimeout(() => (btn.textContent = "Use this window for the agent"), 3000);
});

refresh();
