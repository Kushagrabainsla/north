import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, post } from "./api";
import { ErrorNotice, Panel } from "./components";
import { useResource } from "./hooks";

// Connections North works with: AI providers, coding agents and Telegram. Each
// lives here once; onboarding and Settings show the same component, never a copy.

export interface ProviderAuthState {
  provider_id: string;
  state:
    | "starting"
    | "pending"
    | "connected"
    | "disconnected"
    | "cancelled"
    | "error";
  configured: boolean;
  detail: string;
  authorization_url?: string;
  account_hint?: string;
}

// The embeddings row is the only provider with no key and no login, so it is
// the only one whose identifying detail has to come from elsewhere.
export function isLocalEmbeddings(provider: any) {
  return !provider.env_key && provider.auth_kind !== "oauth_pkce";
}

export function ProviderConnections() {
  const overview = useResource<any>("/web/api/system", 8000);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [providerMessage, setProviderMessage] = useState("");
  const [providerAuth, setProviderAuth] = useState<
    Record<string, ProviderAuthState>
  >({});
  const authWindows = useRef<Record<string, Window | null>>({});
  const providers = overview.data?.providers || [];
  const embeddings = overview.data?.embeddings;
  const embeddingsFor = (provider: any) =>
    isLocalEmbeddings(provider) && embeddings?.model ? embeddings.model : "";
  const pendingAuthIds = Object.values(providerAuth)
    .filter((state) => state.state === "starting" || state.state === "pending")
    .map((state) => state.provider_id)
    .sort()
    .join(",");

  const saveProvider = async (id: string) => {
    try {
      await post(`/web/api/providers/${id}`, { api_key: keys[id] });
      setKeys((current) => ({ ...current, [id]: "" }));
      setProviderMessage("Provider credentials saved and runtime refreshed.");
      await overview.reload();
    } catch (error) {
      setProviderMessage(String(error));
    }
  };

  const connectProvider = async (id: string) => {
    const popup = window.open(
      "about:blank",
      `north-${id}-auth`,
      "popup,width=560,height=760",
    );
    authWindows.current[id] = popup;
    if (popup)
      popup.document.body.textContent = "Preparing secure OpenAI login…";
    setProviderAuth((current) => ({
      ...current,
      [id]: {
        provider_id: id,
        state: "starting",
        configured: false,
        detail: "Preparing browser login…",
      },
    }));
    try {
      const state = await post<ProviderAuthState>(
        `/web/api/providers/${id}/auth`,
        {},
      );
      setProviderAuth((current) => ({ ...current, [id]: state }));
      if (state.authorization_url && popup) {
        popup.opener = null;
        popup.location.replace(state.authorization_url);
      } else if (state.authorization_url) {
        setProviderMessage("Pop-up blocked. Use ‘Continue login’ below.");
      } else if (popup) {
        popup.close();
      }
      if (state.state === "connected") await overview.reload();
    } catch (error) {
      popup?.close();
      setProviderAuth((current) => ({
        ...current,
        [id]: {
          provider_id: id,
          state: "error",
          configured: false,
          detail: String(error),
        },
      }));
    }
  };

  const disconnectProvider = async (id: string) => {
    try {
      const state = await api<ProviderAuthState>(
        `/web/api/providers/${id}/auth`,
        { method: "DELETE" },
      );
      setProviderAuth((current) => ({ ...current, [id]: state }));
      setProviderMessage("OpenAI Codex disconnected.");
      await overview.reload();
    } catch (error) {
      setProviderMessage(String(error));
    }
  };

  useEffect(() => {
    const ids = pendingAuthIds ? pendingAuthIds.split(",") : [];
    if (!ids.length) return;
    let stopped = false;
    const poll = async () => {
      for (const id of ids) {
        try {
          const state = await api<ProviderAuthState>(
            `/web/api/providers/${id}/auth`,
          );
          if (stopped) return;
          setProviderAuth((current) => ({ ...current, [id]: state }));
          if (!["starting", "pending"].includes(state.state)) {
            authWindows.current[id]?.close();
            delete authWindows.current[id];
            await overview.reload();
          }
        } catch (error) {
          if (!stopped) setProviderMessage(String(error));
        }
      }
    };
    const timer = window.setInterval(poll, 1500);
    void poll();
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [pendingAuthIds]);

  return (
    <Panel
      title="Providers"
      label={`${providers.filter((provider: any) => provider.configured).length} configured`}
    >
      {providerMessage && <div className="notice">{providerMessage}</div>}
      <div className="provider-list">
        {providers.map((provider: any) => {
          const auth = providerAuth[provider.id];
          const waiting =
            auth?.state === "starting" || auth?.state === "pending";
          const configured = auth ? auth.configured : provider.configured;
          return (
            <div className="provider-row" key={provider.id}>
              {/* The trailing detail is whatever identifies the provider: the env var
            holding its key, "Browser login" for OAuth, or - for the on-device
            embedder, which has no key at all - the model it runs. Without the
            last case the row ended on a dangling separator. */}
              <div>
                <b>{provider.name}</b>
                <small>
                  {[
                    provider.description,
                    provider.auth_kind === "oauth_pkce"
                      ? "Browser login"
                      : provider.env_key || embeddingsFor(provider),
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </small>
              </div>
              <div className="provider-controls">
                <span
                  className={
                    configured
                      ? "provider-state configured"
                      : "provider-state"
                  }
                >
                  {waiting
                    ? "Waiting for browser login"
                    : configured
                      ? `Ready ${auth?.account_hint || provider.credential_hint}`
                      : "Not configured"}
                </span>
                {provider.env_key && (
                  <div className="provider-edit">
                    <input
                      type="password"
                      placeholder="API key"
                      value={keys[provider.id] || ""}
                      onChange={(event) =>
                        setKeys((current) => ({
                          ...current,
                          [provider.id]: event.target.value,
                        }))
                      }
                    />
                    <button
                      className="ghost-button"
                      disabled={!keys[provider.id]}
                      onClick={() => saveProvider(provider.id)}
                    >
                      Save
                    </button>
                  </div>
                )}
                {provider.auth_kind === "oauth_pkce" && (
                  <div className="provider-auth">
                    <div className="provider-auth-actions">
                      <button
                        className={
                          configured ? "ghost-button" : "primary-button"
                        }
                        disabled={waiting}
                        onClick={() => connectProvider(provider.id)}
                      >
                        {waiting
                          ? "Waiting…"
                          : configured
                            ? "Reconnect"
                            : "Connect"}
                      </button>
                      {configured && (
                        <button
                          className="ghost-button"
                          onClick={() => disconnectProvider(provider.id)}
                        >
                          Disconnect
                        </button>
                      )}
                      {auth?.authorization_url && (
                        <a
                          href={auth.authorization_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Continue login
                        </a>
                      )}
                    </div>
                    {auth?.detail && (
                      <small
                        className={
                          auth.state === "error"
                            ? "provider-auth-error"
                            : "provider-auth-detail"
                        }
                      >
                        {auth.detail}
                      </small>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

export interface CodingAgentStatus {
  name: string;
  installed: boolean;
  ready: boolean;
  version: string;
  detail: string;
}

// Installed is not enough: the probe also checks the version, the agent's own
// login and its protocol. It starts each CLI, so it runs when asked, not on a timer.
export function CodingAgents() {
  const [agents, setAgents] = useState<CodingAgentStatus[] | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const check = async () => {
    setChecking(true); setError("");
    try { setAgents(await api<CodingAgentStatus[]>("/web/api/setup/coding-agents")); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setChecking(false); }
  };
  useEffect(() => { void check(); }, []);
  return <Panel title="Coding agents" label="Codex and Claude Code" actions={<button className="ghost-button" disabled={checking} onClick={() => void check()}>{checking ? "Checking…" : "Check again"}</button>}>
    {error && <div className="notice">{error}</div>}
    {!agents && !error && <p className="setup-copy muted">Checking each agent’s install and login…</p>}
    {agents && <div className="setup-status-list">{agents.map(agent => <div key={agent.name}>
      <b>{agent.name}{agent.version && <small> {agent.version}</small>}</b>
      <span className={`status status-${agent.ready ? "completed" : "pending"}`}>{agent.ready ? "ready" : agent.installed ? "needs attention" : "not installed"}</span>
      {agent.detail && <small className="setup-tool-detail">{agent.detail}</small>}
    </div>)}</div>}
    <p className="setup-copy muted">North hands coding to these agents in their own login. Ready means installed, recent enough and logged in; usage limits are only known when a run starts.</p>
  </Panel>;
}

export interface TelegramStatus {
  configured: boolean;
  token_hint: string;
  allowed_chat_ids: string;
  problem: string;
  bot: string;
}

// Telegram runs tasks and answers approvals from your phone. North checks the
// token with Telegram before saving, and the running server picks it up live.
export function TelegramConnection() {
  const resource = useResource<TelegramStatus>("/web/api/telegram", 10000);
  const [token, setToken] = useState("");
  const [ids, setIds] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const data = resource.data;
  const allowed = ids ?? data?.allowed_chat_ids ?? "";
  const perform = async (work: () => Promise<string>) => {
    setBusy(true); setError(""); setMessage("");
    try { setMessage(await work()); setToken(""); setIds(null); await resource.reload(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  };
  const save = (event: FormEvent) => {
    event.preventDefault();
    void perform(async () => {
      const saved = await post<TelegramStatus>("/web/api/telegram", { bot_token: token, allowed_chat_ids: allowed });
      return `Connected as @${saved.bot}. Send it a message to run a task.`;
    });
  };
  const disconnect = () => void perform(async () => {
    await api<TelegramStatus>("/web/api/telegram", { method: "DELETE" });
    return "Telegram disconnected.";
  });
  return <Panel title="Telegram" label={data?.configured ? `Ready ${data.token_hint}` : "Optional"}
    actions={data?.token_hint && <button className="ghost-button" disabled={busy} onClick={disconnect}>Disconnect</button>}>
    {message && <div className="notice">{message}</div>}
    {(error || resource.error || data?.problem) && <ErrorNotice message={error || resource.error || data!.problem}/>}
    <div className="setup-connection-note"><ol>
      <li>In Telegram, message <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather</a>, send <code>/newbot</code> and copy the token it gives you.</li>
      <li>Message <a href="https://t.me/userinfobot" target="_blank" rel="noreferrer">@userinfobot</a> to get your numeric user id.</li>
      <li>Paste both here. Only the ids you list can use the bot.</li>
    </ol></div>
    <form className="setup-form" onSubmit={save}>
      <label>Bot token<input type="password" autoComplete="off" value={token}
        placeholder={data?.token_hint ? `Saved ${data.token_hint} · paste a new one to replace it` : "123456789:AA…"}
        onChange={event => setToken(event.target.value)}/></label>
      <label>Allowed user ids<input value={allowed} placeholder="123456789, comma-separated for more than one"
        onChange={event => setIds(event.target.value)}/></label>
      <div className="setup-actions"><button className="primary-button" type="submit"
        disabled={busy || (!token.trim() && !data?.token_hint) || !allowed.trim()}>{busy ? "Checking with Telegram…" : data?.token_hint ? "Save" : "Connect"}</button></div>
    </form>
    <p className="setup-copy muted">Send the bot any message or voice note to run a task. North also sends approval requests there, unless the terminal UI is already showing them. In the chat, <code>/help</code> lists the commands; <code>/workspace</code> picks the folder a task works in, which must be a git repository for coding.</p>
  </Panel>;
}
