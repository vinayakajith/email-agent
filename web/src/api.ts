import { readSSE, type SSEEvent } from "./stream/sse";

const WRITE_HEADERS = { "Content-Type": "application/json", "X-Email-Agent": "1" };

export class ApiError extends Error {}

async function handle<T>(res: Response): Promise<T> {
  if (res.ok) return res.json() as Promise<T>;
  let detail = `Request failed (${res.status})`;
  try {
    const body = await res.json();
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    /* keep the generic message */
  }
  throw new ApiError(detail);
}

export const get = <T>(path: string) => fetch(path).then((r) => handle<T>(r));

export const post = <T>(path: string, body?: unknown) =>
  fetch(path, { method: "POST", headers: WRITE_HEADERS, body: JSON.stringify(body ?? {}) }).then((r) =>
    handle<T>(r),
  );

export type StepName = "api_keys" | "gmail" | "profile" | "first_ingest";

export type Session = {
  onboarding: {
    steps: Record<StepName, boolean>;
    next: StepName | null;
    account: string | null;
    ingest_running: boolean;
  };
  profile: { name: string; timezone: string; about: string } | null;
  latest_conversation: string | null;
  suggestions: string[];
  status: Status;
};

export type Status = { status: "ok" | "attention" | "down"; message: string };

export type Job = {
  id: number;
  mode: string;
  status: "queued" | "running" | "done" | "failed";
  stage: string | null;
  stage_done: number;
  stage_total: number;
  fetched: number;
  enriched: number;
  embedded: number;
  error: string | null;
};

export type Source = { id: number; subject: string; from: string; date: string | null };
export type Draft = { draft_id: number; to: string[]; cc: string[]; subject: string; body: string };

export type Message =
  | { role: "user"; text: string }
  | { role: "assistant"; text: string; steps: string[]; sources: Source[] }
  | { role: "note"; text: string; draft_id: number }
  | { role: "approval"; draft: Draft };

export async function* streamChat(
  message: string,
  conversationId: string | null,
  signal: AbortSignal,
): AsyncGenerator<SSEEvent> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: WRITE_HEADERS,
    body: JSON.stringify({ message, conversation_id: conversationId }),
    signal,
  });
  if (!res.ok || !res.body) throw new ApiError((await res.text()) || "The assistant is unavailable");
  yield* readSSE(res.body);
}
