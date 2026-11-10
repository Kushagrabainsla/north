import { useState } from "react";
import { Link } from "react-router-dom";
import { post } from "../api";
import { BrowserProfiles } from "../browserProfiles";
import { CodingAgents, ProviderConnections, TelegramConnection, type TelegramStatus } from "../connections";
import { configureDisplayTimezone, PageHeader, Panel } from "../components";
import { useResource } from "../hooks";
import { ActionBar, Button, Empty, ListRow, Loading, Notice, Status, Tabs, type NoticeTone, Note } from "../kit";

interface SetupData {
  status: "not_started" | "in_progress" | "completed" | "skipped"; step: number;
  timezone: string; timezone_configured: boolean;
  profiles: { id: string; name: string; context: string; enabled: boolean }[];
  providers: { id: string; name: string; configured: boolean; optional: boolean }[];
}
interface BootstrapData {
  status: "complete" | "in_progress" | "not_started";
  candidates: string[]; completed: { path: string; status: string }[]; candidate_count: number;
  /** Eligible files North has not read: never, or changed since it last did. */
  unread: string[];
}
// Each step is one connection North needs, shown with the same component its
// home in Settings uses. Approval mode is a preference, so it stays in Settings.
export const SETUP_STEPS = ["AI connection", "Coding agents", "Browser profiles", "Telegram", "Your files", "Finish"];
const LAST = SETUP_STEPS.length - 1;
export const setupNeedsAttention = (status: SetupData["status"]) => status === "not_started" || status === "in_progress";

export function SetupReminder() {
  const { data } = useResource<SetupData>("/web/api/setup", 10000);
  if (!data || !setupNeedsAttention(data.status)) return null;
  return <Panel title="Set up North" label="Your connections" className="setup-reminder">
    <Note>Connect an AI provider, your coding agents, browser profiles and Telegram, and choose whether North reads your files. Skip any step and come back later.</Note>
    <Button variant="primary" to="/setup">{data.status === "in_progress" ? "Continue setup" : "Start setup"}</Button>
  </Panel>;
}

/** Reading the person's own files is opt-in: nothing starts until they press the button. */
export function YourFiles() {
  const resource = useResource<BootstrapData>("/web/api/bootstrap", 5000);
  const [said, setSaid] = useState<{ tone: NoticeTone; text: string } | null>(null);
  const data = resource.data;
  const done = new Set((data?.completed || []).map(item => item.path));
  // After a finished run only new or changed files are read, at most 25 at a time.
  const fresh = data?.status === "complete" ? (data.unread || []).slice(0, 25) : [];
  const start = async () => {
    setSaid({ tone: "info", text: "Starting…" });
    try {
      const result = await post<{ status: string; selected?: number }>("/web/api/bootstrap", { paths: fresh });
      setSaid({ tone: "success", text: result.status === "in_progress" ? "Already reading." : `Reading ${result.selected || 0} document(s).` });
      await resource.reload();
    } catch (error) { setSaid({ tone: "error", text: error instanceof Error ? error.message : String(error) }); }
  };
  return <Panel title="Let North learn from your files" label="Optional"
    actions={!data ? undefined : data.status === "complete" && !fresh.length ? <Status value="completed"/>
      : <Button variant="primary" busy={data.status === "in_progress"} busyLabel="Reading…" onClick={() => void start()}>{fresh.length ? `Read ${fresh.length} new file${fresh.length > 1 ? "s" : ""}` : "Read my files"}</Button>}>
    <Note>
      <p>North can read documents like your resume, notes and plans once, and remember facts about you: your work, goals and preferences. Then you don’t have to repeat them.</p>
      <p>It looks only in your Documents, Desktop, Downloads and notes folders and skips code and build files. The text of each document goes to your AI provider to pull out facts. Nothing starts until you press the button.</p>
      {data?.status === "complete" && <p>North has read the documents marked completed. Facts it learned are on the <Link to="/memory?tab=facts">Memory</Link> page, where you can correct or forget them.{fresh.length > 0 && " Files added or changed since then are marked pending."}</p>}
    </Note>
    {said && <Notice tone={said.tone}>{said.text}</Notice>}
    {resource.error && <Notice tone="error">{resource.error}</Notice>}
    {resource.loading && !data ? <Loading/> : <>
      <div className="bootstrap-list">{(data?.candidates || []).map(path =>
        <ListRow key={path} title={path.split("/").pop()} description={path} status={<Status value={done.has(path) ? "completed" : "pending"}/>}/>)}</div>
      {!data?.candidate_count && <Empty>No eligible documents found.</Empty>}
    </>}
  </Panel>;
}

function Finish({ data }: { data: SetupData }) {
  const telegram = useResource<TelegramStatus>("/web/api/telegram").data;
  const providers = data.providers.filter(provider => !provider.optional && provider.configured);
  const browsers = data.profiles.filter(profile => profile.enabled);
  const rows: [string, boolean, string][] = [
    ["AI connection", providers.length > 0, providers.length ? providers.map(provider => provider.name).join(", ") : "Required for any task"],
    ["Browser profiles", browsers.length > 0, browsers.length ? browsers.map(profile => profile.name).join(", ") : "Optional"],
    ["Telegram", !!telegram?.configured, telegram?.configured ? `Bot ${telegram.token_hint}` : "Optional"],
  ];
  return <Panel title="Ready to go" label="Summary">
    <div>{rows.map(([name, ok, detail]) =>
      <ListRow key={name} title={name} description={detail} status={<Status value={ok ? "set" : "not_set"}/>}/>)}</div>
    <Note>North asks before acting according to your approval mode. Change it, and everything else here, in <Link to="/settings">Settings</Link>.</Note>
  </Panel>;
}

export function Setup() {
  const resource = useResource<SetupData>("/web/api/setup", 5000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const progress = async (step: number, status: SetupData["status"] = "in_progress") => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      if (!resource.data?.timezone_configured) {
        const saved = await post<{ timezone: string }>("/orchestrator/settings", {
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, initialize_timezone_only: true,
        });
        configureDisplayTimezone(saved.timezone);
      }
      await post("/web/api/setup", { step, status }); await resource.reload();
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  };
  const data = resource.data;
  const header = <PageHeader title="Set up North" subtitle="Connect what North works with. Every step is also in Settings, so you can change it later." actions={<Button to="/settings">Settings</Button>}/>;
  if (!data) return <div className="page setup-page">
    {header}
    {resource.loading ? <Loading/> : <Notice tone="error">{resource.error || "Setup unavailable"}</Notice>}
  </div>;
  const step = Math.min(data.step, LAST);
  const configured = data.providers.filter(provider => !provider.optional && provider.configured);
  return <div className="page setup-page">
    {header}
    {(error || resource.error) && <Notice tone="error">{error || resource.error}</Notice>}
    <Tabs label="Setup steps" value={String(step)} onChange={value => void progress(Number(value))}
      items={SETUP_STEPS.map((name, index) => ({ value: String(index), label: `${index + 1}. ${name}` }))}/>
    {data.status === "completed" && <Notice tone="success">Setup complete. You can revisit any step.</Notice>}
    {data.status === "skipped" && <Notice>Setup skipped. Continue whenever you like.</Notice>}
    {step === 0 && <ProviderConnections/>}
    {step === 1 && <CodingAgents/>}
    {step === 2 && <BrowserProfiles/>}
    {step === 3 && <TelegramConnection/>}
    {step === 4 && <YourFiles/>}
    {step === 5 && <Finish data={data}/>}
    {step === LAST && !configured.length && <Notice tone="warning">Connect an AI provider before finishing, or skip setup for now.</Notice>}
    <ActionBar>
      <Button variant="quiet" to="/">Dashboard</Button>
      <Button variant="quiet" disabled={busy} onClick={() => void progress(step, "skipped")}>Skip setup for now</Button>
      {step > 0 && <Button disabled={busy} onClick={() => void progress(step - 1)}>Back</Button>}
      {step < LAST ? <Button variant="primary" disabled={busy} onClick={() => void progress(step + 1)}>Continue</Button>
        : <Button variant="primary" disabled={busy || !configured.length} onClick={() => void progress(LAST, "completed")}>Finish setup</Button>}
    </ActionBar>
  </div>;
}
