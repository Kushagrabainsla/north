import { describe, expect, it } from "vitest";
import { latestWins } from "./latest";

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

describe("a microphone's frames", () => {
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
