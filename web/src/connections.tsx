import { useEffect, useRef, useState } from "react";
import { api, del, post } from "./api";
import { Panel } from "./components";
import { useDialog } from "./dialog";
import { useResource } from "./hooks";
import { Button, confirmDelete, EntityForm, Field, ListRow, Loading, Notice, Status, type NoticeTone, type Tone } from "./kit";

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

/** What a panel says after an action: the words, and whether it went well. */
type Said = { tone: NoticeTone; text: string } | null;

const errorText = (error: unknown) => (error instanceof Error ? error.message : String(error));

// The embeddings row is the only provider with no key and no login, so it is
// the only one whose identifying detail has to come from elsewhere.
export function isLocalEmbeddings(provider: any) {
  return !provider.env_key && provider.auth_kind !== "oauth_pkce";
}

// Trying a provider, not finding its key, is what "Ready" means: the local embedder read "Ready"
// while its model failed to load on every refresh (issue #76, M6).
const FAILING = new Set(["unavailable", "failing"]);

/** What saving a key found by trying it: "saved and runtime refreshed" was said of keys the provider rejected. */
export function savedKeyMessage(saved: { state?: string; state_detail?: string }): string {
  if (saved.state === "ready") return "Key saved. The provider answered and its models are listed.";
  if (saved.state === "failing") return `Key saved, but the provider did not accept it: ${saved.state_detail || "no reason given"}`;
  return "Key saved. North is still checking it with the provider.";
}

/** A rejected key is a failure, shown as one: it was shown in the success colour. */
export function savedKeyTone(saved: { state?: string }): NoticeTone {
  if (saved.state === "ready") return "success";
  if (saved.state === "failing") return "error";
  return "info";
}

export function providerFailing(provider: any): boolean {
  return FAILING.has(provider.state);
}

export function providerStateLabel(provider: any, configured: boolean, accountHint?: string): string {
  if (provider.state === "loading") return "Loading";
  if (FAILING.has(provider.state)) return `Not working: ${provider.state_detail || "no reason given"}`;
  if (!configured) return "Not configured";
  return `Ready ${accountHint || provider.credential_hint || ""}`.trim();
}

/** The colour of a provider's state: only one that works is green. */
export function providerStateTone(provider: any, configured: boolean, waiting = false): Tone {
  if (waiting || provider.state === "loading") return "busy";
  if (FAILING.has(provider.state)) return "bad";
  return configured ? "good" : "neutral";
}

export function ProviderConnections() {
  const dialog = useDialog();
  const overview = useResource<any>("/web/api/system", 8000);
  const [keys, setKeys] = useState<Record<string, string>>({});
  // The provider whose key form is open.
  const [editing, setEditing] = useState("");
  const [saving, setSaving] = useState(false);
  const [said, setSaid] = useState<Said>(null);
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
    setSaving(true);
    try {
      const saved = await post<{ state?: string; state_detail?: string }>(`/web/api/providers/${id}`, { api_key: keys[id] });
      setKeys((current) => ({ ...current, [id]: "" }));
      setEditing("");
      setSaid({ tone: savedKeyTone(saved), text: savedKeyMessage(saved) });
      await overview.reload();
    } catch (error) {
      setSaid({ tone: "error", text: errorText(error) });
    } finally {
      setSaving(false);
    }
  };

  // A wrong key could be replaced but not removed, short of editing .env by hand.
  const removeProvider = async (provider: any) => {
    const yes = await confirmDelete(dialog, {
      noun: "key", name: provider.name, verb: "Remove",
      consequence: `North stops using ${provider.name} until you add a key again.`,
    });
    if (!yes) return;
    try {
      await del(`/web/api/providers/${provider.id}`);
      setSaid({ tone: "success", text: "Key removed." });
      await overview.reload();
    } catch (error) {
      setSaid({ tone: "error", text: errorText(error) });
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
        setSaid({ tone: "warning", text: "Pop-up blocked. Use ‘Continue login’ below." });
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

  const signOut = async (provider: any) => {
    try {
      const state = await api<ProviderAuthState>(
        `/web/api/providers/${provider.id}/auth`,
        { method: "DELETE" },
      );
      setProviderAuth((current) => ({ ...current, [provider.id]: state }));
      setSaid({ tone: "success", text: `Signed out of ${provider.name}.` });
      await overview.reload();
    } catch (error) {
      setSaid({ tone: "error", text: errorText(error) });
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
          if (!stopped) setSaid({ tone: "error", text: errorText(error) });
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
      {said && <Notice tone={said.tone} onDismiss={() => setSaid(null)}>{said.text}</Notice>}
      {overview.error && !overview.data && <Notice tone="error">{overview.error}</Notice>}
      {overview.loading && !overview.data && <Loading/>}
      {providers.map((provider: any) => {
        const auth = providerAuth[provider.id];
        const waiting =
          auth?.state === "starting" || auth?.state === "pending";
        const configured = auth ? auth.configured : provider.configured;
        const failing = providerFailing(provider);
        const label = waiting
          ? "Waiting for browser login"
          : failing ? "Not working" : providerStateLabel(provider, configured, auth?.account_hint);
        return (
          // The trailing detail is whatever identifies the provider: the env var
          // holding its key, "Browser login" for OAuth, or - for the on-device
          // embedder, which has no key at all - the model it runs. Without the
          // last case the row ended on a dangling separator.
          <ListRow
            key={provider.id}
            title={provider.name}
            description={[
              provider.description,
              provider.auth_kind === "oauth_pkce"
                ? "Browser login"
                : provider.env_key || embeddingsFor(provider),
            ].filter(Boolean).join(" · ")}
            status={<span title={provider.state_detail || ""}><Status label={label} tone={providerStateTone(provider, configured, waiting)}/></span>}
            actions={<>
              {provider.env_key && <>
                <Button size="sm" aria-pressed={editing === provider.id} onClick={() => setEditing(editing === provider.id ? "" : provider.id)}>
                  {provider.configured ? "Replace key" : "Add key"}
                </Button>
                {provider.configured && <Button size="sm" variant="danger" onClick={() => void removeProvider(provider)}>Remove</Button>}
              </>}
              {provider.auth_kind === "oauth_pkce" && <>
                {configured && <Button size="sm" onClick={() => void signOut(provider)}>Sign out</Button>}
                <Button size="sm" variant={configured ? "secondary" : "primary"} busy={waiting} busyLabel="Waiting…" onClick={() => void connectProvider(provider.id)}>
                  {configured ? "Sign in again" : "Sign in"}
                </Button>
              </>}
            </>}
          >
            {failing && <Notice tone="error">{providerStateLabel(provider, configured)}</Notice>}
            {auth?.authorization_url && <a href={auth.authorization_url} target="_blank" rel="noreferrer">Continue login</a>}
            {auth?.detail && <Notice tone={auth.state === "error" ? "error" : "info"}>{auth.detail}</Notice>}
            {editing === provider.id && <EntityForm
              title={`${provider.configured ? "Replace" : "Add"} the ${provider.name} key`}
              description={`North keeps it on this computer as ${provider.env_key} and checks it with the provider.`}
              submitLabel="Save key" busy={saving} busyLabel="Checking…"
              canSubmit={Boolean(keys[provider.id])}
              onSubmit={() => saveProvider(provider.id)}
              onCancel={() => { setEditing(""); setKeys((current) => ({ ...current, [provider.id]: "" })); }}
            >
              <Field label="API key" wide>
                <input type="password" autoComplete="off" placeholder="API key" value={keys[provider.id] || ""}
                  onChange={(event) => setKeys((current) => ({ ...current, [provider.id]: event.target.value }))}/>
              </Field>
            </EntityForm>}
          </ListRow>
        );
      })}
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

/** An agent's state in words and colour: installed is not ready. */
export function agentStatus(agent: Pick<CodingAgentStatus, "installed" | "ready">): { label: string; tone: Tone } {
  if (agent.ready) return { label: "Ready", tone: "good" };
  if (agent.installed) return { label: "Needs attention", tone: "warn" };
  return { label: "Not installed", tone: "neutral" };
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
    catch (err) { setError(errorText(err)); }
    finally { setChecking(false); }
  };
  useEffect(() => { void check(); }, []);
  return <Panel title="Coding agents" label="Codex and Claude Code" actions={<Button size="sm" busy={checking} busyLabel="Checking…" onClick={() => void check()}>Check again</Button>}>
    {error && <Notice tone="error">{error}</Notice>}
    {!agents && !error && <Loading label="Checking each agent’s install and login"/>}
    {agents?.map(agent => {
      const state = agentStatus(agent);
      return <ListRow key={agent.name} title={agent.name} meta={[agent.version]} description={agent.detail}
        status={<Status label={state.label} tone={state.tone}/>}/>;
    })}
    <p className="setup-copy muted">North hands coding to these agents in their own login. Ready means installed, recent enough, logged in and able to run its sandbox here; usage limits are only known when a run starts.</p>
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
  const dialog = useDialog();
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
    catch (err) { setError(errorText(err)); }
    finally { setBusy(false); }
  };
  const save = () => perform(async () => {
    const saved = await post<TelegramStatus>("/web/api/telegram", { bot_token: token, allowed_chat_ids: allowed });
    return `Connected as @${saved.bot}. Send it a message to run a task.`;
  });
  const removeToken = async () => {
    const yes = await confirmDelete(dialog, {
      noun: "Telegram token", name: data?.token_hint || "", verb: "Remove",
      consequence: "North stops answering in Telegram until you connect a bot again.",
    });
    if (!yes) return;
    await perform(async () => {
      await api<TelegramStatus>("/web/api/telegram", { method: "DELETE" });
      return "Telegram token removed.";
    });
  };
  return <Panel title="Telegram" label={data?.configured ? `Ready ${data.token_hint}` : "Optional"}
    actions={data?.token_hint && <Button size="sm" variant="danger" disabled={busy} onClick={() => void removeToken()}>Remove token</Button>}>
    {message && <Notice tone="success">{message}</Notice>}
    {(error || resource.error || data?.problem) && <Notice tone="error">{error || resource.error || data!.problem}</Notice>}
    <Notice><ol>
      <li>In Telegram, message <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather</a>, send <code>/newbot</code> and copy the token it gives you.</li>
      <li>Message <a href="https://t.me/userinfobot" target="_blank" rel="noreferrer">@userinfobot</a> to get your numeric user id.</li>
      <li>Paste both here. Only the ids you list can use the bot.</li>
    </ol></Notice>
    <EntityForm title={data?.token_hint ? "Change the bot" : "Connect a bot"} onSubmit={save}
      submitLabel={data?.token_hint ? "Save" : "Connect"} busy={busy} busyLabel="Checking with Telegram…"
      canSubmit={Boolean((token.trim() || data?.token_hint) && allowed.trim())}>
      <Field label="Bot token"><input type="password" autoComplete="off" value={token}
        placeholder={data?.token_hint ? `Saved ${data.token_hint} · paste a new one to replace it` : "123456789:AA…"}
        onChange={event => setToken(event.target.value)}/></Field>
      <Field label="Allowed user ids"><input value={allowed} placeholder="123456789, comma-separated for more than one"
        onChange={event => setIds(event.target.value)}/></Field>
    </EntityForm>
    <p className="setup-copy muted">Send the bot any message or voice note to run a task. North also sends approval requests there, unless the terminal UI is already showing them. In the chat, <code>/help</code> lists the commands; <code>/workspace</code> picks the folder a task works in, which must be a git repository for coding.</p>
  </Panel>;
}

// Your keys and passwords for North's tools (CODING_STYLE §7.6). North's AI only ever
// sees a secret's name; nothing here ever shows a value again, not even part of one.
export interface SavedSecret {
  name: string;
  note: string;
  updated_at: string;
  used_by: string[];
}

interface SecretsList {
  secrets: SavedSecret[];
  missing: { name: string; used_by: string[] }[];
}

const SECRET_NAME = /^[a-z][a-z0-9_]{0,63}$/;
export const SECRET_MIN_LENGTH = 6;

// Why a secret cannot be saved yet, or "" when it can: the same rules north checks.
export function secretProblem(name: string, value: string): string {
  if (!name) return "";
  if (!SECRET_NAME.test(name)) return "A name is lowercase letters, digits and underscores, starting with a letter.";
  if (value && value.length < SECRET_MIN_LENGTH) return `A secret is at least ${SECRET_MIN_LENGTH} characters.`;
  return "";
}

// The line under a secret's name: its note, and which tools use it.
export function secretDetail(secret: SavedSecret): string {
  const uses = secret.used_by.length ? `used by ${secret.used_by.join(", ")}` : "no tool uses it yet";
  return [secret.note, uses].filter(Boolean).join(" · ");
}

/** What removing a secret means, asked before it goes. */
export function secretRemoval(secret: SavedSecret): string {
  return secret.used_by.length
    ? `${secret.used_by.join(", ")} can no longer use it until you add it again.`
    : "No tool uses it. You can add it again later.";
}

export function Secrets() {
  const dialog = useDialog();
  const resource = useResource<SecretsList>("/web/api/secrets");
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const saved = resource.data?.secrets ?? [];
  const missing = resource.data?.missing ?? [];
  const problem = secretProblem(name.trim(), value);
  // Each rule's message sits under the field it is about.
  const nameProblem = secretProblem(name.trim(), "");
  const replacing = saved.some((secret) => secret.name === name.trim());
  const save = async () => {
    const key = name.trim();
    setBusy("save"); setError(""); setMessage("");
    try {
      await api(`/web/api/secrets/${encodeURIComponent(key)}`, { method: "PUT", body: JSON.stringify({ value, note: note.trim() }) });
      setMessage(`Saved ${key}.`);
      setName(""); setValue(""); setNote("");
      await resource.reload();
    } catch (err) { setError(errorText(err)); }
    finally { setBusy(""); }
  };
  const remove = async (secret: SavedSecret) => {
    if (!await confirmDelete(dialog, { noun: "secret", name: secret.name, verb: "Remove", consequence: secretRemoval(secret) })) return;
    setBusy(secret.name); setError(""); setMessage("");
    try { await del(`/web/api/secrets/${encodeURIComponent(secret.name)}`); setMessage(`Removed ${secret.name}.`); await resource.reload(); }
    catch (err) { setError(errorText(err)); }
    finally { setBusy(""); }
  };
  return <Panel title="Secrets" label={saved.length ? `${saved.length} saved` : "None yet"}>
    {message && <Notice tone="success">{message}</Notice>}
    {(error || resource.error) && <Notice tone="error">{error || resource.error}</Notice>}
    <p className="setup-copy muted">Keys and passwords for the tools North makes for you. North's AI only sees a secret's name: North hands the value to a tool that says it uses it, once you approve that tool, and hides it from anything a tool returns. Kept on this computer, readable only by you.</p>
    {missing.map((needed) => <Notice tone="warning" key={needed.name}>
      {needed.used_by.join(", ")} needs the secret <b>{needed.name}</b>.{" "}
      <Button variant="quiet" size="sm" onClick={() => setName(needed.name)}>Add it</Button>
    </Notice>)}
    {resource.loading && !resource.data && <Loading/>}
    {saved.map((secret) => <ListRow key={secret.name} title={secret.name} description={secretDetail(secret)}
      actions={<Button size="sm" variant="danger" busy={busy === secret.name} busyLabel="Removing…" onClick={() => void remove(secret)}>Remove</Button>}/>)}
    <EntityForm title="Add a secret" onSubmit={save} submitLabel={replacing ? "Replace" : "Save"} busy={busy === "save"}
      canSubmit={Boolean(name.trim()) && value.length >= SECRET_MIN_LENGTH && !problem}>
      <Field label="Name" error={nameProblem || undefined}><input value={name} autoComplete="off" placeholder="openweather" onChange={(event) => setName(event.target.value)}/></Field>
      <Field label="Value" error={(!nameProblem && problem) || undefined}><input type="password" autoComplete="new-password" value={value}
        placeholder={replacing ? "Saved · type a new one to replace it" : `at least ${SECRET_MIN_LENGTH} characters`} onChange={(event) => setValue(event.target.value)}/></Field>
      <Field label="Note" wide><input value={note} autoComplete="off" placeholder="what it is for (optional)" onChange={(event) => setNote(event.target.value)}/></Field>
    </EntityForm>
  </Panel>;
}

