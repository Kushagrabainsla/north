import { describe, expect, it } from "vitest";
import { addDeviceRequest, brandOf, inScope, kelvinToRgb, latestWins, swatch, type Light } from "./lights";

const light = (over: Partial<Light>): Light => ({
  id: "a", name: "Desk lamp", host: "10.0.0.36", model: "KL130", on: true, kind: "light",
  can: { dim: true, color: true, white: [2500, 6500] }, mine: false, source: "kasa", ...over,
});

describe("which lights a choice covers", () => {
  it("is only yours for My lights, and every one, a friend's too, for All lights", () => {
    const lights = [light({ id: "mine", mine: true }), light({ id: "friend's" })];

    expect(inScope(lights, "mine").map((l) => l.id)).toEqual(["mine"]);
    expect(inScope(lights, "all").map((l) => l.id)).toEqual(["mine", "friend's"]);
  });
});

describe("the colour a card shows", () => {
  it("follows what the light reports: off, a colour, or a white", () => {
    expect(swatch(light({ on: false, hue: 120 }))).toBe("transparent");
    expect(swatch(light({ hue: 240, saturation: 80 }))).toBe("hsl(240 80% 55%)");
    expect(swatch(light({ kelvin: 6500 }))).toBe("rgb(255 254 250)");
  });

  it("is warmer the lower the temperature", () => {
    const [, warmGreen, warmBlue] = kelvinToRgb(2700);
    const [, coolGreen, coolBlue] = kelvinToRgb(6500);
    expect(warmBlue).toBeLessThan(coolBlue);
    expect(warmGreen).toBeLessThan(coolGreen);
  });
});

describe("a slider dragged across its range", () => {
  it("sends one request at a time, then only the newest value", async () => {
    const sent: number[] = [];
    const settled: number[] = [];
    let release = () => {};
    const send = (value: number) => {
      sent.push(value);
      return new Promise<number>((resolve) => { release = () => resolve(value); });
    };
    const push = latestWins(send, (result) => settled.push(result), () => {});

    for (const value of [10, 20, 30, 40]) push(value);
    expect(sent).toEqual([10]);  // one in flight; 20 and 30 were overtaken
    release();
    await new Promise((resolve) => setTimeout(resolve, 0));
    release();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(sent).toEqual([10, 40]);
    expect(settled).toEqual([10, 40]);
  });
});

describe("music sync's frames", () => {
  it("keep a waiting beat: the frames fading after it do not overtake it", async () => {
    const sent: string[] = [];
    let release = () => {};
    const send = (value: string) => {
      sent.push(value);
      return new Promise<void>((resolve) => { release = resolve; });
    };
    const push = latestWins(send, () => {}, () => {}, (value) => value.startsWith("beat"));

    for (const value of ["fade 1", "beat", "fade 2", "fade 3"]) push(value);
    release();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(sent).toEqual(["fade 1", "beat"]);
  });
});

describe("a device of any brand", () => {
  it("shows a Kasa bulb's model and address, and another brand by name", () => {
    expect(brandOf(light({}))).toBe("KL130 · 10.0.0.36");
    expect(brandOf(light({ id: "hue_lights/3", source: "hue_lights", host: "", model: "" }))).toBe("hue lights");
  });

  it("asks north to add a device in the user's own words, naming where it should show", () => {
    expect(addDeviceRequest("  A Philips Hue bridge at 192.168.1.20 ")).toBe(
      "Add this to my Devices page, so I can control it there and from chat: A Philips Hue bridge at 192.168.1.20",
    );
  });
});
