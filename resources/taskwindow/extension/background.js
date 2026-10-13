import { tabsList, tabsCreate, tabsClose, navigate, reload, groupsSummary, focusGroup, adoptWindow } from "./tools/tabs.js";
import { computer } from "./tools/computer.js";
import { setViewport } from "./tools/responsive.js";
import { readPage, find, getPageText, formInput, fileUpload, uploadImage } from "./tools/page.js";
import { javascriptExecute } from "./tools/javascript.js";
import { readConsoleMessages, readNetworkRequests } from "./tools/console-net.js";
import { gifRecord } from "./tools/gif.js";
import { shortcutsList, shortcutsExecute } from "./tools/shortcuts.js";
import { connectWs, isConnected, isIdle, reloadExtension } from "./tools/connection.js";
import { needsOnboarding } from "./tools/bootstrap.js";
import { initGroupReaper } from "./tools/tabs.js";

const VERSION = chrome.runtime.getManifest().version;

const HANDLERS = {
  tabs_list: tabsList,
  tabs_create: tabsCreate,
  tabs_close: tabsClose,
  navigate: navigate,
  reload: reload,
  computer: computer,
  set_viewport: setViewport,
  read_page: readPage,
  find: find,
  get_page_text: getPageText,
  form_input: formInput,
  file_upload: fileUpload,
  upload_image: uploadImage,
  javascript_execute: javascriptExecute,
  read_console_messages: readConsoleMessages,
  read_network_requests: readNetworkRequests,
  gif_record: gifRecord,
  shortcuts_list: shortcutsList,
  shortcuts_execute: (params) => shortcutsExecute(params, dispatchTool),
  reload_extension: reloadExtension, // daemon-only (taskwindow update); no MCP tool exposes it
};

async function dispatchTool(tool, params) {
  const handler = HANDLERS[tool];
  if (!handler) throw new Error(`Unknown tool "${tool}"`);
  return handler(params || {});
}

connectWs({ version: VERSION, dispatchTool });
initGroupReaper();

// Chrome installs a downloaded update only once this worker unloads, and the
// daemon connection keeps it alive for good, so on an always-on machine an
// update would wait for the next Chrome restart. Apply it at the first quiet
// moment instead; the connection comes back on its own after the reload.
chrome.runtime.onUpdateAvailable.addListener(() => {
  const reloadWhenIdle = () => {
    if (isIdle(10_000)) chrome.runtime.reload();
    else setTimeout(reloadWhenIdle, 2_000);
  };
  reloadWhenIdle();
});

// First install: if a daemon already answers, the CLI installer is driving
// and pairing happens silently — open nothing. Otherwise the user found the
// store listing first and nothing else tells them the extension needs the
// CLI, so open the setup guide; it turns green on its own once they run it.
// A policy install opens nothing either (see needsOnboarding).
chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason !== "install") return;
  (async () => {
    const { port } = await chrome.storage.local.get("port");
    if (!(await needsOnboarding(Number(port) || 9377))) return;
    chrome.tabs.create({ url: chrome.runtime.getURL("onboarding/onboarding.html") });
  })();
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "taskwindow:getStatus") {
    sendResponse({ connected: isConnected(), version: VERSION });
    return;
  }
  if (msg?.type === "taskwindow:adoptWindow") {
    (async () => {
      try {
        const moved = await adoptWindow(msg.windowId);
        sendResponse({ ok: true, moved });
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || String(err) });
      }
    })();
    return true; // async response
  }
  if (msg?.type === "taskwindow:focusGroup") {
    focusGroup(msg.name, msg.token).then((ok) => sendResponse({ ok }));
    return true; // async response
  }
  if (msg?.type === "taskwindow:getSummary") {
    (async () => {
      try {
        sendResponse({ connected: isConnected(), version: VERSION, groups: await groupsSummary() });
      } catch {
        sendResponse({ connected: isConnected(), version: VERSION, groups: [] });
      }
    })();
    return true; // async response
  }
});
