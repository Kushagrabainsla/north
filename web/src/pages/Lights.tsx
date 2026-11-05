import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, post } from "../api";
import { Empty, ErrorNotice, Loading, PageHeader } from "../components";
import { useResource } from "../hooks";
import { inScope, kelvinToRgb, latestWins, swatch, type Light, type LightChange, type LightsOverview, type Scope } from "../lights";
import { sliceMs, type Levels } from "../listen";
import listenWorklet from "../listen.worklet.ts?worker&url";
import { hear, listening, spread, type Frame, type Mode } from "../music";

type Applied = { lights: Light[]; errors: { id: string; error: string }[] };

const message = (error: unknown) => (error instanceof Error ? error.message : String(error));

// Your Kasa lights, controlled by you: what you set happens at once, with no approval
// card (CODING_STYLE §7.3). "My lights" acts only on the lights you marked as yours.
export function Lights() {
  const overview = useResource<LightsOverview>("/web/api/lights");
  const [lights, setLights] = useState<Light[]>([]);
  const [scope, setScope] = useState<Scope | null>(null);
  const [notice, setNotice] = useState("");
  const [looking, setLooking] = useState(false);
  useEffect(() => { if (overview.data) setLights(overview.data.lights); }, [overview.data]);

  const chosen: Scope = scope ?? (lights.some((light) => light.mine) ? "mine" : "all");
  const shown = inScope(lights, chosen);
  const merge = (changed: Light[]) => setLights((current) => current.map((light) => changed.find((c) => c.id === light.id) ?? light));
  const named = (id: string) => lights.find((light) => light.id === id)?.name ?? id;
  const applyToShown = async (change: LightChange) => {
    try {
      const out = await post<Applied>("/web/api/lights/apply", { ids: shown.map((light) => light.id), ...change });
      merge(out.lights);
      setNotice(out.errors.map((e) => `${named(e.id)}: ${e.error}`).join(" · "));
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
    ? "None of these lights is marked as yours yet: switch to All lights and press ☆ Mine on yours."
    : locked.length
      ? "No light answers yet: they need their TP-Link login."
      : `${overview.data?.problem || "No Kasa devices were found."} North finds bulbs on the network of the computer it runs on: make sure that computer and your bulbs are on the same Wi-Fi, then press Look again.`;

  return <div className="page lights-page">
    <PageHeader
      eyebrow="Home"
      title="Lights"
      subtitle="Your Kasa lights. What you set here happens at once, with no approval card."
      actions={<button className="ghost-button" onClick={lookAgain} disabled={looking}>{looking ? "Looking…" : "Look again"}</button>}
    />
    {overview.loading ? <Loading/> : overview.error ? <ErrorNotice message={overview.error}/> : <>
      <section className="lights-bar">
        <div className="session-tabs" role="tablist" aria-label="Which lights">
          <button type="button" role="tab" aria-selected={chosen === "mine"} className={`session-tab ${chosen === "mine" ? "active" : ""}`} onClick={() => setScope("mine")}>My lights ({mineCount})</button>
          <button type="button" role="tab" aria-selected={chosen === "all"} className={`session-tab ${chosen === "all" ? "active" : ""}`} onClick={() => setScope("all")}>All lights ({lights.length})</button>
        </div>
        <div className="lights-group" aria-label={chosen === "mine" ? "Your lights together" : "All lights together"}>
          <button type="button" disabled={!shown.length} onClick={() => applyToShown({ on: true })}>All on</button>
          <button type="button" disabled={!shown.length} onClick={() => applyToShown({ on: false })}>All off</button>
          {(overview.data?.scenes ?? []).map((scene) => <button type="button" key={scene} className="lights-scene" disabled={!shown.length} onClick={() => applyToShown({ scene })}>{scene}</button>)}
        </div>
      </section>
      {shown.length > 0 && <MusicSync lights={shown} scope={chosen} onStopped={() => void overview.reload()}/>}
      {locked.length > 0 && <p className="lights-notice lights-locked">
        {locked.length} light{locked.length === 1 ? "" : "s"} found {locked.length === 1 ? "needs its" : "need their"} TP-Link login, the Kasa app account {locked.length === 1 ? "it was" : "they were"} set up with: {locked.join(", ")}. <Link to="/settings">Add it in Settings → Connections → Kasa lights</Link>, then press Look again.
      </p>}
      {(lights.length > 0 || locked.length > 0) && overview.data?.problem && !locked.length && <p className="lights-notice">{overview.data.problem}</p>}
      {notice && <p className="lights-notice" role="status">{notice}</p>}
      {shown.length
        ? <div className="lights-grid">{shown.map((light) => <LightCard key={light.id} light={light} onChanged={merge} onError={(error) => setNotice(`${light.name}: ${message(error)}`)} onMine={markMine}/>)}</div>
        : <Empty>{empty}</Empty>}
    </>}
  </div>;
}

function LightCard({ light, onChanged, onError, onMine }: {
  light: Light;
  onChanged: (lights: Light[]) => void;
  onError: (error: unknown) => void;
  onMine: (light: Light, mine: boolean) => void;
}) {
  const [draft, setDraft] = useState(light);
  const [mode, setMode] = useState<"white" | "colour">(light.hue !== undefined || !light.can.white ? "colour" : "white");
  const holding = useRef(false);
  // While a slider is held, what the light reports back must not pull it from under your finger.
  useEffect(() => { if (!holding.current) setDraft(light); }, [light]);
  const send = useMemo(
    () => latestWins((change: LightChange) => post<Applied>("/web/api/lights/apply", { ids: [light.id], ...change }), (out) => onChanged(out.lights), onError),
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
      <div className="light-name"><b>{light.name}</b><small>{light.model} · {light.host}</small></div>
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
  const [running, setRunning] = useState(false);
  const [level, setLevel] = useState(0);
  const [beats, setBeats] = useState(0);
  const [note, setNote] = useState("");
  const settings = useRef({ mode, strength });
  settings.current = { mode, strength };
  const session = useRef<{ stop: () => Promise<void> } | null>(null);
  const colourful = lights.filter((light) => light.can.color && !light.unreachable);

  const stop = async () => {
    const running = session.current;
    session.current = null;
    setRunning(false);
    setLevel(0);
    if (running) {
      await running.stop();
      onStopped();
    }
  };
  useEffect(() => () => { void session.current?.stop(); }, []);

  const start = async () => {
    setNote("");
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setNote("Browsers allow the microphone only on a secure page: open the dashboard on the computer running north (http://127.0.0.1…), or over HTTPS.");
      return;
    }
    if (!colourful.length) {
      setNote("None of these lights can show colour.");
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
    } catch (error) {
      const name = error instanceof DOMException ? error.name : "";
      setNote(name === "NotAllowedError" ? "The microphone was not allowed. Allow it for this page in your browser, then press Start."
        : name === "NotFoundError" ? "No microphone was found on this device." : `The microphone could not start: ${message(error)}`);
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
      setNote(message(error));
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
    setRunning(true);
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
    <button type="button" className={`light-power ${running ? "" : "music-start"}`} aria-pressed={running} onClick={() => (running ? void stop() : void start())}>{running ? "Stop" : "Start"}</button>
    {note && <p className="lights-notice music-note" role="status">{note}</p>}
  </section>;
}
