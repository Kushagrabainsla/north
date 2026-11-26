// Draws a view north made: A2UI v0.9 messages on north's kit catalog (views/catalog.py).
//
// Each part in the catalog is drawn by the kit part of the same role, so a view looks
// like every other page and can show nothing the kit cannot. The server checked the view
// before it got here; this still draws an unknown part as an error rather than guessing,
// and stops at a loop or a tree too deep to be a page.
//
// Inputs are bound both ways, as A2UI's are: a part shows the value at its `value` path
// and writes your change back, to the view's `/state` or to a field of its row. Using a
// part runs its action with each bound argument read from there at that moment.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Markdown, Panel } from "./components";
import {
  ActionBar, Badge, Button, Empty, EntityTable, Field, KeyValues, ListRow, Note, Notice, SectionHeader, Stack, Stat, StatGrid,
  Status, Text, type Column, type NoticeTone,
} from "./kit";
import { latestWins } from "./latest";
import type { Levels } from "./listen";
import { sliceMs } from "./listen";
import listenWorklet from "./listen.worklet.ts?worker&url";
import { hear, listening } from "./music";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface ViewComponent { id: string; component: string; [property: string]: unknown }
export interface ViewMessage {
  version: string;
  createSurface?: { surfaceId: string; catalogId: string };
  updateComponents?: { surfaceId: string; components: ViewComponent[] };
  updateDataModel?: { surfaceId: string; path?: string; value?: Json };
}
/** For each action, the paths its bound arguments read: {argument: path}. */
export type ActionBindings = Record<string, Record<string, string>>;

const MAX_DEPTH = 12;

/** The parts and the data a surface's messages describe. */
export function readMessages(messages: ViewMessage[]): { components: Map<string, ViewComponent>; data: Json } {
  const components = new Map<string, ViewComponent>();
  let data: Json = {};
  for (const message of messages) {
    for (const component of message.updateComponents?.components || []) components.set(component.id, component);
    const update = message.updateDataModel;
    if (update && (!update.path || update.path === "/")) data = update.value ?? {};
  }
  return { components, data };
}

/** `path` read from the data: from the top when it starts with "/", else from `scope`, the item a template draws. */
export function resolvePath(data: Json, path: string, scope = ""): Json | undefined {
  const full = path.startsWith("/") ? path : `${scope}/${path}`;
  let node: Json | undefined = data;
  for (const raw of full.split("/").filter(Boolean)) {
    const key = raw.replaceAll("~1", "/").replaceAll("~0", "~");
    if (Array.isArray(node)) node = node[Number(key)];
    else if (node && typeof node === "object") node = (node as Record<string, Json>)[key];
    else return undefined;
  }
  return node;
}

export const absolute = (path: string, scope: string) => (path.startsWith("/") ? path : `${scope}/${path}`);

/** The value at absolute `path`: what you set on the page there (or above it), else the data's. */
export function readAt(data: Json, local: Record<string, Json>, path: string): Json | undefined {
  const parts = path.split("/").filter(Boolean);
  for (let end = parts.length; end > 0; end--) {
    const prefix = `/${parts.slice(0, end).join("/")}`;
    if (prefix in local) return end === parts.length ? local[prefix] : resolvePath(local[prefix], `/${parts.slice(end).join("/")}`);
  }
  return resolvePath(data, path);
}

/** Each bound argument of an action, read where its part is: what the page sends when you use it. */
export function boundValues(bindings: Record<string, string>, scope: string, read: (path: string) => Json | undefined): Record<string, Json> {
  return Object.fromEntries(Object.entries(bindings).map(([argument, path]) => [argument, read(absolute(path, scope)) ?? null]));
}

/** An item's `id`, the one thing that says it is the same item when the data comes again. */
const itemId = (item: Json): string | undefined => {
  if (!item || typeof item !== "object" || Array.isArray(item)) return undefined;
  const id = (item as Record<string, Json>).id;
  return typeof id === "string" || typeof id === "number" ? String(id) : undefined;
};

/** The order to draw a list's items in, and the ids to remember for the next time.
 *
 *  A list a tool reads can come back in a new order on each refresh (lights in the order the
 *  network answered), so the row under your pointer became another item between look and click.
 *  Items keep the place they had when first shown, by `id`; new ones follow in the data's order,
 *  and one that is missing for a while gets its old place back. A list where an item has no id,
 *  or two share one, is drawn as it comes: nothing says which item is which. */
export function steadyOrder(items: Json[], known: string[] = []): { order: number[]; known: string[] } {
  const ids = items.map(itemId);
  const unique = ids.every((id): id is string => id !== undefined) && new Set(ids).size === ids.length;
  if (!unique) return { order: items.map((_, index) => index), known };
  const remembered = [...known, ...ids.filter((id) => !known.includes(id))];
  const place = new Map(remembered.map((id, index) => [id, index]));
  const order = items.map((_, index) => index).sort((a, b) => (place.get(ids[a]!) ?? 0) - (place.get(ids[b]!) ?? 0));
  return { order, known: remembered };
}

/** An image address a view may show: https, a data: image, or a page of this dashboard. */
export const showableImage = (src: string) => /^https:\/\//i.test(src) || /^data:image\//i.test(src) || /^\/(?!\/)/.test(src);

export interface ViewSurfaceProps {
  messages: ViewMessage[];
  bindings?: ActionBindings;
  /** Runs an action with its bound values. Absent for a draft: its parts do nothing until it is added.
   *  `live` marks the Microphone's stream of calls: sent quietly, with no reload after each. */
  onAction?: (name: string, values: Record<string, Json>, live?: boolean) => unknown;
  busyAction?: string;
}

export function ViewSurface({ messages, bindings = {}, onAction, busyAction }: ViewSurfaceProps) {
  const { components, data } = readMessages(messages);
  // What you set on this page, by absolute path. Row values give way to fresh data; the view's state stays.
  const [local, setLocal] = useState<Record<string, Json>>({});
  // For each repeated list, by its absolute path: the ids of its items in the order they are drawn.
  const orders = useRef<Record<string, string[]>>({});
  useEffect(() => {
    setLocal((current) => Object.fromEntries(Object.entries(current).filter(([path]) => path.startsWith("/state/"))));
  }, [messages]);

  const read = (path: string, overrides: Record<string, Json> = {}) => readAt(data, { ...local, ...overrides }, path);
  const set = (path: string, value: Json) => setLocal((current) => ({ ...current, [path]: value }));
  const run = (name: string, scope: string, overrides: Record<string, Json> = {}, live = false) =>
    onAction?.(name, boundValues(bindings[name] || {}, scope, (path) => read(path, overrides)), live);

  const draw = (id: string, scope: string, trail: string[]): ReactNode => {
    const part = components.get(id);
    if (!part) return <Notice key={id} tone="error">This view names a part, {id}, that it does not have.</Notice>;
    if (trail.includes(id) || trail.length >= MAX_DEPTH) return <Notice key={id} tone="error">This view is nested too deep to draw.</Notice>;
    const inner = [...trail, id];
    const children = (): ReactNode[] => {
      const list = part.children;
      if (Array.isArray(list)) return list.map(child => <Frag key={String(child)}>{draw(String(child), scope, inner)}</Frag>);
      if (list && typeof list === "object") {
        const { path, componentId } = list as { path: string; componentId: string };
        const items = read(absolute(path, scope));
        const base = absolute(path, scope);
        if (!Array.isArray(items)) return [];
        // Each row still reads its own item by its place in the data; only the order drawn is steady.
        const { order, known } = steadyOrder(items, orders.current[base]);
        orders.current[base] = known;
        return order.map((index) => <Frag key={itemId(items[index]) ?? index}>{draw(componentId, `${base}/${index}`, inner)}</Frag>);
      }
      return [];
    };
    const valueOf = (property: unknown): Json | undefined => {
      if (property && typeof property === "object" && !Array.isArray(property) && "path" in property) {
        return read(absolute(String((property as { path: unknown }).path), scope));
      }
      return property as Json | undefined;
    };
    const say = (property: unknown) => {
      const resolved = valueOf(property);
      if (resolved === undefined || resolved === null) return "";
      return typeof resolved === "object" ? JSON.stringify(resolved) : String(resolved);
    };
    const event = (part.action as { event?: { name: string } } | undefined)?.event?.name;
    const where = part.value ? absolute(String((part.value as { path: string }).path), scope) : "";
    const current = where ? read(where) : undefined;
    // Set the input's value, then run its action with that value already in place.
    const change = (value: Json, fire = true) => {
      set(where, value);
      if (fire && event) run(event, scope, { [where]: value });
    };
    const disabled = !onAction;

    switch (part.component) {
      case "Column": return <Stack>{children()}</Stack>;
      case "Row": return <ActionBar align="start">{children()}</ActionBar>;
      case "Panel": return <Panel title={String(part.title)} label={part.label ? String(part.label) : undefined}><Stack>{children()}</Stack></Panel>;
      case "Heading": return <SectionHeader level={3} title={say(part.text)} description={part.description ? say(part.description) : undefined}/>;
      case "Text": return <Text>{say(part.text)}</Text>;
      case "Note": return <Note>{say(part.text)}</Note>;
      case "Markdown": return <Markdown>{say(part.text)}</Markdown>;
      case "StatGrid": return <StatGrid>{children()}</StatGrid>;
      case "Stat": return <Stat label={String(part.label)} value={say(part.value)} detail={part.detail ? say(part.detail) : undefined}/>;
      case "Table": {
        const rows = valueOf(part.rows);
        const columns = (part.columns as { key: string; label: string }[]).map((column): Column => ({ key: column.key, label: column.label }));
        const list = Array.isArray(rows) ? rows : [];
        return <EntityTable label={id} columns={columns} rows={list.map((row, index) => ({ row, index }))} rowKey={item => String(item.index)}
          cell={(item, key) => { const cell = resolvePath(item.row, `/${key}`); return cell === undefined || cell === null ? "" : typeof cell === "object" ? JSON.stringify(cell) : String(cell); }}
          empty={part.empty ? String(part.empty) : "Nothing to list."}/>;
      }
      case "ListItem": return <ListRow title={say(part.title)} description={part.description ? say(part.description) : undefined}
        meta={((part.meta as unknown[]) || []).map(say).filter(Boolean)} status={part.status ? <Status value={say(part.status)}/> : undefined}/>;
      case "Status": return <Status value={say(part.value)}/>;
      case "Badge": return <Badge>{say(part.text)}</Badge>;
      case "Notice": return <Notice tone={part.tone as NoticeTone}>{say(part.text)}</Notice>;
      case "Empty": return <Empty>{say(part.text)}</Empty>;
      case "KeyValues": return <KeyValues items={(part.items as { label: string; value: unknown }[]).map(item => [item.label, say(item.value)])}/>;
      case "Button": return <Button variant={(part.variant as "primary" | "secondary" | "danger") || "secondary"} disabled={disabled}
        busy={busyAction === event} busyLabel="Working…" title={disabled ? "Its parts work once the view is added." : undefined}
        onClick={() => event && run(event, scope)}>{String(part.label)}</Button>;
      case "Link": return <Button variant="quiet" to={String(part.to)}>{String(part.label)}</Button>;
      case "Image": {
        const src = say(part.src);
        return showableImage(src) ? <img className="kit-image" src={src} alt={String(part.alt)}/> : <Note>{String(part.alt)}</Note>;
      }
      case "TextField": return <Field label={String(part.label)}>
        <input value={current === undefined || current === null ? "" : String(current)} placeholder={part.placeholder ? String(part.placeholder) : undefined}
          disabled={disabled} onChange={(e) => change(e.target.value, false)} onKeyDown={(e) => { if (e.key === "Enter" && event) run(event, scope); }}/>
      </Field>;
      case "Select": {
        const options = Array.isArray(part.options) ? part.options.map(String) : (() => { const listed = valueOf(part.options); return Array.isArray(listed) ? listed.map(String) : []; })();
        return <Field label={String(part.label)}>
          <select value={current === undefined || current === null ? "" : String(current)} disabled={disabled} onChange={(e) => change(e.target.value)}>
            {options.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
        </Field>;
      }
      case "Switch": {
        const on = Boolean(current);
        return <ActionBar align="start"><span>{say(part.label)}</span>
          <Button size="sm" variant={on ? "primary" : "secondary"} aria-pressed={on} disabled={disabled} onClick={() => change(!on)}>{on ? "On" : "Off"}</Button>
        </ActionBar>;
      }
      case "Slider":
      case "ColorPicker": {
        const hue = part.component === "ColorPicker";
        const number = typeof current === "number" ? current : Number(current ?? (hue ? 0 : part.min ?? 0));
        const commit = () => { if (event) run(event, scope); };
        return <label className="kit-slider"><span>{say(part.label)}<em>{hue ? `${Math.round(number)}°` : `${number}${part.unit ? String(part.unit) : ""}`}</em></span>
          <input type="range" className={hue ? "kit-hue" : undefined} min={hue ? 0 : Number(part.min)} max={hue ? 360 : Number(part.max)}
            step={part.step ? Number(part.step) : 1} value={number} disabled={disabled}
            onChange={(e) => change(Number(e.target.value), false)} onPointerUp={commit} onKeyUp={commit}/>
        </label>;
      }
      case "Microphone": return <Microphone key={id} label={String(part.label)} mode={(part.mode as "beat" | "mood") || "beat"}
        perSecond={Number(part.per_second || 5)} disabled={disabled}
        onSound={(sound) => { set(where, sound); return event ? run(event, scope, { [where]: sound }, true) : undefined; }}/>;
      default: return <Notice tone="error">This view uses a part north's kit does not have: {part.component}.</Notice>;
    }
  };

  if (!components.has("root")) return <Notice tone="error">This view has no root part, so there is nothing to draw.</Notice>;
  return <div className="kit-view">{draw("root", "", [])}</div>;
}

function Frag({ children }: { children: ReactNode }) {
  return <>{children}</>;
}

interface Sound { [key: string]: Json; level: number; beat: boolean; hue: number; saturation: number; brightness: number }

/** Listens in the browser while you let it. The sound stays here: only what it hears goes to the action. */
function Microphone({ label, mode, perSecond, disabled, onSound }: {
  label: string; mode: "beat" | "mood"; perSecond: number; disabled: boolean;
  onSound: (sound: Sound) => unknown;
}) {
  const [running, setRunning] = useState(false);
  const [level, setLevel] = useState(0);
  const [note, setNote] = useState("");
  const stopRef = useRef<(() => Promise<void>) | null>(null);
  const soundRef = useRef(onSound);
  soundRef.current = onSound;
  useEffect(() => () => { void stopRef.current?.(); }, []);

  const stop = async () => {
    const ending = stopRef.current;
    stopRef.current = null;
    setRunning(false);
    setLevel(0);
    await ending?.();
  };

  const start = async () => {
    setNote("");
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setNote("Browsers allow the microphone only on a secure page: open the dashboard on the computer running north (http://127.0.0.1…), or over HTTPS.");
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
    } catch (error) {
      const name = error instanceof DOMException ? error.name : "";
      setNote(name === "NotAllowedError" ? "The microphone was not allowed. Allow it for this page in your browser, then press Listen."
        : name === "NotFoundError" ? "No microphone was found on this device." : "The microphone could not start.");
      return;
    }
    const context = new AudioContext();
    try {
      await context.audioWorklet.addModule(listenWorklet);
    } catch {
      stream.getTracks().forEach((track) => track.stop());
      await context.close().catch(() => undefined);
      setNote("The browser could not start listening.");
      return;
    }
    const listener = new AudioWorkletNode(context, "north-listen", { numberOfInputs: 1, numberOfOutputs: 0 });
    context.createMediaStreamSource(stream).connect(listener);
    const ms = sliceMs(context.sampleRate);
    const every = 1000 / Math.max(1, Math.min(10, perSecond));
    // One call in flight; a beat waiting is never replaced by the ordinary frames after it.
    const send = latestWins(async (sound: Sound) => soundRef.current(sound), () => undefined, () => undefined, (sound) => sound.beat);
    let state = listening();
    let clock = 0;
    let sentAt = -Infinity;
    listener.port.onmessage = ({ data }: MessageEvent<Levels>) => {
      const out = hear(state, data, ms, mode, 0.6);
      state = out.state;
      clock += ms;
      if (out.beat || clock - sentAt >= every) {
        sentAt = clock;
        const sound: Sound = { level: out.state.loud, beat: out.beat, ...out.frame };
        setLevel(out.state.loud);
        send(sound);
      }
    };
    stopRef.current = async () => {
      listener.port.onmessage = null;
      stream.getTracks().forEach((track) => track.stop());
      await context.close().catch(() => undefined);
    };
    setRunning(true);
  };

  return <Stack gap="sm">
    <div className="kit-listen">
      <span>{label}</span>
      <span className="kit-listen-meter" aria-label="How loud"><i style={{ width: `${Math.round(level * 100)}%` }}/></span>
      <Button size="sm" variant={running ? "secondary" : "primary"} aria-pressed={running} disabled={disabled}
        onClick={() => void (running ? stop() : start())}>{running ? "Stop" : "Listen"}</Button>
    </div>
    {note && <Note>{note}</Note>}
  </Stack>;
}
