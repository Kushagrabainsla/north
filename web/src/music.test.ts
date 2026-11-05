import { describe, expect, it } from "vitest";
import { BandMeter, sliceMs } from "./listen";
import { FLOOR, hear, listening, spread, type Mode } from "./music";

const RATE = 48_000;

/** A seeded noise source, so a failing test fails the same way every time. */
function noise(seed = 7) {
  let value = seed;
  return () => {
    value = (value * 16807) % 2147483647;
    return value / 2147483647 - 0.5;
  };
}

interface Song {
  bpm: number;
  seconds?: number;
  kick?: number; // 0 for none
  slap?: number; // a dhol's slap on every off-beat; 0 for none
  bassLine?: number; // a bass line that never stops
  voices?: number; // a mid-band wash: vocals and synths
  gain?: number; // how loud the microphone hears it all
}

/** A song as a room microphone hears it: mastered loud, a bass line under the kicks, voices over them. */
function song({ bpm, seconds = 12, kick = 0.9, slap = 0, bassLine = 0.5, voices = 0.3, gain = 1 }: Song) {
  const samples = new Float32Array(RATE * seconds);
  const random = noise();
  const beat = 60 / bpm;
  const beats: number[] = [];
  for (let t = 0; t < seconds; t += beat) beats.push(t);
  let voice = 0;
  for (let i = 0; i < samples.length; i++) {
    const t = i / RATE;
    const sinceBeat = t % beat;
    const sinceOff = (t + beat / 2) % beat;
    const notes = [41.2, 41.2, 55, 49];
    const note = notes[Math.floor(t / (beat * 2)) % notes.length];
    let x = bassLine * Math.sin(2 * Math.PI * note * t);
    if (kick) x += kick * Math.sin(2 * Math.PI * (50 * sinceBeat + 2.4 * (1 - Math.exp(-sinceBeat / 0.03)))) * Math.exp(-sinceBeat / 0.09);
    if (slap) x += slap * random() * 2 * Math.exp(-sinceOff / 0.03);
    voice = 0.9 * voice + random(); // brown-ish noise, then a crude high-pass: a mid-band wash
    x += voices * (random() - voice * 0.05);
    samples[i] = gain * Math.tanh(1.6 * x);
  }
  return { samples, beats };
}

function listen(samples: Float32Array, mode: Mode = "beat", strength = 0.6) {
  const meter = new BandMeter(RATE);
  const ms = sliceMs(RATE);
  let state = listening();
  const heard: { t: number; beat: boolean; brightness: number; hue: number }[] = [];
  for (let i = 0; i < samples.length; i += 128) {
    for (const levels of meter.push(samples.subarray(i, i + 128))) {
      const out = hear(state, levels, ms, mode, strength);
      state = out.state;
      heard.push({ t: (heard.length + 1) * ms / 1000, beat: out.beat, brightness: out.frame.brightness, hue: out.frame.hue });
    }
  }
  return heard.filter((slice) => slice.t >= 2); // it settles on the song in its first seconds
}

const caught = (beats: number[], heard: ReturnType<typeof listen>) => {
  const found = heard.filter((slice) => slice.beat).map((slice) => slice.t);
  const real = beats.filter((t) => t >= heard[0].t + 0.1 && t <= heard.at(-1)!.t - 0.1);
  return {
    share: real.filter((t) => found.some((f) => Math.abs(f - t) <= 0.06)).length / real.length,
    stray: found.filter((f) => !beats.some((t) => Math.abs(f - t) <= 0.06)).length,
  };
};

describe("lights following music", () => {
  it("catch the beat of a song whose bass line never stops between the kicks", () => {
    // The old detector wanted the bass 35% above its average, on a decibel scale: real songs never
    // get there, so the lights wandered with the loudness and ignored the beat.
    const { samples, beats } = song({ bpm: 124 });
    const { share, stray } = caught(beats, listen(samples));
    expect(share).toBeGreaterThan(0.9);
    expect(stray).toBeLessThanOrEqual(1);
  });

  it("judge a quiet, far-away microphone by the song's own beats, not a fixed loudness", () => {
    const { samples, beats } = song({ bpm: 100, gain: 0.02 });
    expect(caught(beats, listen(samples)).share).toBeGreaterThan(0.9);
  });

  it("catch the beat as soon as a song starts after silence, as when you press play", () => {
    // The jump from silence was read as the song's biggest hit, and for seconds after it every
    // real beat looked too small to count.
    const { samples, beats } = song({ bpm: 124, seconds: 10 });
    const pressedPlay = new Float32Array(RATE * 2 + samples.length);
    pressedPlay.set(samples, RATE * 2);
    const heard = listen(pressedPlay).filter((slice) => slice.t >= 3);
    expect(caught(beats.map((t) => t + 2), heard).share).toBeGreaterThan(0.9);
  });

  it("stay at their floor in silence, and hear no beats in it", () => {
    const heard = listen(new Float32Array(RATE * 5));
    expect(heard.every((slice) => slice.brightness === FLOOR && !slice.beat)).toBe(true);
  });

  it("react visibly harder on Strong than on Gentle", () => {
    const { samples } = song({ bpm: 100, slap: 0.8 });
    const peak = (strength: number) => Math.max(...listen(samples, "beat", strength).map((slice) => slice.brightness));
    const beatsHeard = (strength: number) => listen(samples, "beat", strength).filter((slice) => slice.beat).length;
    expect(peak(1) - peak(0)).toBeGreaterThanOrEqual(30);
    expect(beatsHeard(1)).toBeGreaterThan(beatsHeard(0)); // Strong also follows the dhol's off-beat slaps
  });

  it("change colour on the beat and hold it between beats, in Beat", () => {
    const all = listen(song({ bpm: 124 }).samples);
    const heard = all.slice(all.findIndex((slice) => slice.beat)); // from the first beat it heard
    const turns = heard.slice(1).map((slice, i) => ({ ...slice, turn: Math.abs(((slice.hue - heard[i].hue + 540) % 360) - 180) }));
    const sinceBeat = (t: number) => t - Math.max(...heard.filter((s) => s.beat && s.t <= t).map((s) => s.t));
    expect(turns.filter((slice) => slice.turn > 0).every((slice) => sinceBeat(slice.t) <= 0.35)).toBe(true);
  });

  it("never strobe: between beats the light only fades, never jumps back up", () => {
    for (const mode of ["beat", "mood"] as const) {
      const heard = listen(song({ bpm: 124, slap: 0.4 }).samples, mode);
      heard.slice(1).forEach((slice, i) => {
        if (!slice.beat) expect(slice.brightness - heard[i].brightness).toBeLessThanOrEqual(2);
        expect(slice.brightness).toBeGreaterThanOrEqual(FLOOR);
      });
    }
  });

  it("are warm for a deep, bass-heavy song and cool for a bright one, in Mood", () => {
    const fromRed = (hue: number) => Math.min(hue, 360 - hue); // round the wheel: 340 is a red too
    const usual = (heard: ReturnType<typeof listen>) => heard.reduce((sum, slice) => sum + fromRed(slice.hue), 0) / heard.length;
    const deep = listen(song({ bpm: 90, voices: 0, bassLine: 0.8 }).samples, "mood");
    const bright = song({ bpm: 90, kick: 0.2, bassLine: 0.05, voices: 0.3 });
    const random = noise(3); // cymbals all the way through: a treble-heavy wash
    for (let i = 0; i < bright.samples.length; i++) bright.samples[i] += 0.6 * (random() - random());
    const cool = listen(bright.samples, "mood");
    expect(usual(deep)).toBeLessThan(60);
    expect(cool.reduce((sum, slice) => sum + slice.hue, 0) / cool.length).toBeGreaterThan(180); // blue is 240
  });

  it("give each light its own hue, so a room is not one colour", () => {
    expect(spread({ hue: 340, saturation: 100, brightness: 50 }, ["a", "b"])).toEqual([
      { id: "a", hue: 340, saturation: 100, brightness: 50 },
      { id: "b", hue: 20, saturation: 100, brightness: 50 },
    ]);
  });
});
