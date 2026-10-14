import { useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { post } from "../api";
import { BrowserProfiles } from "../browserProfiles";
import { CodingAgents, ProviderConnections, TelegramConnection, type TelegramStatus } from "../connections";
import { configureDisplayTimezone, ErrorNotice, Loading, PageHeader, Panel, Status } from "../components";
import { useResource } from "../hooks";

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
    <p className="setup-copy">Connect an AI provider, your coding agents, browser profiles and Telegram, and choose whether North reads your files. Skip any step and come back later.</p>
    <Link className="primary-button" to="/setup">{data.status === "in_progress" ? "Continue setup" : "Start setup"}</Link>
  </Panel>;
}

/** Reading the person's own files is opt-in: nothing starts until they press the button. */
export function YourFiles() {
  const resource = useResource<BootstrapData>("/web/api/bootstrap", 5000);
  const [message, setMessage] = useState("");
  const data = resource.data;
  const done = new Set((data?.completed || []).map(item => item.path));
  // After a finished run only new or changed files are read, at most 25 at a time.
  const fresh = data?.status === "complete" ? (data.unread || []).slice(0, 25) : [];
  const start = async () => {
    setMessage("Starting…");
    try {
      const result = await post<{ status: string; selected?: number }>("/web/api/bootstrap", { paths: fresh });
      setMessage(result.status === "in_progress" ? "Already reading." : `Reading ${result.selected || 0} document(s).`);
      await resource.reload();
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  };
  if (resource.loading) return <Loading/>;
  return <Panel title="Let North learn from your files" label="Optional"
    actions={data?.status === "complete" && !fresh.length ? <Status value="completed"/>
      : <button className="primary-button" disabled={data?.status === "in_progress"} onClick={() => void start()}>{data?.status === "in_progress" ? "Reading…" : fresh.length ? `Read ${fresh.length} new file${fresh.length > 1 ? "s" : ""}` : "Read my files"}</button>}>
    <div className="setup-copy">
      <p>North can read documents like your resume, notes and plans once, and remember facts about you: your work, goals and preferences. Then you don’t have to repeat them.</p>
      <p>It looks only in your Documents, Desktop, Downloads and notes folders and skips code and build files. The text of each document goes to your AI provider to pull out facts. Nothing starts until you press the button.</p>
      {data?.status === "complete" && <p>North has read the documents marked completed. Facts it learned are on the <Link to="/memory?tab=facts">Memory</Link> page, where you can correct or forget them.{fresh.length > 0 && " Files added or changed since then are marked pending."}</p>}
    </div>
    {message && <div className="notice">{message}</div>}
    {resource.error && <ErrorNotice message={resource.error}/>}
    <div className="bootstrap-list">{(data?.candidates || []).map(path => <div className="bootstrap-row" key={path}>
      <span><b>{path.split("/").pop()}</b><small>{path}</small></span>
      <Status value={done.has(path) ? "completed" : "pending"}/>
    </div>)}</div>
    {!data?.candidate_count && <p className="setup-copy muted">No eligible documents found.</p>}
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
    <div className="setup-status-list">{rows.map(([name, ok, detail]) => <div key={name}>
      <b>{name}</b><span className={`status status-${ok ? "completed" : "pending"}`}>{ok ? "set" : "not set"}</span><small className="setup-tool-detail">{detail}</small>
    </div>)}</div>
    <p className="setup-copy muted">North asks before acting according to your approval mode. Change it, and everything else here, in <Link to="/settings">Settings</Link>.</p>
  </Panel>;
}

export function Setup() {
  const resource = useResource<SetupData>("/web/api/setup", 5000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const progress = async (step: number, status: SetupData["status"] = "in_progress") => {
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
  if (resource.loading) return <Loading/>;
  if (!resource.data) return <ErrorNotice message={resource.error || "Setup unavailable"}/>;
  const data = resource.data;
  const step = Math.min(data.step, LAST);
  const configured = data.providers.filter(provider => !provider.optional && provider.configured);
  return <div className="page setup-page">
    <PageHeader title="Set up North" eyebrow="Onboarding" subtitle="Connect what North works with. Every step is also in Settings, so you can change it later." actions={<Link className="ghost-button" to="/settings">Settings</Link>}/>
    {(error || resource.error) && <ErrorNotice message={error || resource.error!}/>}
    <nav className="segmented setup-steps" aria-label="Setup steps" style={{ "--steps": SETUP_STEPS.length } as CSSProperties}>{SETUP_STEPS.map((name, index) => <button key={name} className={step === index ? "active" : ""} aria-current={step === index ? "step" : undefined} disabled={busy} onClick={() => void progress(index)}><span className="setup-step-number">{index + 1}</span>{name}</button>)}</nav>
    {data.status === "completed" && <p className="setup-copy">Setup complete. You can revisit any step.</p>}
    {data.status === "skipped" && <p className="setup-copy">Setup skipped. Continue whenever you like.</p>}
    {step === 0 && <ProviderConnections/>}
    {step === 1 && <CodingAgents/>}
    {step === 2 && <BrowserProfiles/>}
    {step === 3 && <TelegramConnection/>}
    {step === 4 && <YourFiles/>}
    {step === 5 && <Finish data={data}/>}
    <footer className="setup-footer"><div className="setup-actions">
      {step > 0 && <button className="ghost-button" disabled={busy} onClick={() => void progress(step - 1)}>Back</button>}
      {step < LAST ? <button className="primary-button" disabled={busy} onClick={() => void progress(step + 1)}>Continue</button>
        : <button className="primary-button" disabled={busy || !configured.length} onClick={() => void progress(LAST, "completed")}>Finish setup</button>}
    </div><div className="setup-actions">
      <button className="ghost-button" disabled={busy} onClick={() => void progress(step, "skipped")}>Skip setup for now</button>
      <Link className="ghost-button" to="/">Dashboard</Link>
    </div></footer>
    {step === LAST && !configured.length && <p className="setup-copy">Connect an AI provider before finishing, or skip setup for now.</p>}
  </div>;
}
