import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, get, post, streamChat, type Draft, type Message, type Session, type Source } from "../api";
import { Smoother } from "../stream/smoother";

export type Turn = {
  question: string;
  phase: "thinking" | "answering" | "error" | "stopped";
  tool: string | null; // the tool currently running, for Pebble
  activity: string | null; // plain-language label for the glimmer
  trail: string[];
  thought: string;
  startedAt: number;
  answeredAt: number | null;
  text: string; // what is visible right now (smoothed)
  sources: Source[];
  approvals: Draft[];
  error: string | null;
};

export type Conversation = { id: string; title: string; updated_at: string };

const newTurn = (question: string): Turn => ({
  question,
  phase: "thinking",
  tool: null,
  activity: null,
  trail: [],
  thought: "",
  startedAt: performance.now(),
  answeredAt: null,
  text: "",
  sources: [],
  approvals: [],
  error: null,
});

export function useChat(session: Session) {
  const [conversationId, setConversationId] = useState<string | null>(session.latest_conversation);
  const [messages, setMessages] = useState<Message[]>([]);
  const [turn, setTurn] = useState<Turn | null>(null);
  const [loading, setLoading] = useState(Boolean(session.latest_conversation));
  const smoother = useRef(new Smoother());
  const abort = useRef<AbortController | null>(null);
  const finalRef = useRef<{ text: string; sources: Source[] } | null>(null);
  const turnRef = useRef<Turn | null>(null);
  turnRef.current = turn;

  const load = useCallback(async (id: string | null) => {
    abort.current?.abort();
    setTurn(null);
    setConversationId(id);
    if (!id) {
      setMessages([]);
      return;
    }
    setLoading(true);
    try {
      setMessages((await get<{ messages: Message[] }>(`/api/conversations/${id}`)).messages);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (session.latest_conversation) void load(session.latest_conversation);
  }, [session.latest_conversation, load]);

  // Reveal loop: runs every frame while a turn is answering, then commits the finished answer.
  useEffect(() => {
    if (turn?.phase !== "answering") return;
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const text = smoother.current.step(now - last);
      last = now;
      setTurn((t) => (t && t.text !== text ? { ...t, text } : t));
      const final = finalRef.current;
      if (final && smoother.current.settled) {
        const t = turnRef.current!;
        setMessages((m) => [
          ...m,
          {
            role: "assistant",
            text: final.text,
            steps: t.trail,
            sources: final.sources,
            thought: t.thought,
            seconds: Math.max(1, Math.round(((t.answeredAt ?? performance.now()) - t.startedAt) / 1000)),
          },
          ...t.approvals.map((draft) => ({ role: "approval" as const, draft })),
        ]);
        finalRef.current = null;
        setTurn(null);
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [turn?.phase]);

  const send = useCallback(
    async (question: string) => {
      const q = question.trim();
      if (!q || turnRef.current) return;
      smoother.current.reset();
      finalRef.current = null;
      setMessages((m) => [...m, { role: "user", text: q }]);
      setTurn(newTurn(q));
      const controller = new AbortController();
      abort.current = controller;
      const update = (fn: (t: Turn) => Turn) => setTurn((t) => (t ? fn(t) : t));
      try {
        for await (const { event, data } of streamChat(q, conversationId, controller.signal)) {
          const d = data as Record<string, unknown>;
          switch (event) {
            case "conversation":
              setConversationId(d.id as string);
              break;
            case "thought_delta":
              update((t) => ({ ...t, thought: t.thought + (d.text as string) }));
              break;
            case "activity":
              update((t) => ({ ...t, tool: d.tool as string, activity: d.label as string }));
              break;
            case "step_done": // back to plain "Thinking" while the model reads the result
              update((t) => ({
                ...t,
                tool: null,
                activity: null,
                trail: t.trail.at(-1) === d.label ? t.trail : [...t.trail, d.label as string],
              }));
              break;
            case "preamble": // the text so far introduced tool calls; it belongs to the thinking
              update((t) => ({ ...t, phase: "thinking", thought: `${t.thought}\n\n${smoother.current.fullText}`.trim(), text: "" }));
              smoother.current.reset();
              break;
            case "text_delta":
              smoother.current.push(d.text as string);
              update((t) => (t.phase === "answering" ? t : { ...t, phase: "answering", tool: null, answeredAt: performance.now() }));
              break;
            case "approval":
              update((t) => ({ ...t, approvals: [...t.approvals, d.draft as Draft] }));
              break;
            case "final":
              finalRef.current = { text: d.text as string, sources: d.sources as Source[] };
              smoother.current.finish(d.text as string);
              update((t) => ({ ...t, phase: "answering", answeredAt: t.answeredAt ?? performance.now(), sources: d.sources as Source[] }));
              break;
            case "error":
              update((t) => ({ ...t, phase: "error", error: d.message as string }));
              break;
          }
        }
      } catch (e) {
        if (controller.signal.aborted) update((t) => ({ ...t, phase: "stopped" }));
        else update((t) => ({ ...t, phase: "error", error: e instanceof ApiError ? e.message : "Connection lost." }));
      }
    },
    [conversationId],
  );

  const stop = useCallback(() => abort.current?.abort(), []);

  /** Leave a stopped or failed turn behind (it was not saved by the server). */
  const dismiss = useCallback(() => {
    const t = turnRef.current;
    if (t && (t.phase === "stopped" || t.phase === "error")) {
      setMessages((m) => (m.at(-1)?.role === "user" ? m.slice(0, -1) : m));
      setTurn(null);
    }
  }, []);

  const decide = useCallback(async (draftId: number, approve: boolean) => {
    await post(`/api/drafts/${draftId}/${approve ? "approve" : "reject"}`);
    setMessages((m) =>
      m.map((msg) =>
        msg.role === "approval" && msg.draft.draft_id === draftId
          ? { role: "note", text: approve ? "Sent." : "Not sent.", draft_id: draftId }
          : msg,
      ),
    );
  }, []);

  return { conversationId, messages, turn, loading, send, stop, dismiss, decide, load };
}
