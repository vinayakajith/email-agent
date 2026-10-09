import { describe, expect, it } from "vitest";
import { renderable, Smoother } from "./smoother";

const run = (s: Smoother, ms: number, frame = 16) => {
  let text = "";
  for (let t = 0; t < ms; t += frame) text = s.step(frame);
  return text;
};

describe("Smoother", () => {
  it("reveals steadily rather than in the bursts it receives", () => {
    const s = new Smoother();
    s.push("The quick brown fox jumps over the lazy dog and keeps running far away. ".repeat(3));
    const after100 = run(s, 100).length;
    const after200 = run(s, 100).length;
    expect(after100).toBeGreaterThan(0);
    expect(after200).toBeGreaterThan(after100);
    expect(after200).toBeLessThan(s.fullText.length); // not dumped all at once
  });

  it("speeds up when behind", () => {
    const small = new Smoother();
    small.push("word ".repeat(10));
    const big = new Smoother();
    big.push("word ".repeat(400));
    expect(run(big, 200).length).toBeGreaterThan(run(small, 200).length);
  });

  it("only shows whole words while streaming", () => {
    const s = new Smoother();
    s.push("Hello wonderful world");
    for (let i = 0; i < 20; i++) {
      const text = s.step(16);
      expect(["", "Hello ", "Hello wonderful "]).toContain(text);
    }
  });

  it("catches up within ~150ms once finished", () => {
    const s = new Smoother();
    s.push("x ".repeat(2000));
    run(s, 50);
    s.finish();
    expect(run(s, 170)).toBe(s.fullText);
    expect(s.settled).toBe(true);
  });

  it("accepts the authoritative final text", () => {
    const s = new Smoother();
    s.push("Total is ₹1,41,441");
    s.finish("Total is ₹1,41,441.34 [#552].");
    expect(run(s, 200)).toBe("Total is ₹1,41,441.34 [#552].");
  });
});

describe("renderable", () => {
  it("hides half-written citations and closes open spans", () => {
    expect(renderable("Paid [#5")).toBe("Paid ");
    expect(renderable("Paid [")).toBe("Paid ");
    expect(renderable("**Total")).toBe("**Total**");
    expect(renderable("use `code")).toBe("use `code`");
    expect(renderable("done [#552].")).toBe("done [#552].");
  });
});
