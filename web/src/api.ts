let csrf = "";

type FieldError = { loc?: (string | number)[]; msg?: string };

// What the server said was wrong, in words. A request that does not fit gets FastAPI's list of field
// errors, and shown as it came, a 46,000-character prompt read "[object Object]" (issue #76, M11).
export function errorText(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail) return detail;
  if (!Array.isArray(detail) || !detail.length) return fallback;
  return (detail as FieldError[])
    .map(({ loc = [], msg = "" }) => {
      const field = loc.filter((part) => part !== "body").join(".");
      return field ? `${field}: ${msg}` : msg;
    })
    .join("; ");
}

// `north web` opens the dashboard with a one-time `?pair=` token that signs this
// browser in; after that a cookie remembers it, across server restarts too.
export async function bootstrapSession(): Promise<void> {
  const pair = new URLSearchParams(window.location.search).get("pair");
  const response = await fetch("/web/session", {
    method: "POST",
    credentials: "same-origin",
    ...(pair ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pair }) } : {}),
  });
  // The token works once; leave nothing to copy from the address bar or history.
  if (pair) window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(errorText(body.detail, "North could not establish a local browser session."));
  }
  csrf = (await response.json()).csrf;
}

// The server keeps browser sessions in memory, so a restart forgets them and an
// open tab kept failing with "Invalid credentials." until it was reloaded. A
// rejected session is renewed once, shared by every request in flight.
const EXPIRED_SESSION = "Invalid credentials.";
let renewing: Promise<void> | null = null;
const renewSession = () => (renewing ??= bootstrapSession().finally(() => { renewing = null; }));

export async function api<T>(path: string, options: RequestInit = {}, renewed = false): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") headers.set("X-North-CSRF", csrf);
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (response.status === 403 && !renewed) {
    const body = await response.clone().json().catch(() => ({}));
    if (body.detail === EXPIRED_SESSION) {
      await renewSession();
      return api<T>(path, options, true);
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(errorText(body.detail, `Request failed (${response.status})`));
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const post = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const patch = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(body) });

export const del = (path: string) => api<void>(path, { method: "DELETE" });
