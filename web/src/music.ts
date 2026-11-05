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
  swell: number; // what is left of the last beat's lift, fading over a few frames
  hue: number;
  hueTarget: number; // where the colour is gliding to: a beat moves this, never the colour itself
  brightness: number;
}

// Brightness never drops below FLOOR, and moves at most STEP between frames, the colour at most
// HUE_STEP degrees: even a sudden change in the music becomes a quick glide, never a flicker.
// (35 points a frame, a beat straight to full and a 37° colour jump flickered on real KL125 bulbs.)
export const FLOOR = 10;
export const STEP = 15;
export const HUE_STEP = 12;

export const listening = (): Listening => ({
  envelope: 0, average: 0, high: 0, low: 1, lastBeat: -Infinity, swell: 0, hue: 30, hueTarget: 30, brightness: FLOOR,
});

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

/** The hue *from* moves toward *to* by *share*, at most HUE_STEP degrees, the short way round the colour wheel. */
function towards(from: number, to: number, share: number): number {
  const step = clamp((((to - from + 540) % 360) - 180) * share, -HUE_STEP, HUE_STEP);
  return (from + step + 360) % 360;
}

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
  const envelope = state.envelope + (loud - state.envelope) * (loud > state.envelope ? 0.45 : 0.12);
  const swell = beat ? 1 : state.swell * 0.6;

  let hueTarget = state.hueTarget;
  if (mode === "pulse") hueTarget = (hueTarget + (beat ? 30 : 0.4)) % 360;
  else {
    const where = clamp((Math.log(Math.max(centroid(spectrum, sampleRate), 100)) - Math.log(100)) / (Math.log(4000) - Math.log(100)));
    hueTarget = where * 280;
  }
  const hue = towards(state.hue, hueTarget, mode === "pulse" ? 0.25 : 0.15);
  const lift = mode === "pulse" ? clamp(envelope * 0.7 + swell * 0.4) : envelope;
  const brightness = clamp(FLOOR + (100 - FLOOR) * lift, state.brightness - STEP, state.brightness + STEP);
  return {
    state: { envelope, average, high, low, lastBeat: beat ? now : state.lastBeat, swell, hue, hueTarget, brightness },
    frame: { hue: Math.round(hue), saturation: 100, brightness: Math.round(clamp(brightness, FLOOR, 100)) },
    beat,
  };
}

/** One frame per light, each a little further round the wheel, so a room of lights is not one colour. */
export const spread = (frame: Frame, ids: string[]) =>
  ids.map((id, index) => ({ id, ...frame, hue: Math.round((frame.hue + index * 40) % 360) }));
