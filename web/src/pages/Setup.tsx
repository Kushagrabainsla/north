import { useState } from "react";
import { Link } from "react-router-dom";
import { post } from "../api";
import { BrowserProfiles } from "../browserProfiles";
import { configureDisplayTimezone, ErrorNotice, Loading, PageHeader, Panel } from "../components";
import { useResource } from "../hooks";

interface SetupData {
  status: "not_started" | "in_progress" | "completed" | "skipped"; step: number;
  timezone: string; timezone_configured: boolean;
  browser: { state: string; detail: string };
  coding_agents: Record<string, boolean>;
  providers: { id: string; name: string; configured: boolean; optional: boolean }[];
}
interface PermissionSettings { autonomy: string; autonomy_options: { value: string; description: string }[]; }
export const SETUP_STEPS = ["AI connection", "Coding tools", "Browser profiles", "Permissions", "Check setup"];
export const setupNeedsAttention = (status: SetupData["status"]) => status === "not_started" || status === "in_progress";

export function SetupReminder() {
  const { data } = useResource<SetupData>("/web/api/setup", 10000);
  if (!data || !setupNeedsAttention(data.status)) return null;
  return <Panel title="Set up North" label="Your connections and preferences" className="setup-reminder">
    <p className="setup-copy">Connect AI, choose browser profiles, and review permissions. Skip optional steps and return whenever you’re ready.</p>
    <Link className="primary-button" to="/setup">{data.status === "in_progress" ? "Continue setup" : "Start setup"}</Link>
  </Panel>;
}

export function Setup() {
  const resource = useResource<SetupData>("/web/api/setup", 5000);
  const settings = useResource<PermissionSettings>("/orchestrator/settings", 5000);
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
  const step = data.step;
  const configured = data.providers.filter(provider => !provider.optional && provider.configured);
  return <div className="page setup-page">
    <PageHeader title="Set up North" eyebrow="Onboarding" subtitle="Connect your tools and browsers. Everything follows your existing approval settings." actions={<Link className="ghost-button" to="/settings">Settings</Link>}/>
    {(error || resource.error) && <ErrorNotice message={error || resource.error!}/>}
    <nav className="segmented setup-steps" aria-label="Setup steps">{SETUP_STEPS.map((name, index) => <button key={name} className={step === index ? "active" : ""} aria-current={step === index ? "step" : undefined} disabled={busy} onClick={() => void progress(index)}><span className="setup-step-number">{index + 1}</span>{name}</button>)}</nav>
    {data.status === "completed" && <p className="setup-copy">Setup guide completed. Connections may still need authentication or further checks.</p>}
    {data.status === "skipped" && <p className="setup-copy">Setup skipped. You can continue whenever you like.</p>}
    {step === 0 && <Panel title="Connect an AI provider" label="Required for agent tasks">
      <div className="setup-copy"><p>{configured.length ? `Configured: ${configured.map(provider => provider.name).join(", ")}` : "No AI provider configured yet."}</p>
      <p className="muted">Use North’s existing provider controls. Configuration alone does not prove a live model call works.</p></div>
      <div className="setup-actions"><Link className="primary-button" to="/system">Open provider connections</Link></div>
    </Panel>}
    {step === 1 && <Panel title="Coding agents" label="Optional">
      <div className="setup-status-list">{Object.entries(data.coding_agents).map(([name, installed]) => <div key={name}><b>{name}</b><span className={`status status-${installed ? "completed" : "pending"}`}>{installed ? "installed" : "not found"}</span></div>)}</div>
      <p className="setup-copy muted">North can delegate coding to installed agents. Their own login and quota must also work; installation alone does not prove that.</p>
    </Panel>}
    {step === 2 && <BrowserProfiles/>}
    {step === 3 && <Panel title="One approval layer" label={settings.data?.autonomy || "Permissions"}>
      <div className="setup-copy"><p>{settings.data?.autonomy_options.find(option => option.value === settings.data?.autonomy)?.description}</p>
      <p>North’s questions, browser access and login handoffs all use the current approval mode. When a request needs you, the task waits; after the answer, North checks the actual browser state before continuing.</p>
      <p className="muted">Your timezone comes from Settings. Setup detects it once if unset; flows never ask for a separate timezone.</p></div>
      <div className="setup-actions"><Link className="primary-button" to="/settings">Review approval mode and settings</Link></div>
    </Panel>}
    {step === 4 && <>
      <Panel title="Check your browser connections" label="Harmless preflight">
        <div className="setup-copy"><p className="setup-tool-detail">{data.browser.detail}</p>
        <p>Test each enabled profile below. Checks open Chrome’s profile information page and verify the profile’s identity. They do not submit forms, export passwords, activate flows, or prove a site is logged in.</p></div>
      </Panel>
      <BrowserProfiles/>
    </>}
    <footer className="setup-footer"><div className="setup-actions">
      {step > 0 && <button className="ghost-button" disabled={busy} onClick={() => void progress(step - 1)}>Back</button>}
      {step < 4 ? <button className="primary-button" disabled={busy} onClick={() => void progress(step + 1)}>Continue</button>
        : <button className="primary-button" disabled={busy || !configured.length} onClick={() => void progress(4, "completed")}>Finish setup</button>}
    </div><div className="setup-actions">
      <button className="ghost-button" disabled={busy} onClick={() => void progress(step, "skipped")}>Skip setup for now</button>
      <Link className="ghost-button" to="/">Dashboard</Link>
    </div></footer>
    {step === 4 && !configured.length && <p className="setup-copy">Connect an AI provider before finishing, or skip setup for now.</p>}
  </div>;
}
