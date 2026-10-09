import { describe, expect, it } from "vitest";
import { readSSE, SSEParser } from "./sse";

const sample =
  'event: conversation\ndata: {"id":"c1"}\n\n' +
  'event: text_delta\ndata: {"text":"Hello ₹12,450 — done"}\n\n' +
  ": keep-alive comment\n\n" +
  'event: final\ndata: {"text":"Hello"}\n\n';

describe("SSEParser", () => {
  it("parses whole events", () => {
    const events = new SSEParser().push(sample);
    expect(events.map((e) => e.event)).toEqual(["conversation", "text_delta", "final"]);
    expect(events[1].data).toEqual({ text: "Hello ₹12,450 — done" });
  });

  it("gives identical results for every possible split point", () => {
    for (let i = 1; i < sample.length; i++) {
      const p = new SSEParser();
      const events = [...p.push(sample.slice(0, i)), ...p.push(sample.slice(i))];
      expect(events.map((e) => e.event)).toEqual(["conversation", "text_delta", "final"]);
    }
  });

  it("handles CRLF line endings and multi-line data", () => {
    const events = new SSEParser().push("event: x\r\ndata: line1\r\ndata: line2\r\n\r\n");
    expect(events).toEqual([{ event: "x", data: "line1\nline2" }]);
  });
});

describe("readSSE", () => {
  it("decodes multi-byte characters split across network chunks", async () => {
    const bytes = new TextEncoder().encode(sample);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 0; i < bytes.length; i += 3) controller.enqueue(bytes.slice(i, i + 3));
        controller.close();
      },
    });
    const events = [];
    for await (const e of readSSE(stream)) events.push(e);
    expect(events[1].data).toEqual({ text: "Hello ₹12,450 — done" });
  });
});
