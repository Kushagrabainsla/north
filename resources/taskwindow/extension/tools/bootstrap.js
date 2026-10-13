const BOOTSTRAP_FILE = "taskwindow-bootstrap.json";
// The Chrome Web Store build's id. Fixed by the store, so the daemon can trust
// requests carrying this origin without a code.
export const STORE_EXTENSION_ID = "adbfpkbjndcpjihceobeegkokblgifpe";
let rejectedCode = null;
let storePairingRefused = false;

/** Whether a TaskWindow daemon answers on the port — the CLI has been run. */
export async function daemonReachable(port = 9377) {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/health`, { cache: "no-store" });
    if (!response.ok) return false;
    return (await response.json().catch(() => ({}))).ok === true;
  } catch {
    return false;
  }
}

/**
 * Whether a first install should open the setup guide. Not when a daemon
 * already answers (the CLI installer is driving), and never for an install
 * pushed by policy: an administrator set up that machine, and Chrome may start
 * before its daemon does, so a guide would only be a stray tab on it.
 */
export async function needsOnboarding(port = 9377) {
  const { installType } = await chrome.management.getSelf();
  if (installType === "admin") return false;
  return !(await daemonReachable(port));
}

/**
 * Pair a Web Store install on its own: Chrome stamps this extension's origin
 * on the request, and the daemon returns the token to the store origin
 * without a code. Only tried when this really is the store build, and only
 * until the daemon says no — nothing to guess, so nothing to retry.
 */
export async function claimStorePairing(port = 9377) {
  if (chrome.runtime.id !== STORE_EXTENSION_ID || storePairingRefused) return null;
  let response;
  try {
    response = await fetch(`http://127.0.0.1:${port}/pair`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({}),
    });
  } catch {
    return null; // daemon not running yet (the user may still be installing the CLI); retry later
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.token) {
    storePairingRefused = true;
    return null;
  }
  await chrome.storage.local.set({ token: body.token, port });
  return { token: body.token, port };
}

/**
 * Redeem the installer-created, short-lived code on first launch. The file
 * never contains the daemon token; the token is returned only by localhost
 * after a successful one-time claim.
 */
export async function claimInstallerBootstrap() {
  let bootstrap;
  try {
    const file = await fetch(chrome.runtime.getURL(BOOTSTRAP_FILE), { cache: "no-store" });
    if (!file.ok) return null;
    bootstrap = await file.json();
  } catch {
    return null;
  }

  const code = String(bootstrap?.code || "").trim().toUpperCase();
  const port = Number(bootstrap?.port) || 9377;
  if (!code || code === rejectedCode) return null;

  let response;
  try {
    response = await fetch(`http://127.0.0.1:${port}/pair`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ code }),
    });
  } catch {
    return null; // daemon may still be starting; retry on the next connection attempt
  }

  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.token) {
    rejectedCode = code;
    return null;
  }
  await chrome.storage.local.set({ token: body.token, port });
  return { token: body.token, port };
}
