/**
 * WebSocket connection to the local daemon (the extension dials in as a
 * client). Reconnects with backoff; a 1-minute alarm covers service-worker
 * restarts.
 */
import { ATTACHED, dropTabState } from "./cdp.js";
import { dropCursor } from "./human.js";
import { claimInstallerBootstrap, claimStorePairing } from "./bootstrap.js";

const DEFAULTS = { port: 9377, token: "" };

let ws = null;
let backoff = 1000;
let dispatchTool = null;
let version = "0.0.0";
let connected = false;
let inFlight = 0;
let lastToolActivity = 0;

function broadcastStatus() {
  chrome.runtime.sendMessage({ type: "taskwindow:status", connected }).catch(() => {});
}

async function settings() {
  const stored = await chrome.storage.local.get(["port", "token"]);
  if (!stored.token) {
    const bootstrapped = (await claimInstallerBootstrap()) || (await claimStorePairing(Number(stored.port) || DEFAULTS.port));
    if (bootstrapped) return bootstrapped;
  }
  return {
    port: Number(stored.port) || DEFAULTS.port,
    token: stored.token || DEFAULTS.token,
  };
}

async function handleToolCall(msg) {
  let result = null;
  let ok = true;
  inFlight++;
  lastToolActivity = Date.now();
  try {
    result = await dispatchTool(msg.tool, msg.params);
  } catch (err) {
    ok = false;
    result = { error: err?.message || String(err) };
  } finally {
    inFlight--;
    lastToolActivity = Date.now();
  }
  const payload = { type: "tool_result", id: msg.id, ok };
  if (ok) {
    payload.result = result ?? {};
  } else {
    payload.error = result.error;
  }
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
}

function scheduleReconnect() {
  setTimeout(connect, backoff);
  backoff = Math.min(backoff * 2, 30_000);
}

export function connectWs({ version: v, dispatchTool: dispatch } = {}) {
  dispatchTool = dispatch;
  version = v;

  chrome.alarms.create("taskwindow-reconnect", { periodInMinutes: 1 });
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "taskwindow-reconnect" && (!ws || ws.readyState === WebSocket.CLOSED)) connect();
  });

  chrome.tabs.onRemoved.addListener((tabId) => {
    dropTabState(tabId);
    dropCursor(tabId);
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && (changes.token || changes.port)) {
      if (ws) try { ws.close(); } catch {}
    }
  });

  connect();
}

export function isConnected() {
  return connected;
}

/**
 * No tool call running, and none for quietMs. A browser_batch reaches the
 * extension as separate calls, so a gap between two of them is not idle.
 */
export function isIdle(quietMs) {
  return inFlight === 0 && Date.now() - lastToolActivity >= quietMs;
}

async function connect() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
  const { port, token } = await settings();
  if (!token) {
    connected = false;
    broadcastStatus();
    scheduleReconnect(); // unconfigured: keep polling so we connect the moment settings are saved
    return;
  }

  let socket;
  try {
    socket = new WebSocket(`ws://127.0.0.1:${port}/ws?token=${encodeURIComponent(token)}`);
  } catch {
    scheduleReconnect();
    return;
  }
  ws = socket; // the active socket; handlers below close over their own `socket`

  // Application-level ping: the pong delivered to onMessage resets the MV3
  // service-worker idle timer, which transport-level pings don't. It doubles
  // as the liveness check: a ping still unanswered when the next one is due
  // means the daemon's end is gone (a laptop sleep can leave the socket
  // half-open, reading OPEN here forever). Drop it and dial a fresh one.
  let pingSentAt = 0;
  const keepalive = setInterval(() => {
    if (socket.readyState !== WebSocket.OPEN) return;
    if (pingSentAt) {
      clearInterval(keepalive);
      if (ws === socket) ws = null; // onclose below is then a no-op; connect() owns the state
      try { socket.close(); } catch {}
      if (connected) {
        connected = false;
        broadcastStatus();
      }
      connect();
      return;
    }
    pingSentAt = Date.now();
    socket.send(JSON.stringify({ type: "ping" }));
  }, 20_000);

  socket.onopen = () => {
    backoff = 1000;
    connected = true;
    broadcastStatus();
    socket.send(
      JSON.stringify({
        type: "hello",
        protocol: 1,
        version,
        id: chrome.runtime.id,
        userAgent: navigator.userAgent,
        attachedTabs: [...ATTACHED],
      })
    );
  };

  socket.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (msg.type === "pong") pingSentAt = 0;
    if (msg.type === "tool_call") handleToolCall(msg);
  };

  socket.onclose = async (event) => {
    clearInterval(keepalive);
    if (ws !== socket) return; // superseded: the live socket owns the connection state
    ws = null;
    if (connected) {
      connected = false;
      broadcastStatus();
    }
    // The daemon has a different token (reinstalled, or rotated on purpose).
    // Redialling with this one can never work; forgetting it lets the next
    // attempt pair again, on its own for the store build.
    if (event?.code === 4401) await chrome.storage.local.remove("token");
    scheduleReconnect();
  };

  socket.onerror = () => {
    try {
      socket.close();
    } catch {}
  };
}

/**
 * Reload the extension itself so Chrome re-reads the unpacked files
 * (`taskwindow update` calls this through the daemon; it is not an MCP tool).
 * The result is sent first — the reload tears this worker down — and Chrome
 * only objects to reloads repeated within a second, not to one.
 */
export async function reloadExtension() {
  setTimeout(() => chrome.runtime.reload(), 200);
  return { text: `extension ${version} reloading` };
}
