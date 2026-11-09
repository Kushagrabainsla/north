import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, post } from "../api";
import { Empty, ErrorNotice, Loading, PageHeader, Panel } from "../components";
import { useResource } from "../hooks";
import { addDeviceRequest, brandOf, deviceRequest, failures, inScope, scanSummary, kelvinToRgb, latestWins, swatch, type Light, type NetworkDevice, type Scan, type LightChange, type LightsOverview, type Scope } from "../lights";
import { sliceMs, type Levels } from "../listen";
import listenWorklet from "../listen.worklet.ts?worker&url";
import { hear, listening, spread, type Frame, type Mode } from "../music";
import type { Conversation } from "../types";

type Applied = { lights: Light[]; errors: { id: string; error: string }[] };

const message = (error: unknown) => (error instanceof Error ? error.message : String(error));

// Your lights, plugs and switches of every brand, controlled by you: what you set happens at
// once, with no approval card (CODING_STYLE §7.3). "My devices" acts only on the ones you
// marked as yours. A brand north does not control yet is added by asking north, which writes
// what it needs; its devices then show here like the Kasa ones.
export function Lights() {
  const overview = useResource<LightsOverview>("/web/api/lights");
  const [lights, setLights] = useState<Light[]>([]);
  const [scope, setScope] = useState<Scope | null>(null);
  const [notice, setNotice] = useState("");
  const [looking, setLooking] = useState(false);
  const [adding, setAdding] = useState(false);
  useEffect(() => { if (overview.data) setLights(overview.data.lights); }, [overview.data]);

  const chosen: Scope = scope ?? (lights.some((light) => light.mine) ? "mine" : "all");
  const shown = inScope(lights, chosen);
  const merge = (changed: Light[]) => setLights((current) => current.map((light) => changed.find((c) => c.id === light.id) ?? light));
  const named = (id: string) => lights.find((light) => light.id === id)?.name ?? id;
  const applyToShown = async (change: LightChange) => {
    try {
      const out = await post<Applied>("/web/api/lights/apply", { ids: shown.map((light) => light.id), ...change });
      merge(out.lights);
      setNotice(failures(out.errors, named));
    } catch (error) {
      setNotice(message(error));
    }
  };
  const lookAgain = async () => {
    setLooking(true);
    try {
      overview.setData(await api<LightsOverview>("/web/api/lights?look_again=true"));
    } catch (error) {
      setNotice(message(error));
    } finally {
      setLooking(false);
    }
  };
  const markMine = async (light: Light, mine: boolean) => {
    try {
      await api(`/web/api/lights/${encodeURIComponent(light.id)}/mine`, { method: "PUT", body: JSON.stringify({ mine }) });
      merge([{ ...light, mine }]);
    } catch (error) {
      setNotice(message(error));
    }
  };
  const mineCount = lights.filter((light) => light.mine).length;
  const locked = overview.data?.locked ?? [];
  // The Wi-Fi hint only when nothing was found: bulbs that refuse the login were found.
  const empty = lights.length
    ? "None of these devices is marked as yours yet: switch to All devices and press ☆ Mine on yours."
    : locked.length
      ? "No device answers yet: the Kasa ones need their TP-Link login."
      : `${overview.data?.problem || "No devices were found."} North finds Kasa bulbs on the network of the computer it runs on: make sure that computer and your bulbs are on the same Wi-Fi, then press Look again. Another brand: press Add a device.`;

  return <div className="page lights-page">
    <PageHeader
      eyebrow="Home"
      title="Devices"
      subtitle="Your lights, plugs and switches, of any brand. What you set here happens at once, with no approval card."
      actions={<>
        <button className="ghost-button" onClick={() => setAdding(!adding)} aria-expanded={adding}>Add a device</button>
        <button className="ghost-button" onClick={lookAgain} disabled={looking}>{looking ? "Looking…" : "Look again"}</button>
      </>}
    />
    {adding && <AddDevice onCancel={() => setAdding(false)}/>}
    {overview.loading ? <Loading/> : overview.error ? <ErrorNotice message={overview.error}/> : <>
      <section className="lights-bar">
        <div className="session-tabs" role="tablist" aria-label="Which devices">
          <button type="button" role="tab" aria-selected={chosen === "mine"} className={`session-tab ${chosen === "mine" ? "active" : ""}`} onClick={() => setScope("mine")}>My devices ({mineCount})</button>
          <button type="button" role="tab" aria-selected={chosen === "all"} className={`session-tab ${chosen === "all" ? "active" : ""}`} onClick={() => setScope("all")}>All devices ({lights.length})</button>
        </div>
        <div className="lights-group" aria-label={chosen === "mine" ? "Your devices together" : "All devices together"}>
          <button type="button" disabled={!shown.length} onClick={() => applyToShown({ on: true })}>All on</button>
          <button type="button" disabled={!shown.length} onClick={() => applyToShown({ on: false })}>All off</button>
          {(overview.data?.scenes ?? []).map((scene) => <button type="button" key={scene} className="lights-scene" disabled={!shown.length} onClick={() => applyToShown({ scene })}>{scene}</button>)}
        </div>
      </section>
      {shown.length > 0 && <MusicSync lights={shown} scope={chosen} onStopped={() => void overview.reload()}/>}
      {locked.length > 0 && <p className="lights-notice lights-locked">
        {locked.length} Kasa light{locked.length === 1 ? "" : "s"} found {locked.length === 1 ? "needs its" : "need their"} TP-Link login, the Kasa app account {locked.length === 1 ? "it was" : "they were"} set up with: {locked.join(", ")}. <Link to="/settings">Add it in Settings → Secrets as kasa_email and kasa_password</Link>, then press Look again.
      </p>}
      {(lights.length > 0 || locked.length > 0) && overview.data?.problem && !locked.length && <p className="lights-notice">{overview.data.problem}</p>}
      {notice && <p className="lights-notice" role="status">{notice}</p>}
      {shown.length
        ? <div className="lights-grid">{shown.map((light) => <LightCard key={light.id} light={light} onChanged={merge} onNotice={setNotice} onMine={markMine}/>)}</div>
        : <Empty>{empty}</Empty>}
    </>}
  </div>;
}

// Adding a device north does not control yet. North looks on your network first and lists what it
// recognises, saying for each how it would reach it or why it cannot yet; choosing one opens a session
// where north writes what it needs, asking only for what you must do (press a button, a login). What
// discovery cannot see (a device only its cloud app reaches) is described in your own words below.
// Every device on your network, grouped by what North can do with it: add one it knows the way into,
// see the ones already here, and try any other, in a session where North works out whether it can.
const GROUPS: [NetworkDevice["connect"][], string][] = [
  [["add"], "North can connect"],
  [["here"], "Already on this page"],
  [["try", "none"], "Other devices on your network"],
];

function AddDevice({ onCancel }: { onCancel: () => void }) {
  const navigate = useNavigate();
  const [scan, setScan] = useState<Scan | null>(null);
  const [looking, setLooking] = useState(false);
  const [what, setWhat] = useState("");
  const [sending, setSending] = useState("");
  const [error, setError] = useState("");
  const look = async () => {
    setLooking(true); setError("");
    try { setScan(await api<Scan>("/web/api/lights/discover")); }
    catch (err) { setError(message(err)); }
    finally { setLooking(false); }
  };
  useEffect(() => { void look(); }, []);
  const ask = async (key: string, title: string, request: string) => {
    setSending(key); setError("");
    try {
      const session = await post<Conversation>("/web/api/conversations", { title: `Add ${title}`.slice(0, 80) });
      await post(`/web/api/conversations/${session.id}/turns`, { prompt: request });
      navigate(`/sessions/${session.id}`);
    } catch (err) {
      setError(message(err));
      setSending("");
    }
  };
  const devices = scan?.devices ?? [];
  return <Panel title="Add a device" label="Find devices" className="add-device" actions={<button className="ghost-button" type="button" onClick={onCancel}>Close</button>}>
    <p className="setup-copy">North lists every device on your network, named as far as it can tell. Choose one to connect: North works out how to control it in a session you can follow, asking you only for what you need to do, and tells you plainly if it cannot. Once it works, it shows up here with the same controls and music sync.</p>
    {looking ? <p className="setup-copy">Looking at every address on your network… this takes a few seconds.</p>
      : scan && <>
        {GROUPS.map(([kinds, title]) => {
          const group = devices.filter((device) => kinds.includes(device.connect));
          return group.length > 0 && <div className="found-group" key={title}>
            <p className="found-group-title">{title} ({group.length})</p>
            <div className="provider-list">{group.map((device) => <DeviceRow key={device.id} device={device} sending={sending} onConnect={() => ask(device.id, device.name, deviceRequest(device))}/>)}</div>
          </div>;
        })}
        <p className="setup-copy">{scanSummary(scan)}</p>
      </>}
    <div className="setup-actions">
      <button className="ghost-button" type="button" onClick={look} disabled={looking}>{looking ? "Looking…" : "Look again"}</button>
    </div>
    <div className="setup-form">
      <label>Not listed? Say what it is: its brand and model, and where it is if you know (its address, or the app you use for it).
        <textarea value={what} rows={2} placeholder="A Philips Hue bridge at 192.168.1.20" onChange={(event) => setWhat(event.target.value)}/>
      </label>
      {error && <ErrorNotice message={error}/>}
      <div className="setup-actions">
        <button className="primary-button" type="button" disabled={!what.trim() || Boolean(sending)} onClick={() => ask("typed", what.trim(), addDeviceRequest(what))}>{sending === "typed" ? "Asking North…" : "Ask North to add it"}</button>
      </div>
    </div>
  </Panel>;
}

function DeviceRow({ device, sending, onConnect }: { device: NetworkDevice; sending: string; onConnect: () => void }) {
  const where = [device.maker, device.address, device.role].filter(Boolean).join(" · ");
  const about = device.found
    ? (device.found.cannot ? `A ${device.found.brand} ${device.found.kind}. North can't connect to it directly yet: ${device.found.cannot}.` : `A ${device.found.brand} ${device.found.kind}. North reaches it through ${device.found.reach}.`)
    : device.offers.length ? `Offers ${device.offers.join(", ")}.` : device.hint ? `${device.hint}.` : "";
  return <div className="provider-row">
    <div>
      <b>{device.name}</b>
      <small>{where}</small>
      {about && <small>{about}</small>}
    </div>
    {device.connect === "here" ? <span className="provider-state configured">On this page</span>
      : device.connect === "none" ? null
      : <button className={device.connect === "add" ? "primary-button" : "ghost-button"} type="button" disabled={Boolean(sending)} onClick={onConnect}>
        {sending === device.id ? "Asking North…" : device.connect === "add" ? "Add" : "Try to connect"}
      </button>}
  </div>;
}

function LightCard({ light, onChanged, onNotice, onMine }: {
  light: Light;
  onChanged: (lights: Light[]) => void;
  onNotice: (notice: string) => void;
  onMine: (light: Light, mine: boolean) => void;
}) {
  const [draft, setDraft] = useState(light);
  const [mode, setMode] = useState<"white" | "colour">(light.hue !== undefined || !light.can.white ? "colour" : "white");
  const holding = useRef(false);
  // While a slider is held, what the light reports back must not pull it from under your finger.
  useEffect(() => { if (!holding.current) setDraft(light); }, [light]);
  const reported = useRef(light);
  reported.current = light;
  const send = useMemo(
    () => latestWins(
      (change: LightChange) => post<Applied>("/web/api/lights/apply", { ids: [light.id], ...change }),
      (out) => {
        onChanged(out.lights);
        if (out.errors.length) {
          onNotice(failures(out.errors, () => light.name));
          setDraft(reported.current); // the card shows what the light is doing, not what was asked
        }
      },
      (error) => { onNotice(`${light.name}: ${message(error)}`); setDraft(reported.current); },
    ),
    [light.id],
  );
  const set = (change: LightChange) => {
    setDraft((current) => ({ ...current, ...change, on: change.on ?? true }));
    send(change);
  };
  const hold = { onPointerDown: () => { holding.current = true; }, onPointerUp: () => { holding.current = false; } };
  const white = light.can.white;
  const kelvin = draft.kelvin ?? white?.[0] ?? 2700;
  const hue = draft.hue ?? 30;
  const saturation = draft.saturation ?? 100;
  const off = Boolean(light.unreachable);

  return <article className={`light-card ${draft.on ? "is-on" : ""}`}>
    <header>
      <span className="light-swatch" style={{ background: swatch(draft) }} aria-hidden="true"/>
      <div className="light-name"><b>{light.name}</b><small>{brandOf(light)}</small></div>
      <button type="button" className={`light-mine ${light.mine ? "active" : ""}`} aria-pressed={light.mine} onClick={() => onMine(light, !light.mine)} title={light.mine ? "Yours: My lights includes it" : "Mark as yours"}>{light.mine ? "★" : "☆"} Mine</button>
      <button type="button" className="light-power" aria-pressed={draft.on} disabled={off} onClick={() => set({ on: !draft.on })}>{draft.on ? "On" : "Off"}</button>
    </header>
    {light.unreachable && <p className="lights-notice">Not answering: {light.unreachable}</p>}
    {light.can.dim && <label className="light-slider">
      <span>Brightness <em>{draft.brightness ?? 100}%</em></span>
      <input type="range" min={1} max={100} value={draft.brightness ?? 100} disabled={off} {...hold} onChange={(e) => set({ brightness: Number(e.target.value) })}/>
    </label>}
    {light.can.color && white && <div className="session-tabs light-modes" role="tablist" aria-label={`${light.name} mode`}>
      <button type="button" role="tab" aria-selected={mode === "white"} className={`session-tab ${mode === "white" ? "active" : ""}`} disabled={off} onClick={() => { setMode("white"); set({ kelvin }); }}>White</button>
      <button type="button" role="tab" aria-selected={mode === "colour"} className={`session-tab ${mode === "colour" ? "active" : ""}`} disabled={off} onClick={() => { setMode("colour"); set({ hue, saturation }); }}>Colour</button>
    </div>}
    {white && (mode === "white" || !light.can.color) && <label className="light-slider">
      <span>Warmth <em>{kelvin}K</em></span>
      <input type="range" className="light-white" min={white[0]} max={white[1]} step={100} value={kelvin} disabled={off} {...hold}
        style={{ background: `linear-gradient(90deg, rgb(${kelvinToRgb(white[0]).join(" ")}), rgb(${kelvinToRgb(white[1]).join(" ")}))` }}
        onChange={(e) => set({ kelvin: Number(e.target.value) })}/>
    </label>}
    {light.can.color && (mode === "colour" || !white) && <>
      <label className="light-slider">
        <span>Colour <em>{hue}°</em></span>
        <input type="range" className="light-hue" min={0} max={360} value={hue} disabled={off} {...hold} onChange={(e) => set({ hue: Number(e.target.value), saturation })}/>
      </label>
      <label className="light-slider">
        <span>Saturation <em>{saturation}%</em></span>
        <input type="range" min={0} max={100} value={saturation} disabled={off} {...hold} style={{ background: `linear-gradient(90deg, hsl(${hue} 0% 70%), hsl(${hue} 100% 50%))` }} onChange={(e) => set({ hue, saturation: Number(e.target.value) })}/>
      </label>
    </>}
  </article>;
}

// The lights get a frame at most this often, and at once on a beat; a beat rises over BEAT_FADE_MS.
const SEND_EVERY_MS = 100;
const BEAT_FADE_MS = 60;

const MODES: { mode: Mode; name: string; does: string }[] = [
  { mode: "beat", name: "Beat", does: "The lights flash and change colour with every beat." },
  { mode: "mood", name: "Mood", does: "Colours drift with the feel of the song: warm for deep, bass-heavy songs, cool for bright ones." },
];

const strengthWord = (strength: number) => (strength < 0.35 ? "Gentle" : strength > 0.7 ? "Strong" : "Medium");

type LiveSend = { frames: (Frame & { id: string })[]; beat: boolean };

// Listens through this device's microphone and sends the lights a colour for each moment of the
// music. The sound stays in the browser: only colours go to north, which sends them on. The
// listening runs on the audio thread, which keeps time by the sound itself.
function MusicSync({ lights, scope, onStopped }: { lights: Light[]; scope: Scope; onStopped: () => void }) {
  const [mode, setMode] = useState<Mode>("beat");
  const [strength, setStrength] = useState(0.6);
  const [phase, setPhase] = useState<"idle" | "starting" | "running" | "stopping">("idle");
  const running = phase === "running";
  const attempt = useRef(0); // a start finding this moved on was stopped or left while it waited
  const [level, setLevel] = useState(0);
  const [beats, setBeats] = useState(0);
  const [note, setNote] = useState("");
  const settings = useRef({ mode, strength });
  settings.current = { mode, strength };
  const session = useRef<{ stop: () => Promise<void> } | null>(null);
  const colourful = lights.filter((light) => light.can.color && !light.unreachable);

  const stop = async () => {
    const ending = session.current;
    session.current = null;
    attempt.current += 1;
    setPhase("stopping");
    setLevel(0);
    if (ending) {
      await ending.stop();
      onStopped();
    }
    setPhase("idle");
  };
  useEffect(() => () => { attempt.current += 1; void session.current?.stop(); }, []);

  const start = async () => {
    attempt.current += 1;
    const mine = attempt.current;
    const stale = () => attempt.current !== mine;
    setNote("");
    setPhase("starting");
    await listen(stale);
  };

  const listen = async (stale: () => boolean) => {
    const giveUp = (why: string) => { setNote(why); setPhase("idle"); };
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      giveUp("Browsers allow the microphone only on a secure page: open the dashboard on the computer running north (http://127.0.0.1…), or over HTTPS.");
      return;
    }
    if (!colourful.length) {
      giveUp("None of these lights can show colour.");
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
    } catch (error) {
      const name = error instanceof DOMException ? error.name : "";
      giveUp(name === "NotAllowedError" ? "The microphone was not allowed. Allow it for this page in your browser, then press Start."
        : name === "NotFoundError" ? "No microphone was found on this device." : `The microphone could not start: ${message(error)}`);
      return;
    }
    if (stale()) { // the prompt can sit open for as long as you like
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    const context = new AudioContext();
    const release = async () => {
      stream.getTracks().forEach((track) => track.stop());
      await context.close().catch(() => undefined);
    };
    let listener: AudioWorkletNode;
    let following: string[] = [];
    try {
      await context.audioWorklet.addModule(listenWorklet);
      listener = new AudioWorkletNode(context, "north-listen", { numberOfInputs: 1, numberOfOutputs: 0 });
      context.createMediaStreamSource(stream).connect(listener);
      following = (await post<{ following: string[] }>("/web/api/lights/live/start", { ids: colourful.map((light) => light.id) })).following;
    } catch (error) {
      await release();
      if (!stale()) giveUp(message(error));
      return;
    }
    if (stale()) {
      await release();
      await post("/web/api/lights/live/stop", {}).catch(() => undefined);
      return;
    }
    const send = latestWins(
      ({ frames, beat }: LiveSend) => post("/web/api/lights/live", { frames, beat, transition_ms: beat ? BEAT_FADE_MS : SEND_EVERY_MS }),
      () => undefined,
      (error) => { setNote(message(error)); void stop(); },
      ({ beat }) => beat,
    );
    const ms = sliceMs(context.sampleRate);
    let state = listening();
    let clock = 0;
    let sentAt = -Infinity;
    listener.port.onmessage = ({ data }: MessageEvent<Levels>) => {
      const out = hear(state, data, ms, settings.current.mode, settings.current.strength);
      state = out.state;
      clock += ms;
      if (out.beat) setBeats((count) => count + 1);
      if (out.beat || clock - sentAt >= SEND_EVERY_MS) {
        sentAt = clock;
        setLevel(out.frame.brightness);
        send({ frames: spread(out.frame, following), beat: out.beat });
      }
    };
    session.current = { stop: async () => { listener.port.onmessage = null; await release(); await post("/web/api/lights/live/stop", {}).catch(() => undefined); } };
    setPhase("running");
  };

  const chosen = MODES.find((entry) => entry.mode === mode) ?? MODES[0];
  return <section className="music-sync" aria-label="Music sync">
    <div className="music-sync-text">
      <b>Music sync</b>
      <small>Listens through this device's microphone. The sound stays in your browser; only colours go to your lights. Follows {scope === "mine" ? "your" : "all"} {colourful.length} colour light{colourful.length === 1 ? "" : "s"}, and puts them back when you stop.</small>
    </div>
    <div className="music-sync-mode">
      <div className="session-tabs" role="tablist" aria-label="How the lights follow the music">
        {MODES.map((entry) => <button key={entry.mode} type="button" role="tab" aria-selected={mode === entry.mode} className={`session-tab ${mode === entry.mode ? "active" : ""}`} onClick={() => setMode(entry.mode)}>{entry.name}</button>)}
      </div>
      <small>{chosen.does}</small>
    </div>
    <label className="light-slider music-strength">
      <span>Strength <em>{strengthWord(strength)}</em></span>
      <input type="range" min={0} max={1} step={0.05} value={strength} onChange={(e) => setStrength(Number(e.target.value))}/>
      <span className="music-strength-ends" aria-hidden="true"><small>Gentle</small><small>Strong</small></span>
    </label>
    <span className="music-heard" aria-live="off">
      <span key={beats} className={`music-beat ${running && beats ? "on" : ""}`} title="Lights up on each beat it hears"/>
      <span className="music-meter" aria-hidden="true"><span style={{ width: `${level}%` }}/></span>
    </span>
    <button type="button" className={`light-power ${running ? "" : "music-start"}`} aria-pressed={running} disabled={phase === "starting" || phase === "stopping"} onClick={() => (running ? void stop() : void start())}>{phase === "starting" ? "Starting…" : running ? "Stop" : "Start"}</button>
    {note && <p className="lights-notice music-note" role="status">{note}</p>}
  </section>;
}
