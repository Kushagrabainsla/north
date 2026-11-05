// What the lights should do for each moment of the music, from the levels listen.ts measures
// every 10 ms. The sound never leaves the browser: only the colours this works out go to north.

import type { Levels } from "./listen";

/** Beat: the lights jump with each beat and change colour on it. Mood: colours drift with the music's feel. */
export type Mode = "beat" | "mood";

export interface Frame {
  hue: number;
  saturation: number;
  brightness: number;
}

interface Slice {
  low: number;
  punch: number;
  mid: number;
  all: number;
}

export interface Listening {
  previous: Slice[]; // the last few slices, which a beat rises above
  strongest: number; // the biggest rise lately (dB), fading: a beat is judged against the song's own hits
  loudHigh: number; // the loudest and quietest the music has been lately, in dB
  loudLow: number;
  loud: number; // how loud now, between those two, 0 to 1, smoothed
  sinceBeat: number; // ms since the last beat
  flash: number; // what is left of the last beat's flash, 1 to 0
  hue: number;
  hueTarget: number; // where the colour is gliding to
  tone: number; // how bright the sound is (treble against bass), 0 to 1, smoothed
  clock: number; // ms listened, for Mood's slow drift
}

// Brightness never drops below FLOOR, and a beat lifts the lights by up to DEPTH above where the
// music's loudness has them. Two beats are at least MIN_BEAT_GAP_MS apart: 250 beats a minute.
export const FLOOR = 10;
export const MIN_BEAT_GAP_MS = 240;
// A beat is the last 20 ms rising over the 40 ms before them. Over a single 10 ms slice a voice
// or a synth pad jumps by several dB at random, which read as beats.
const NOW = 2;
const BEFORE = 4;
const SILENCE_DB = -60;
// A kick rises 10-15 dB over a bass line. A bigger rise is music starting, or a knock on the
// microphone: judged against one, every real beat for seconds after looked small.
const LARGEST_RISE_DB = 24;
const COLOUR_PER_BEAT = 47; // degrees: not a divisor of 360, so the colours do not repeat every few beats

export const listening = (): Listening => ({
  previous: [],
  strongest: 6,
  loudHigh: -100,
  loudLow: 0,
  loud: 0,
  sinceBeat: Infinity,
  flash: 0,
  hue: 30,
  hueTarget: 30,
  tone: 0.5,
  clock: 0,
});

const clamp = (value: number, low = 0, high = 1) => Math.max(low, Math.min(high, value));
/** The share of the way something gliding with time constant *tauMs* covers in *ms*. */
const glide = (ms: number, tauMs: number) => 1 - Math.exp(-ms / tauMs);
const mean = (values: number[]) => values.reduce((sum, value) => sum + value, 0) / (values.length || 1);

/** The hue *from* moves toward *to* by *share*, the short way round the colour wheel. */
function towards(from: number, to: number, share: number): number {
  return (from + (((to - from + 540) % 360) - 180) * share + 360) % 360;
}

/** The share of the song's biggest recent hit a rise must reach to count, from *strength* (0 gentle to 1 strong). */
const beatBar = (strength: number) => 0.6 - 0.3 * clamp(strength);

const power = (db: number) => 10 ** (db / 10);
/** How many dB the newest slices of one band rise over the ones before them. */
function riseIn(slices: Slice[], band: keyof Slice): number {
  const now = mean(slices.slice(-NOW).map((slice) => power(slice[band])));
  const before = mean(slices.slice(-NOW - BEFORE, -NOW).map((slice) => power(slice[band])));
  return clamp(10 * Math.log10(now / before), 0, LARGEST_RISE_DB);
}

/**
 * The next frame for the lights from one 10 ms slice of sound, and what to remember for the next.
 *
 * A beat is a sudden rise in the bass and kick bands (and, less, in the middle: a snare, a clap,
 * a dhol's slap) that comes close to the biggest such rise of the last few seconds - measured
 * against the song itself, so a loud mastered track and a quiet one, a ballad and bhangra, are
 * each judged by their own hits.
 */
export function hear(
  state: Listening, levels: Levels, ms: number, mode: Mode, strength: number,
): { state: Listening; frame: Frame; beat: boolean } {
  const slices = [...state.previous, { low: levels.low, punch: levels.punch, mid: levels.mid, all: levels.all }].slice(-(NOW + BEFORE));
  const rise = slices.length < NOW + BEFORE
    ? 0
    : riseIn(slices, "low") + riseIn(slices, "punch") + 0.4 * riseIn(slices, "mid");
  const quiet = levels.all < SILENCE_DB;
  // Music starting after quiet is an entrance, not a hit: the song's hits are measured from there.
  const entering = slices.slice(0, BEFORE).some((slice) => slice.all < SILENCE_DB);
  const strongest = entering ? listening().strongest : Math.max(rise, state.strongest * Math.exp(-ms / 3000));
  const beat = !quiet && !entering && rise >= Math.max(2, beatBar(strength) * strongest) && state.sinceBeat + ms >= MIN_BEAT_GAP_MS;

  // Loudness against the song's own range: the loudest and quietest drift 2 dB a second toward now.
  const drift = (2 * ms) / 1000;
  const loudHigh = levels.all > state.loudHigh ? levels.all : state.loudHigh - drift;
  const loudLow = levels.all < state.loudLow ? levels.all : state.loudLow + drift;
  const position = quiet ? 0 : clamp((levels.all - loudLow) / Math.max(loudHigh - loudLow, 6));
  const loud = state.loud + (position - state.loud) * glide(ms, 250);

  const flash = beat ? 1 : state.flash * Math.exp(-ms / (mode === "beat" ? 170 : 400));
  // Songs run from about 45 dB less treble than bass (deep) to 10 dB less (bright).
  const tone = state.tone + (clamp((levels.high - levels.low + 45) / 35) - state.tone) * glide(ms, 1200);

  let hueTarget = state.hueTarget;
  let hue: number;
  let brightness: number;
  if (mode === "beat") {
    if (beat) hueTarget = (hueTarget + COLOUR_PER_BEAT) % 360;
    hue = towards(state.hue, hueTarget, glide(ms, 60));
    brightness = FLOOR + 30 * loud + (25 + 60 * clamp(strength)) * flash;
  } else {
    // Deep songs toward red, bright ones toward blue, drifting 30° either way every 16 s.
    hueTarget = (tone * 240 + 30 * Math.sin((2 * Math.PI * state.clock) / 16_000) + 360) % 360;
    hue = towards(state.hue, hueTarget, glide(ms, 800));
    brightness = FLOOR + 70 * loud + (6 + 16 * clamp(strength)) * flash;
  }

  return {
    state: {
      previous: slices,
      strongest,
      loudHigh,
      loudLow,
      loud,
      sinceBeat: beat ? 0 : state.sinceBeat + ms,
      flash,
      hue,
      hueTarget,
      tone,
      clock: state.clock + ms,
    },
    frame: { hue: Math.round(hue) % 360, saturation: 100, brightness: Math.round(clamp(brightness, FLOOR, 100)) },
    beat,
  };
}

/** One frame per light, each a little further round the wheel, so a room of lights is not one colour. */
export const spread = (frame: Frame, ids: string[]) =>
  ids.map((id, index) => ({ id, ...frame, hue: Math.round((frame.hue + index * 40) % 360) }));
