import { motion } from "motion/react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { get, type Session, type Status } from "../api";
import { moodOf } from "../pebble/mood";
import type { Mood } from "../pebble/moods";
import { Pebble } from "../pebble/Pebble";
import { Answer } from "./Answer";
import { Approval } from "./Approval";
import { HistoryDrawer } from "./HistoryDrawer";
import { ArrowUp, History, Stop } from "./icons";
import { Thinking, ThoughtFor } from "./Thinking";
import { useChat, type Turn } from "./useChat";
import "./chat.css";

const rise = { initial: { opacity: 0, y: 6 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.32, ease: [0.22, 1, 0.36, 1] as const } };

export function Chat({ session }: { session: Session }) {
  const chat = useChat(session);
  const [draft, setDraft] = useState("");
  const [drawer, setDrawer] = useState(false);
  const [status, setStatus] = useState<Status>(session.status);
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    const t = setInterval(() => get<Status>("/api/status").then(setStatus, () => undefined), 30000);
    return () => clearInterval(t);
  }, []);

  // Stay pinned to the bottom while streaming, unless the reader scrolled up.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [chat.messages, chat.turn?.text, chat.turn?.trail.length, chat.turn?.phase]);
  const onScroll = () => {
    const el = scroller.current!;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const send = (text: string) => {
    if (!text.trim() || chat.turn) return;
    pinned.current = true;
    setDraft("");
    void chat.send(text);
  };

  const pick = useCallback((id: string | null) => {
    setDrawer(false);
    void chat.load(id);
  }, [chat]);

  const empty = !chat.loading && chat.messages.length === 0 && !chat.turn;
  const mood = usePebbleMood(chat, draft);
  const name = session.profile?.name;

  return (
    <div className="chat">
      <header className="chat-header">
        <button className="icon-button" onClick={() => setDrawer(true)} aria-label="Conversations">
          <History />
        </button>
        <div className="chat-presence">
          <Pebble mood={mood} size={empty ? 120 : 84} talking={chat.turn?.phase === "answering"} />
          <p className={`status status-${status.status}`}>
            <span className="status-dot" aria-hidden="true" />
            {status.message}
          </p>
        </div>
        <span className="icon-button-spacer" />
      </header>

      <div className="chat-scroll" ref={scroller} onScroll={onScroll}>
        <div className="chat-column" role="log" aria-label="Conversation">
          {empty && (
            <motion.div className="empty" {...rise}>
              <h1>{greeting()}{name ? `, ${name}` : ""}.</h1>
              <p className="muted">Ask anything about your mail.</p>
              <div className="suggestions">
                {session.suggestions.map((s) => (
                  <button key={s} onClick={() => send(s)}>{s}</button>
                ))}
              </div>
            </motion.div>
          )}

          {chat.messages.map((m, i) => (
            <motion.div key={i} className={`message message-${m.role}`} {...(i >= chat.messages.length - 2 ? rise : {})}>
              {m.role === "user" && <p className="user-text">{m.text}</p>}
              {m.role === "assistant" && (
                <>
                  {m.seconds !== undefined ? (
                    <ThoughtFor seconds={m.seconds} thought={m.thought ?? ""} trail={m.steps} />
                  ) : (
                    m.steps.length > 0 && <p className="steps-done faint">{m.steps.join(" · ")}</p>
                  )}
                  <Answer text={m.text} sources={m.sources} />
                </>
              )}
              {m.role === "note" && <p className="note">{m.text}</p>}
              {m.role === "approval" && <Approval draft={m.draft} onDecide={(ok) => chat.decide(m.draft.draft_id, ok)} />}
            </motion.div>
          ))}

          {chat.turn && <LiveTurn turn={chat.turn} onDismiss={chat.dismiss} />}
          <p className="sr-only" aria-live="polite">
            {chat.messages.at(-1)?.role === "assistant" && !chat.turn ? "Answer ready." : ""}
          </p>
        </div>
      </div>

      <Composer value={draft} onChange={setDraft} onSend={() => send(draft)} onStop={chat.stop} busy={Boolean(chat.turn && (chat.turn.phase === "thinking" || chat.turn.phase === "answering"))} />
      <HistoryDrawer open={drawer} current={chat.conversationId} onClose={() => setDrawer(false)} onPick={pick} />
    </div>
  );
}

function LiveTurn({ turn, onDismiss }: { turn: Turn; onDismiss: () => void }) {
  return (
    <motion.div className="message message-assistant" {...rise}>
      <Thinking activity={turn.activity} trail={turn.trail} thought={turn.thought} startedAt={turn.startedAt}
        answeredAt={turn.answeredAt} />
      {turn.text && <Answer text={turn.text} sources={turn.sources} streaming />}
      {(turn.phase === "error" || turn.phase === "stopped") && (
        <p className="note">
          {turn.phase === "error" ? turn.error : "Stopped."}{" "}
          <button className="link-button" onClick={onDismiss}>Dismiss</button>
        </p>
      )}
    </motion.div>
  );
}

function Composer({ value, onChange, onSend, onStop, busy }: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  busy: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current!;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [value]);
  useEffect(() => ref.current?.focus(), []);
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      onSend();
    }
  };
  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea ref={ref} rows={1} value={value} placeholder="Ask about your mail…" aria-label="Message"
          onChange={(e) => onChange(e.target.value)} onKeyDown={onKey} />
        {busy ? (
          <button className="send" onClick={onStop} aria-label="Stop">
            <Stop />
          </button>
        ) : (
          <button className="send" onClick={onSend} disabled={!value.trim()} aria-label="Send">
            <ArrowUp />
          </button>
        )}
      </div>
    </div>
  );
}

function greeting(): string {
  const h = new Date().getHours();
  return h < 5 ? "Up late" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

/** Pebble's mood from the conversation state; time-based beats (settling, recovering) live here. */
function usePebbleMood(chat: ReturnType<typeof useChat>, draft: string): Mood {
  const { turn, messages } = chat;
  const [justFinished, setJustFinished] = useState(false);
  const [recovered, setRecovered] = useState(false);
  const wasAnswering = useRef(false);

  useEffect(() => {
    if (turn?.phase === "answering") wasAnswering.current = true;
    if (!turn && wasAnswering.current) {
      wasAnswering.current = false;
      setJustFinished(true);
      const t = setTimeout(() => setJustFinished(false), 1600);
      return () => clearTimeout(t);
    }
  }, [turn]);

  useEffect(() => {
    setRecovered(false);
    if (turn?.phase !== "error") return;
    const t = setTimeout(() => setRecovered(true), 2200); // a moment of disappointment, then it recovers
    return () => clearTimeout(t);
  }, [turn?.phase]);

  const lastQuestion = [...messages].reverse().find((m) => m.role === "user");
  return moodOf({
    turn: !turn || turn.phase === "stopped" ? "none" : turn.phase === "error" ? (recovered ? "none" : "error") : turn.phase,
    tool: turn?.tool ?? null,
    draft,
    awaitingApproval: messages.at(-1)?.role === "approval",
    justFinished,
    lastQuestion: lastQuestion?.role === "user" ? lastQuestion.text : "",
  });
}
