import { describe, expect, it } from "vitest";
import { FLOOR, STEP, listening, nextFrame, spread, type Listening, type Mode } from "./music";

const RATE = 48_000;
const BINS = 512; // fftSize 1024

/** A spectrum with *loudness* (0 to 255) around *hz*. */
function tone(hz: number, loudness: number): Uint8Array {
  const spectrum = new Uint8Array(BINS);
  const bin = Math.round(hz / (RATE / 2 / BINS));
  for (let i = Math.max(0, bin - 2); i <= Math.min(BINS - 1, bin + 2); i++) spectrum[i] = loudness;
  return spectrum;
}

function play(spectra: Uint8Array[], mode: Mode, start: Listening = listening()) {
  let state = start;
  return spectra.map((spectrum, index) => {
    const out = nextFrame(state, spectrum, RATE, mode, 3, index * 125);
    state = out.state;
    return out;
  });
}

describe("lights following music", () => {
  it("stay at their floor in silence", () => {
    const frames = play(Array.from({ length: 8 }, () => new Uint8Array(BINS)), "pulse");
    expect(frames.every(({ frame, beat }) => frame.brightness === FLOOR && !beat)).toBe(true);
  });

  it("pulse on a kick drum after a quiet bar, and move the colour on", () => {
    const quiet = Array.from({ length: 16 }, () => tone(80, 10));
    const frames = play([...quiet, tone(80, 250), tone(80, 250)], "pulse");
    const kick = frames[16];
    expect(kick.beat).toBe(true);
    expect(kick.frame.brightness).toBeGreaterThan(frames[15].frame.brightness);
    expect((kick.frame.hue - frames[15].frame.hue + 360) % 360).toBeGreaterThan(30);
  });

  it("are red for a bass line and toward violet for cymbals, in Spectrum", () => {
    const low = play(Array.from({ length: 20 }, () => tone(110, 200)), "spectrum").at(-1)!;
    const high = play(Array.from({ length: 20 }, () => tone(3800, 200)), "spectrum").at(-1)!;
    expect(low.frame.hue).toBeLessThan(40);
    expect(high.frame.hue).toBeGreaterThan(240);
  });

  it("never strobe: brightness moves at most STEP a frame, whatever the music does", () => {
    const spectra = Array.from({ length: 200 }, (_, i) => tone(60 + ((i * 97) % 3000), i % 3 ? 255 : 0));
    for (const mode of ["pulse", "spectrum"] as const) {
      const frames = play(spectra, mode);
      frames.slice(1).forEach(({ frame }, i) => {
        expect(Math.abs(frame.brightness - frames[i].frame.brightness)).toBeLessThanOrEqual(STEP);
        expect(frame.brightness).toBeGreaterThanOrEqual(FLOOR);
      });
    }
  });

  it("give each light its own hue, so a room is not one colour", () => {
    expect(spread({ hue: 340, saturation: 100, brightness: 50 }, ["a", "b"])).toEqual([
      { id: "a", hue: 340, saturation: 100, brightness: 50 },
      { id: "b", hue: 20, saturation: 100, brightness: 50 },
    ]);
  });
});
