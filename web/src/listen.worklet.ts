// The audio-thread half of music sync: measures each 10 ms of sound and posts its levels to
// the page. The audio thread keeps time by the sound itself, not by the page's timers.

import { BandMeter } from "./listen";

// What an AudioWorkletGlobalScope provides; TypeScript's DOM library does not describe it.
declare const sampleRate: number;
declare function registerProcessor(name: string, processor: unknown): void;
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
}

class Listener extends AudioWorkletProcessor {
  private readonly meter = new BandMeter(sampleRate);

  process(inputs: Float32Array[][]): boolean {
    const channel = inputs[0]?.[0];
    if (channel) for (const levels of this.meter.push(channel)) this.port.postMessage(levels);
    return true;
  }
}

registerProcessor("north-listen", Listener);
