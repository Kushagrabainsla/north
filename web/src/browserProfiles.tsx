import { useState } from "react";
import { post } from "./api";
import { Panel } from "./components";
import { useResource } from "./hooks";
import { ActionBar, Badge, Button, Empty, EntityForm, Field, ListRow, Loading, Notice, Status, Note } from "./kit";

export interface BrowserProfile {
  id: string; name: string; purpose: string; context: "isolated" | "existing";
  data_directory: string; profile_directory: string; headed: boolean; enabled: boolean;
}
interface ConnectSetup { folder: string; steps: string[]; }
interface ProfileTest { status: string; error?: string; data?: { profile_verified?: boolean }; }
interface ProfilesData { profiles: BrowserProfile[]; tests: Record<string, ProfileTest>; }
type DiscoveredProfile = BrowserProfile & { browser: string };

export function newBrowserProfile(): BrowserProfile {
  return { id: `profile-${crypto.randomUUID()}`, name: "", purpose: "", context: "isolated",
    data_directory: "", profile_directory: "Default", headed: true, enabled: true };
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
  const [setup, setSetup] = useState<{ id: string; data: ConnectSetup } | null>(null);
  const profiles = resource.data?.profiles || [];
  const perform = async (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await work(); await resource.reload(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  };
  const save = (next: BrowserProfile[]) => post("/web/api/browser/profiles", { profiles: next });
  const submit = () => {
    if (!draft) return;
    return perform(async () => {
      await save([...profiles.filter(profile => profile.id !== draft.id), draft]); setDraft(null);
    });
  };
  return <Panel title="Browser profiles" label="Chosen per tool call" className="browser-profiles">
    <Note>Give each profile a purpose. North selects it for the current task and asks through your approval mode when unclear—not through a flow setting.</Note>
    {(error || resource.error) && <Notice tone="error">{error || resource.error}</Notice>}
    {resource.loading && <Loading/>}
    {!resource.loading && !resource.error && !profiles.length && !draft && <Empty>No browser profiles yet. Connect an existing profile or create a separate one for North.</Empty>}
    <div>{profiles.map(profile => {
      const test = resource.data?.tests[profile.id];
      return <ListRow key={profile.id} title={profile.name} muted={!profile.enabled} description={profile.purpose}
        badge={<Badge>{profile.context === "existing" ? "Existing browser" : "North-managed"}</Badge>}
        meta={[<span role="status">{profileTestLabel(test)}</span>]}
        status={!profile.enabled && <Status label="Switched off" tone="neutral"/>}
        actions={<>
          {profile.context === "existing" && <Button size="sm" disabled={busy}
            onClick={() => void perform(async () => setSetup({ id: profile.id, data: await post<ConnectSetup>(`/web/api/browser/profiles/${profile.id}/connect`, {}) }))}>Connect</Button>}
          <Button size="sm" disabled={busy || !profile.enabled || test?.status === "running"}
            onClick={() => void perform(() => post(`/web/api/browser/profiles/${profile.id}/test`, {}))}>Test connection</Button>
          <Button size="sm" disabled={busy} aria-label={`Edit ${profile.name}`} onClick={() => setDraft({ ...profile })}>Edit</Button>
          <Button size="sm" disabled={busy} onClick={() => void perform(() => save(profiles.map(item => item.id === profile.id ? { ...item, enabled: !item.enabled } : item)))}>{profile.enabled ? "Switch off" : "Switch on"}</Button>
        </>}>
        {setup?.id === profile.id && <Notice>
          <ol>{setup.data.steps.map(step => <li key={step}>{step}</li>)}</ol>
          <p><code>{setup.data.folder}</code> <Button size="sm" variant="quiet" onClick={() => void navigator.clipboard.writeText(setup.data.folder)}>Copy folder</Button></p>
          <p>Then choose Test connection. North’s copy of the extension never updates by itself.</p>
        </Notice>}
      </ListRow>;
    })}</div>
    <ActionBar align="start">
      <Button disabled={busy || resource.loading || !!resource.error} onClick={() => setDraft(newBrowserProfile())}>Add North-managed profile</Button>
      <Button disabled={busy} onClick={() => void perform(async () => {
        const result = await post<{ profiles: DiscoveredProfile[] }>("/web/api/browser/profiles/discover", {});
        setFound(result.profiles); if (!result.profiles.length) setError("No supported browser profiles found. Chrome, Chromium and Brave are supported.");
      })}>Find existing profiles</Button>
      <Button variant="quiet" to="/approvals">Open Approvals →</Button>
    </ActionBar>
    <Note>Discovery reads names and directories only, never passwords, cookies or history. Switch off only stops North using a profile; your browser and logins stay as they are.</Note>
    {found.length > 0 && <ActionBar align="start">{found.map(({ browser, ...profile }) =>
      <Button size="sm" key={profile.id} disabled={busy} onClick={() => { setDraft({ ...profile, purpose: "", enabled: false }); setFound([]); }}>{browser} · {profile.name}</Button>)}</ActionBar>}
    {draft && <EntityForm
      title={profiles.some(profile => profile.id === draft.id) ? "Edit browser profile" : "Add browser profile"}
      description={draft.context === "existing" ? "Use a profile you already sign in with." : "Keep this browser separate from your personal profiles."}
      onSubmit={submit} onCancel={() => setDraft(null)} submitLabel="Save profile" busy={busy}
      canSubmit={!resource.loading && !resource.error}>
      <Field label="Name"><input required maxLength={100} value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })}/></Field>
      <Field label="Use this profile for" wide><textarea required maxLength={1000} value={draft.purpose} placeholder="University work and job applications" onChange={event => setDraft({ ...draft, purpose: event.target.value })}/></Field>
      {draft.context === "existing" ? <>
        <Field label="What North can reach" wide><Notice><p>Access includes this profile’s logged-in sessions, cookies, tabs and extensions. North verifies the profile before use and never copies credentials.</p>
          <p>After saving, choose Connect and load North’s extension folder into this profile once. North then works in its own background window, one tab group per task, and never touches your tabs or brings Chrome forward. Needs Node 20 or later and npm.</p></Notice></Field>
        <Field label="Browser data directory" hint="Found by North. Change it only if North picked the wrong browser."><input required value={draft.data_directory} onChange={event => setDraft({ ...draft, data_directory: event.target.value })}/></Field>
        <Field label="Profile directory"><input required value={draft.profile_directory} onChange={event => setDraft({ ...draft, profile_directory: event.target.value })}/></Field>
      </> : <Field label="Browser window" wide><label><input type="checkbox" checked={draft.headed} onChange={event => setDraft({ ...draft, headed: event.target.checked })}/> Show the browser window so I can help when needed</label></Field>}
      <Field label="Use" wide><label><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })}/> Allow North to select this profile. Calls still follow my approval mode.</label></Field>
    </EntityForm>}
  </Panel>;
}
