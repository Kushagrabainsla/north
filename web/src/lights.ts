// What the Lights page needs beyond drawing: which lights a choice covers, the colour a
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

/** The lights a choice covers: yours, or every one on the network (a friend's too). */
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
 * position, not a queue of every position it passed through.
 */
export function latestWins<T, R>(send: (value: T) => Promise<R>, settled: (result: R) => void, failed: (error: unknown) => void) {
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
    if (busy) waiting = { value };
    else void run(value);
  };
}
