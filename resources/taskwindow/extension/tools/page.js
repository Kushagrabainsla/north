import { resolveTab } from "./tabs.js";

/**
 * Bridge to the page-level content scripts (content/ax-tree.js). We inject on
 * demand (works even for tabs opened before the extension loaded) and talk via
 * chrome.tabs.sendMessage. Top frame only for now.
 */
async function pageOp(tabId, op, params) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content/ax-tree.js"] });
  } catch (err) {
    throw new Error(
      `Cannot access this page: ${err.message}. Chrome system pages (chrome://), the Web Store and other extensions' pages are off limits.`
    );
  }
  let response;
  try {
    response = await chrome.tabs.sendMessage(tabId, { type: "taskwindow:op", op, params });
  } catch (err) {
    throw new Error(`Page did not answer (${err.message}). Try navigating again or take a screenshot instead.`);
  }
  if (!response) throw new Error(`Page returned no response for "${op}"`);
  if (response.ok === false) throw new Error(response.error || `"${op}" failed on the page`);
  return response;
}

export async function readPage({ tabId, sessionToken } = {}) {
  const tab = await resolveTab(tabId, sessionToken);
  const res = await pageOp(tab.id, "read_page", {});
  return {
    text: `${tab.url ? `# ${tab.title || tab.url}\n` : ""}${res.text}`,
    data: { tabId: tab.id, url: tab.url, refs: res.refCount },
  };
}

export async function find({ query, tabId, sessionToken }) {
  const tab = await resolveTab(tabId, sessionToken);
  const res = await pageOp(tab.id, "find", { query });
  return {
    text: res.matches.length
      ? `${res.matches.length} match(es) for "${query}":\n${res.matches.join("\n")}`
      : `no matches for "${query}"`,
    data: { count: res.matches.length, matches: res.matches },
  };
}

export async function getPageText({ tabId, sessionToken, maxLength = 50_000 }) {
  const tab = await resolveTab(tabId, sessionToken);
  const res = await pageOp(tab.id, "get_page_text", { maxLength });
  return { text: res.text };
}

export async function formInput({ value, ref, selector, clear, tabId, sessionToken }) {
  const tab = await resolveTab(tabId, sessionToken);
  const res = await pageOp(tab.id, "form_input", { value, ref, selector, clear });
  return { data: res.result, text: res.summary || `set ${res.result?.target || "field"} to ${JSON.stringify(value)}` };
}

export async function fileUpload({ files, ref, selector, tabId, sessionToken }) {
  const tab = await resolveTab(tabId, sessionToken);
  const res = await pageOp(tab.id, "file_upload", { files, ref, selector });
  return { data: res.result, text: `attached ${res.result?.names?.join(", ") || "file(s)"} to ${res.result?.target || "file input"}` };
}

export async function uploadImage({ data, mimeType = "image/png", name = "image.png", ref, selector, tabId, sessionToken }) {
  const tab = await resolveTab(tabId, sessionToken);
  const res = await pageOp(tab.id, "upload_image", { data, mimeType, name, ref, selector });
  return { data: res.result, text: res.summary || "image pushed into the page" };
}

/** Best-effort "agent is acting" indicator on a tab; never throws. */
export async function indicator(tabId, payload) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content/indicator.js"] });
    await chrome.tabs.sendMessage(tabId, { type: "taskwindow:indicator", ...payload });
  } catch {}
}
