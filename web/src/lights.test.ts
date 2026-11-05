import { describe, expect, it } from "vitest";
import { addDeviceRequest, brandOf, foundRequest, inScope, kelvinToRgb, latestWins, scanSummary, swatch, type Light } from "./lights";

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

describe("adding a device Find devices saw", () => {
  it("tells north everything discovery learned, so it asks only what the user must do", () => {
    const found = {
      id: "hue@192.168.1.20", brand: "Philips Hue", key: "hue", kind: "bridge", name: "Hue Bridge - 1A2B3C",
      address: "192.168.1.20", how: "mDNS _hue._tcp", reach: "the bridge's local API; pressing its link button once gives north a key",
      cannot: "", details: { bridgeid: "001788fffe1a2b3c", modelid: "" }, added: false,
    };
    expect(foundRequest(found)).toBe(
      'Add this to my Devices page, so I can control it there and from chat: the Philips Hue bridge "Hue Bridge - 1A2B3C" ' +
        "at 192.168.1.20, found on the network by mDNS _hue._tcp (bridgeid=001788fffe1a2b3c). " +
        "North can reach it through the bridge's local API; pressing its link button once gives north a key.",
    );
  });
});

describe("what a Find devices scan means", () => {
  it("says why the list is short: Kasa is already here, others are not lights, and what cannot be seen", () => {
    const text = scanSummary({ found: [], others: 7 }, 4);
    expect(text).toContain("North found no other lights, plugs or switches it knows on this network.");
    expect(text).toContain("Your 4 Kasa devices are found by North itself and already on this page.");
    expect(text).toContain("7 other devices answered (TVs, speakers, routers and the like)");
    expect(text).toContain("maker's cloud or to a building's hub");
  });

  it("leaves out what does not apply", () => {
    const one = { id: "wiz@1", brand: "WiZ", key: "wiz", kind: "light", name: "WiZ light", address: "1", how: "WiZ", reach: "", cannot: "", details: {}, added: false };
    const text = scanSummary({ found: [one], others: 0 }, 0);
    expect(text).not.toContain("found no other");
    expect(text).not.toContain("Kasa");
    expect(text).not.toContain("answered");
  });
});
