import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { post } from "./api";
import { ErrorNotice, Panel } from "./components";
import { useResource } from "./hooks";

export interface BrowserProfile {
  id: string; name: string; purpose: string; context: "isolated" | "existing";
  data_directory: string; profile_directory: string; connect: string; headed: boolean; enabled: boolean;
}
interface ProfileTest { status: string; error?: string; data?: { profile_verified?: boolean }; }
interface ProfilesData { profiles: BrowserProfile[]; tests: Record<string, ProfileTest>; }
type DiscoveredProfile = BrowserProfile & { browser: string };

export function newBrowserProfile(): BrowserProfile {
  return { id: `profile-${crypto.randomUUID()}`, name: "", purpose: "", context: "isolated",
    data_directory: "", profile_directory: "Default", connect: "", headed: true, enabled: true };
}

export function profileTestLabel(test?: ProfileTest): string {
  if (!test) return "Not tested";
  if (test.status === "running") return "Checking · may be waiting in Approvals";
  if (test.status === "completed" && test.data?.profile_verified) return "Profile verified · login not checked";
  return test.error || "Connection not verified";
}

export function BrowserProfiles() {
  const resource = useResource<ProfilesData>("/web/api/browser/profiles", 3000);
  const [draft, setDraft] = useState<BrowserProfile | null>(null);
  const [found, setFound] = useState<DiscoveredProfile[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const profiles = resource.data?.profiles || [];
  const perform = async (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await work(); await resource.reload(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  };
  const save = (next: BrowserProfile[]) => post("/web/api/browser/profiles", { profiles: next });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!draft) return;
    void perform(async () => {
      await save([...profiles.filter(profile => profile.id !== draft.id), draft]); setDraft(null);
    });
  };
  return <Panel title="Browser profiles" label="Chosen per tool call">
    <p className="muted">Give each profile a purpose. North chooses from the current task context, and asks through the same approval layer when unclear. Profiles are not attached to flows.</p>
    {(error || resource.error) && <ErrorNotice message={error || resource.error!}/>}
    {resource.loading && <p>Loading profiles…</p>}
    {profiles.map(profile => <div className="browser-profile" key={profile.id}>
      <div><b>{profile.name}{!profile.enabled && " · disabled"}</b><p>{profile.purpose}</p>
        <small>{profile.context === "existing" ? "Your existing browser" : "North-managed browser"} · {profileTestLabel(resource.data?.tests[profile.id])}</small></div>
      <div className="setup-actions">
        <button disabled={busy || !profile.enabled || resource.data?.tests[profile.id]?.status === "running"}
          onClick={() => void perform(() => post(`/web/api/browser/profiles/${profile.id}/test`, {}))}>Test connection</button>
        <button disabled={busy} onClick={() => setDraft({ ...profile })}>Edit</button>
        <button disabled={busy} onClick={() => void perform(() => save(profiles.map(item => item.id === profile.id ? { ...item, enabled: !item.enabled } : item)))}>{profile.enabled ? "Disconnect" : "Enable"}</button>
      </div>
    </div>)}
    <div className="setup-actions">
      <button disabled={busy || resource.loading || !!resource.error} onClick={() => setDraft(newBrowserProfile())}>Add North-managed profile</button>
      <button disabled={busy} onClick={() => void perform(async () => {
        const result = await post<{ profiles: DiscoveredProfile[] }>("/web/api/browser/profiles/discover", {});
        setFound(result.profiles); if (!result.profiles.length) setError("No supported browser profiles found. Chrome, Chromium and Brave are supported.");
      })}>Find existing profiles</button>
      <Link to="/approvals">Open Approvals</Link>
    </div>
    <p className="muted">Discovery reads profile names and directories only—not passwords, cookies or browsing history. Disconnect disables North’s use; it does not close your browser or erase logins.</p>
    {found.length > 0 && <div className="setup-discovery">{found.map(({ browser, ...profile }) =>
      <button key={profile.id} disabled={busy} onClick={() => { setDraft({ ...profile, purpose: "", enabled: false }); setFound([]); }}>{browser} · {profile.name}</button>)}</div>}
    {draft && <form className="setup-form" onSubmit={submit}>
      <label>Name<input required maxLength={100} value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })}/></label>
      <label>Use this profile for<textarea required maxLength={1000} value={draft.purpose} placeholder="University work and job applications" onChange={event => setDraft({ ...draft, purpose: event.target.value })}/></label>
      {draft.context === "existing" ? <>
        <p>Existing browser access includes logged-in sessions, cookies, tabs and extensions. North checks the actual profile directory before use, never silently switches profiles, and never copies credentials.</p>
        <p>Open this profile in Chrome. Enable remote debugging at <code>chrome://inspect/#remote-debugging</code>, then approve Chrome’s connection prompt when shown. North cannot bypass that browser permission.</p>
        <details><summary>Connection details</summary>
          <label>Browser data directory<input required value={draft.data_directory} onChange={event => setDraft({ ...draft, data_directory: event.target.value })}/></label>
          <label>Profile directory<input required value={draft.profile_directory} onChange={event => setDraft({ ...draft, profile_directory: event.target.value })}/></label>
          <label>Local CDP endpoint (optional)<input value={draft.connect} placeholder="Auto-detect from this browser’s data directory" onChange={event => setDraft({ ...draft, connect: event.target.value })}/></label>
        </details>
      </> : <label className="setup-check"><input type="checkbox" checked={draft.headed} onChange={event => setDraft({ ...draft, headed: event.target.checked })}/>Show the browser window so I can help when needed</label>}
      <label className="setup-check"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })}/>Allow North to select this profile (calls still follow my approval mode)</label>
      <div className="setup-actions"><button className="primary-button" disabled={busy || resource.loading || !!resource.error} type="submit">Save profile</button><button type="button" disabled={busy} onClick={() => setDraft(null)}>Cancel</button></div>
    </form>}
  </Panel>;
}
