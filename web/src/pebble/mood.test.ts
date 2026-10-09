import { describe, expect, it } from "vitest";
import { intentOf, moodOf, type MoodInput } from "./mood";

const base: MoodInput = { turn: "none", tool: null, draft: "", awaitingApproval: false, justFinished: false, lastQuestion: "" };

describe("intentOf", () => {
  it.each([
    ["total of my credit card bills this month", "money"],
    ["how much did I spend on ₹ stuff", "money"],
    ["anything urgent today?", "urgent"],
    ["thanks!", "warm"],
    ["hi there", "warm"],
    ["reply to Priya that Friday works", "write"],
    ["remember that I prefer short answers", "memory"],
    ["please forget my old address", "memory"],
    ["who is priya", null],
    ["this is important, what's my bill due?", "money"], // money outranks urgent
  ])("%s -> %s", (text, intent) => expect(intentOf(text)).toBe(intent));

  it("does not match words inside other words", () => {
    expect(intentOf("the payload was large")).toBe(null);
    expect(intentOf("whither")).toBe(null);
  });
});

describe("moodOf", () => {
  it("prefers what the agent is doing over what is being typed", () => {
    expect(moodOf({ ...base, turn: "thinking", tool: "search_emails", draft: "thanks" })).toBe("searching");
    expect(moodOf({ ...base, turn: "thinking", tool: "aggregate_spend" })).toBe("counting");
    expect(moodOf({ ...base, turn: "thinking", tool: "get_email" })).toBe("reading");
    expect(moodOf({ ...base, turn: "thinking", tool: "get_user_context" })).toBe("recalling");
    expect(moodOf({ ...base, turn: "thinking", tool: "some_new_tool" })).toBe("thinking");
    expect(moodOf({ ...base, turn: "thinking" })).toBe("thinking");
    expect(moodOf({ ...base, turn: "answering" })).toBe("answering");
  });

  it("errors and approvals come first", () => {
    expect(moodOf({ ...base, turn: "error", tool: "search_emails" })).toBe("sad");
    expect(moodOf({ ...base, awaitingApproval: true, draft: "hi" })).toBe("waiting");
  });

  it("reacts to typing intent", () => {
    expect(moodOf({ ...base, draft: "what's my card bill" })).toBe("focused");
    expect(moodOf({ ...base, draft: "urgent stuff?" })).toBe("alert");
    expect(moodOf({ ...base, draft: "draft a reply" })).toBe("writing");
    expect(moodOf({ ...base, draft: "who wrote" })).toBe("listening");
  });

  it("settles after an answer, warmly after thanks", () => {
    expect(moodOf({ ...base, justFinished: true, lastQuestion: "bills?" })).toBe("happy");
    expect(moodOf({ ...base, justFinished: true, lastQuestion: "thank you!" })).toBe("warm");
    expect(moodOf(base)).toBe("idle");
  });
});
