// What the Devices page needs beyond drawing: which lights a choice covers, the colour a
// card shows, and a sender that never lets requests pile up behind a slider.

export interface Light {
  id: string;
  name: string;
  host: string;
  model: string;
  on: boolean;
  kind: "light" | "switch";
  can: { dim: boolean; color: boolean; white: [number, number] | null };
  brightness?: number;
  kelvin?: number;
  hue?: number;
  saturation?: number;
  mine: boolean;
  unreachable?: string;
  // The brand it comes through: "kasa", or the tool north wrote for it ("hue_lights").
  source: string;
}

export interface LightsOverview {
  lights: Light[];
  problem: string;
  locked: string[]; // found, but refusing every TP-Link login north has
  scenes: string[];
}

export type Scope = "mine" | "all";

export interface LightChange {
  on?: boolean;
  brightness?: number;
  hue?: number;
  saturation?: number;
  kelvin?: number;
  scene?: string;
}

/** The line under a device's name: its model and address for Kasa, the brand for any other. */
export function brandOf(light: Light): string {
  if (light.source === "kasa" || !light.source) return [light.model, light.host].filter(Boolean).join(" · ");
  return light.source.replaceAll("_", " ");
}

/** What north is asked when you add a device: your words, and where it should end up. */
export const addDeviceRequest = (what: string) =>
  `Add this to my Devices page, so I can control it there and from chat: ${what.trim()}`;

/** A brand Find devices recognised on a device: what it is, how it was found, and how north would reach it. */
export interface FoundDevice {
  id: string;
  brand: string;
  key: string;
  kind: string;
  name: string;
  address: string;
  how: string;
  reach: string;
  cannot: string; // why north cannot connect to it yet; "" when it can
  details: Record<string, string>;
}

/** One device on your network, with everything Find devices could tell about it. */
export interface NetworkDevice {
  id: string;
  address: string;
  mac: string;
  maker: string;
  hint: string; // what the maker's name alone does not say
  name: string;
  names: string[];
  offers: string[]; // in plain words: "Google Cast", "a web page, \"Shelly 1PM\""
  role: string; // "this computer" or "your router"
  found: FoundDevice | null;
  here: string; // "kasa", or north's tool for it: already on this page
  connect: "add" | "try" | "here" | "none";
}

export interface Scan {
  devices: NetworkDevice[];
}

/** What north is told when you choose a device Find devices recognised: everything discovery learned about it. */
export function foundRequest(found: FoundDevice): string {
  const details = Object.entries(found.details).filter(([, value]) => value).map(([key, value]) => `${key}=${value}`).join(", ");
  return addDeviceRequest(
    `the ${found.brand} ${found.kind} "${found.name}" at ${found.address}, found on the network by ${found.how}` +
      `${details ? ` (${details})` : ""}. North can reach it through ${found.reach}.`,
  );
}

/** Everything Find devices learned about a device, as north is told it. */
export function deviceFacts(device: NetworkDevice): string {
  const facts = [`address ${device.address}`];
  if (device.mac) facts.push(`hardware address ${device.mac}`);
  if (device.maker) facts.push(`network card made by ${device.maker}${device.hint ? ` (${device.hint})` : ""}`);
  if (device.names.length) facts.push(`it calls itself ${device.names.map((name) => `"${name}"`).join(", ")}`);
  if (device.offers.length) facts.push(`it offers ${device.offers.join("; ")}`);
  if (device.found) facts.push(`it announced itself as a ${device.found.brand} ${device.found.kind} by ${device.found.how}${device.found.cannot ? `, which North cannot connect to yet: ${device.found.cannot}` : ""}`);
  return facts.join("; ");
}

/** What north is asked when you choose a device: add one it knows the way into, or find a way into any other. */
export function deviceRequest(device: NetworkDevice): string {
  if (device.connect === "add" && device.found) return `${foundRequest(device.found)} What else Find devices saw: ${deviceFacts(device)}.`;
  return `Find out whether North can control this device on my network, and if it can, add it to my Devices page so I can control it there and from chat. If it cannot, tell me plainly why and what it would need. What Find devices saw: ${deviceFacts(device)}.`;
}

/** What a scan means in words: how many devices answered, and what no scan can see. */
export function scanSummary(scan: Scan): string {
  const listed = scan.devices.length;
  const heard = listed ? `North heard ${listed} device${listed === 1 ? "" : "s"} on your network.` : "North heard no devices on your network.";
  return `${heard} It cannot see a device that talks only to its maker's cloud or to a building's hub, one on another Wi-Fi network such as a guest network, or one that only uses Bluetooth: describe it below.`;
}

/** The lights a choice covers: yours, or every one on the network (a friend's too). */
/** Why some lights did not change, under their names: one line per reason, so "no Kasa device answers on
 * this network" is said once for every light it stopped, never under a light's network-card id. */
export function failures(errors: { id: string; error: string }[], nameOf: (id: string) => string): string {
  const named = new Map<string, string[]>();
  for (const { id, error } of errors) named.set(error, [...(named.get(error) ?? []), nameOf(id)]);
  return [...named].map(([why, names]) => `${names.join(", ")}: ${why}`).join(" · ");
}

export const inScope = (lights: Light[], scope: Scope) => (scope === "mine" ? lights.filter((light) => light.mine) : lights);

/** The colour of the light a white temperature gives, for a card's swatch (Tanner Helland's fit). */
export function kelvinToRgb(kelvin: number): [number, number, number] {
  const t = kelvin / 100;
  const clamp = (value: number) => Math.round(Math.max(0, Math.min(255, value)));
  const red = t <= 66 ? 255 : 329.698727446 * (t - 60) ** -0.1332047592;
  const green = t <= 66 ? 99.4708025861 * Math.log(t) - 161.1195681661 : 288.1221695283 * (t - 60) ** -0.0755148492;
  const blue = t >= 66 ? 255 : t <= 19 ? 0 : 138.5177312231 * Math.log(t - 10) - 305.0447927307;
  return [clamp(red), clamp(green), clamp(blue)];
}

/** What a card shows as the light's colour: off is dark, then colour or white, as the light reports. */
export function swatch(light: Light): string {
  if (!light.on) return "transparent";
  if (light.kind === "switch") return "var(--accent)";
  if (light.hue !== undefined) return `hsl(${light.hue} ${light.saturation ?? 100}% 55%)`;
  const [red, green, blue] = kelvinToRgb(light.kelvin ?? 2700);
  return `rgb(${red} ${green} ${blue})`;
}

/**
 * A sender with one request in flight: while one is on its way, a newer value replaces any
 * waiting one, and is sent when it returns. A slider dragged across its range sends its last
 * position, not a queue of every position it passed through. A waiting value that *keeps* says
 * must arrive (a beat) is replaced only by another such value, never by the ordinary ones after it.
 */
export function latestWins<T, R>(
  send: (value: T) => Promise<R>, settled: (result: R) => void, failed: (error: unknown) => void,
  keeps: (value: T) => boolean = () => false,
) {
  let busy = false;
  let waiting: { value: T } | null = null;
  const run = async (value: T) => {
    busy = true;
    try {
      settled(await send(value));
    } catch (error) {
      failed(error);
    } finally {
      busy = false;
      const next = waiting;
      waiting = null;
      if (next) void run(next.value);
    }
  };
  return (value: T) => {
    if (!busy) void run(value);
    else if (!waiting || !keeps(waiting.value) || keeps(value)) waiting = { value };
  };
}
