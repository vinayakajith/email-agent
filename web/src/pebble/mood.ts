import type { Mood } from "./moods";

export type Intent = "memory" | "write" | "money" | "urgent" | "warm" | null;

const INTENTS: [Exclude<Intent, null>, RegExp][] = [
  ["memory", /\b(remember|forget|keep in mind|note that|don't forget)\b/i],
  ["write", /\b(reply|respond|write|draft|compose|forward|send (an? )?(email|mail|note|message))\b/i],
  ["money", /\b(bills?|total|spen[dt]|paid|pay(ment)?s?|amounts?|costs?|price|invoices?|subscriptions?|charged?|refunds?|cards?|owe|due)\b|[₹$€£]/i],
  ["urgent", /\b(urgent|asap|important|attention|priority|deadline|overdue|immediately)\b/i],
  ["warm", /^\s*(hi|hey|hello|yo|thanks|thank you|thx|ty|good (morning|afternoon|evening|night))\b|\b(thanks|thank you|appreciate it)\b/i],
];

/** What the message being typed is about; checked locally on every keystroke, no model involved. */
export function intentOf(text: string): Intent {
  for (const [intent, re] of INTENTS) if (re.test(text)) return intent;
  return null;
}

const TOOL_MOODS: Record<string, Mood> = {
  search_emails: "searching",
  filter_emails: "searching",
  get_email: "reading",
  get_thread: "reading",
  aggregate_spend: "counting",
  list_subscriptions: "counting",
  get_user_context: "recalling",
  draft_email: "writing",
  send_email: "waiting",
  save_memory: "noting",
  delete_memory: "noting",
};

const INTENT_MOODS: Record<Exclude<Intent, null>, Mood> = {
  memory: "noting",
  write: "writing",
  money: "focused",
  urgent: "alert",
  warm: "warm",
};

export type MoodInput = {
  turn: "none" | "thinking" | "answering" | "error";
  tool: string | null; // tool currently running
  draft: string; // what is in the composer
  awaitingApproval: boolean;
  justFinished: boolean; // an answer completed a moment ago
  lastQuestion: string;
};

/** Agent state > your typing > idle. */
export function moodOf(s: MoodInput): Mood {
  if (s.turn === "error") return "sad";
  if (s.turn === "thinking") return (s.tool && TOOL_MOODS[s.tool]) || "thinking";
  if (s.turn === "answering") return "answering";
  if (s.awaitingApproval) return "waiting";
  if (s.draft.trim()) {
    const intent = intentOf(s.draft);
    return intent ? INTENT_MOODS[intent] : "listening";
  }
  if (s.justFinished) return intentOf(s.lastQuestion) === "warm" ? "warm" : "happy";
  return "idle";
}
