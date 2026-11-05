// What the lights should do for this moment of the music, from the spectrum the browser hears.
// The sound never leaves the browser: only the colours this works out are sent to north.

export type Mode = "pulse" | "spectrum";

export interface Frame {
  hue: number;
  saturation: number;
  brightness: number;
}

export interface Listening {
  envelope: number; // how loud, smoothed: quick to rise, slow to fall, so the lights do not flicker
  average: number; // the bass over the last few seconds, which a beat stands out from
  // The loudest and quietest the music has been lately. The analyser reports decibels, so even the
  // quiet between two kicks reads loud: how loud a moment is, is where it sits between these.
  high: number;
  low: number;
  lastBeat: number; // ms
  hue: number;
  brightness: number;
}

// Brightness never drops below this, nor moves more than STEP between frames: lights that
// follow music must never strobe.
export const FLOOR = 10;
export const STEP = 35;

export const listening = (): Listening => ({ envelope: 0, average: 0, high: 0, low: 1, lastBeat: -Infinity, hue: 30, brightness: FLOOR });

const clamp = (value: number, low = 0, high = 1) => Math.max(low, Math.min(high, value));

/** How loud the music is between two frequencies, 0 to 1, from the analyser's byte spectrum. */
export function band(spectrum: Uint8Array, sampleRate: number, low: number, high: number): number {
  const binHz = sampleRate / 2 / spectrum.length;
  const from = Math.max(0, Math.floor(low / binHz));
  const to = Math.min(spectrum.length - 1, Math.ceil(high / binHz));
  let sum = 0;
  for (let i = from; i <= to; i++) sum += spectrum[i];
  return to >= from ? sum / ((to - from + 1) * 255) : 0;
}

/** Where the sound's weight sits, in Hz: low for a bass line, high for cymbals and voices. */
export function centroid(spectrum: Uint8Array, sampleRate: number): number {
  const binHz = sampleRate / 2 / spectrum.length;
  let weighted = 0;
  let total = 0;
  for (let i = 1; i < spectrum.length; i++) {
    weighted += spectrum[i] * i * binHz;
    total += spectrum[i];
  }
  return total ? weighted / total : 0;
}

/** The hue *from* moves toward *to* by *share*, the short way round the colour wheel. */
const towards = (from: number, to: number, share: number) => (from + (((to - from + 540) % 360) - 180) * share + 360) % 360;

/**
 * The next frame for the lights, and what to remember for the one after.
 *
 * Pulse: brightness follows the bass, and each beat moves the colour on.
 * Spectrum: colour follows where the sound sits (deep red to violet), brightness its loudness.
 */
export function nextFrame(
  state: Listening, spectrum: Uint8Array, sampleRate: number, mode: Mode, sensitivity: number, now: number,
): { state: Listening; frame: Frame; beat: boolean } {
  const bass = band(spectrum, sampleRate, 30, 150);
  const raw = mode === "pulse" ? bass : band(spectrum, sampleRate, 30, 4000);
  const high = raw > state.high ? raw : state.high * 0.995 + raw * 0.005;
  const low = raw < state.low ? raw : state.low * 0.995 + raw * 0.005;
  const loud = clamp(((raw - low) / Math.max(high - low, 0.08)) * (sensitivity / 2.5));
  const beat = bass > 0.15 && bass > state.average * 1.35 && now - state.lastBeat > 280;
  const average = state.average * 0.95 + bass * 0.05;
  const envelope = state.envelope + (loud - state.envelope) * (loud > state.envelope ? 0.7 : 0.25);

  let hue = state.hue;
  if (mode === "pulse") hue = (hue + (beat ? 37 : 0.6)) % 360;
  else {
    const where = clamp((Math.log(Math.max(centroid(spectrum, sampleRate), 100)) - Math.log(100)) / (Math.log(4000) - Math.log(100)));
    hue = towards(hue, where * 280, 0.3);
  }
  const wanted = mode === "pulse" && beat ? 100 : FLOOR + (100 - FLOOR) * envelope;
  const brightness = clamp(wanted, state.brightness - STEP, state.brightness + STEP);
  return {
    state: { envelope, average, high, low, lastBeat: beat ? now : state.lastBeat, hue, brightness },
    frame: { hue: Math.round(hue), saturation: 100, brightness: Math.round(clamp(brightness, FLOOR, 100)) },
    beat,
  };
}

/** One frame per light, each a little further round the wheel, so a room of lights is not one colour. */
export const spread = (frame: Frame, ids: string[]) =>
  ids.map((id, index) => ({ id, ...frame, hue: Math.round((frame.hue + index * 40) % 360) }));
