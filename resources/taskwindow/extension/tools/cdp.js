/**
 * Shared low-level chrome.debugger (CDP) helpers plus per-tab console/network
 * capture buffers.
 *
 * Attachment is kept alive once a tab is first driven (that's what powers
 * read_console_messages / read_network_requests); the "started debugging this
 * browser" infobar that Chrome shows while attached is a platform constraint.
 */

export const ATTACHED = new Set();

const BUFFER_CAP = 500;
/** tabId -> { console: [], network: Map(requestId -> entry), networkOrder: [] } */
const buffers = new Map();

function buf(tabId) {
  let b = buffers.get(tabId);
  if (!b) {
    b = { console: [], network: new Map(), networkOrder: [] };
    buffers.set(tabId, b);
  }
  return b;
}

export function getBuffers(tabId) {
  return buf(tabId);
}

export function dropTabState(tabId) {
  ATTACHED.delete(tabId);
  buffers.delete(tabId);
}

function levelOf(consoleType) {
  switch (consoleType) {
    case "error": return "error";
    case "warning": return "warning";
    case "info": case "log": case "debug": case "dir": case "table": case "trace":
      return consoleType === "debug" || consoleType === "trace" ? "log" : "info";
    default: return "log";
  }
}

function argPreview(arg) {
  if (!arg || typeof arg !== "object") return String(arg);
  switch (arg.type) {
    case "string": return arg.value ?? "";
    case "number": case "boolean": case "undefined": case "symbol": case "bigint":
      return String(arg.value);
    case "function": return "ƒ ()";
    case "object": {
      if (arg.subtype === "null") return "null";
      if (arg.subtype === "regexp") return arg.value?.description || "/.../";
      const preview = arg.preview;
      if (preview) {
        const props = preview.properties
          .slice(0, 12)
          .map((p) => `${p.name}: ${p.value ?? ""}`)
          .join(", ");
        return preview.overflow ? `{${props}, …}` : `{${props}}`;
      }
      return "{…}";
    }
    default: return String(arg.value ?? "");
  }
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  const tabId = source?.tabId;
  if (tabId == null) return;
  const b = buf(tabId);

  switch (method) {
    case "Runtime.consoleAPICalled": {
      b.console.push({
        ts: params.timestamp || Date.now(),
        level: levelOf(params.type),
        text: (params.args || []).map(argPreview).join(" "),
        url: params.stackTrace?.[0]?.url || "",
      });
      if (b.console.length > BUFFER_CAP) b.console.splice(0, b.console.length - BUFFER_CAP);
      break;
    }
    case "Log.entryAdded": {
      const e = params.entry || {};
      b.console.push({
        ts: e.timestamp || Date.now(),
        level: e.level === "warning" ? "warning" : e.level === "error" ? "error" : "log",
        text: e.text || "",
        url: e.url || "",
      });
      if (b.console.length > BUFFER_CAP) b.console.splice(0, b.console.length - BUFFER_CAP);
      break;
    }
    case "Runtime.exceptionThrown": {
      const details = params.exceptionDetails || {};
      b.console.push({
        ts: params.timestamp || Date.now(),
        level: "error",
        text: details.exception?.description || details.text || "Uncaught exception",
        url: details.url || "",
      });
      if (b.console.length > BUFFER_CAP) b.console.splice(0, b.console.length - BUFFER_CAP);
      break;
    }
    case "Network.requestWillBeSent": {
      const entry = {
        requestId: params.requestId,
        method: params.request.method,
        url: params.request.url,
        type: params.type || "",
        ts: params.timestamp || Date.now(),
      };
      b.network.set(params.requestId, entry);
      b.networkOrder.push(params.requestId);
      if (b.networkOrder.length > BUFFER_CAP) {
        const dropped = b.networkOrder.splice(0, b.networkOrder.length - BUFFER_CAP);
        for (const id of dropped) b.network.delete(id);
      }
      break;
    }
    case "Network.responseReceived": {
      const entry = b.network.get(params.requestId);
      if (entry) {
        entry.status = params.response.status;
        entry.mimeType = params.response.mimeType;
      }
      break;
    }
    case "Network.loadingFailed": {
      const entry = b.network.get(params.requestId);
      if (entry) entry.error = params.errorText;
      break;
    }
    default:
      break;
  }
});

chrome.debugger.onDetach.addListener((source) => {
  const tabId = source?.tabId;
  if (tabId == null) return;
  ATTACHED.delete(tabId);
  // The tab is no longer being driven: take the cursor/glow indicator down
  // (it otherwise stays up so screenshots can show where the mouse is).
  chrome.tabs.sendMessage(tabId, { type: "taskwindow:indicator", op: "hide" }).catch(() => {});
});

export async function ensureAttached(tabId) {
  if (ATTACHED.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, "1.3");
  ATTACHED.add(tabId);
  // Best-effort domain enables: some pages (e.g. Chrome Web Store) restrict
  // parts of this, but input dispatch and screenshots still work.
  for (const [method, params] of [
    ["Runtime.enable"], ["Log.enable"], ["Page.enable"],
    ["Network.enable", { maxResourceBufferSize: 10_000_000, maxTotalBufferSize: 50_000_000 }],
    // The agent window is deliberately never focused (the user is elsewhere);
    // make the page behave as if it were, as headless-style drivers do.
    ["Emulation.setFocusEmulationEnabled", { enabled: true }],
  ]) {
    try {
      await send(tabId, method, params);
    } catch {}
  }
}

export async function send(tabId, method, params) {
  return chrome.debugger.sendCommand({ tabId }, method, params);
}

export async function withDebugger(tabId, fn) {
  await ensureAttached(tabId);
  return fn(tabId);
}
