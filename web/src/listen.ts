// How loud the music is in its bass, middle and treble, every 10 ms. Runs on the audio thread
// (listen.worklet.ts), so it hears every sample however busy or hidden the page is.

/** Loudness in decibels (0 is full scale) of one 10 ms slice of sound. */
export interface Levels {
  low: number; // below 150 Hz: kick drums, the dhol's bass, bass lines
  punch: number; // around 120 Hz: where a kick or a dhol hit lands, above a bass line's own notes
  mid: number; // around 1 kHz: snares, claps, the dhol's slap, voices
  high: number; // above 6 kHz: cymbals and hi-hats
  all: number; // everything
}

/** One second-order filter section (the RBJ cookbook's), run sample by sample. */
class Biquad {
  private readonly b: [number, number, number];
  private readonly a: [number, number];
  private x1 = 0;
  private x2 = 0;
  private y1 = 0;
  private y2 = 0;

  constructor(b: [number, number, number], a: [number, number]) {
    this.b = b;
    this.a = a;
  }

  static lowpass(rate: number, hz: number) {
    const [cos, alpha] = Biquad.angle(rate, hz, Math.SQRT1_2);
    const n = 1 + alpha;
    return new Biquad([(1 - cos) / 2 / n, (1 - cos) / n, (1 - cos) / 2 / n], [(-2 * cos) / n, (1 - alpha) / n]);
  }

  static highpass(rate: number, hz: number) {
    const [cos, alpha] = Biquad.angle(rate, hz, Math.SQRT1_2);
    const n = 1 + alpha;
    return new Biquad([(1 + cos) / 2 / n, -(1 + cos) / n, (1 + cos) / 2 / n], [(-2 * cos) / n, (1 - alpha) / n]);
  }

  static bandpass(rate: number, hz: number, q: number) {
    const [cos, alpha] = Biquad.angle(rate, hz, q);
    const n = 1 + alpha;
    return new Biquad([alpha / n, 0, -alpha / n], [(-2 * cos) / n, (1 - alpha) / n]);
  }

  private static angle(rate: number, hz: number, q: number): [number, number] {
    const w = (2 * Math.PI * hz) / rate;
    return [Math.cos(w), Math.sin(w) / (2 * q)];
  }

  next(x: number): number {
    const y = this.b[0] * x + this.b[1] * this.x1 + this.b[2] * this.x2 - this.a[0] * this.y1 - this.a[1] * this.y2;
    this.x2 = this.x1;
    this.x1 = x;
    this.y2 = this.y1;
    this.y1 = y;
    return y;
  }
}

const decibels = (meanSquare: number) => 10 * Math.log10(meanSquare + 1e-12);

/** How long one slice is, in ms, at *sampleRate*: as close to 10 ms as whole samples allow. */
export const sliceMs = (sampleRate: number) => (Math.round(sampleRate / 100) / sampleRate) * 1000;

/** Splits sound into bands and reports each band's loudness once per 10 ms. */
export class BandMeter {
  private readonly hop: number;
  // Two sections each: a single one lets the voices and synths through to the bass band.
  private readonly low: Biquad[];
  private readonly punch: Biquad[];
  private readonly mid: Biquad[];
  private readonly high: Biquad[];
  private sums = [0, 0, 0, 0, 0];
  private count = 0;

  constructor(sampleRate: number) {
    this.hop = Math.round(sampleRate / 100);
    this.low = [Biquad.lowpass(sampleRate, 150), Biquad.lowpass(sampleRate, 150)];
    this.punch = [Biquad.bandpass(sampleRate, 120, 1.2), Biquad.bandpass(sampleRate, 120, 1.2)];
    this.mid = [Biquad.bandpass(sampleRate, 1000, 0.8), Biquad.bandpass(sampleRate, 1000, 0.8)];
    this.high = [Biquad.highpass(sampleRate, 6000), Biquad.highpass(sampleRate, 6000)];
  }

  /** Feed samples; returns the levels of every 10 ms slice they completed. */
  push(samples: Float32Array): Levels[] {
    const out: Levels[] = [];
    for (const x of samples) {
      const bands = [this.low, this.punch, this.mid, this.high].map(([first, second]) => second.next(first.next(x)));
      bands.forEach((y, i) => (this.sums[i] += y * y));
      this.sums[4] += x * x;
      if (++this.count === this.hop) {
        const [low, punch, mid, high, all] = this.sums.map((sum) => decibels(sum / this.hop));
        out.push({ low, punch, mid, high, all });
        this.sums = [0, 0, 0, 0, 0];
        this.count = 0;
      }
    }
    return out;
  }
}
