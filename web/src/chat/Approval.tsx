import { useState } from "react";
import type { Draft } from "../api";

export function Approval({ draft, onDecide }: { draft: Draft; onDecide: (approve: boolean) => Promise<void> }) {
  const [busy, setBusy] = useState<"send" | "discard" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const decide = async (approve: boolean) => {
    setBusy(approve ? "send" : "discard");
    setError(null);
    try {
      await onDecide(approve);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't do that.");
      setBusy(null);
    }
  };
  return (
    <section className="approval" aria-label="Email waiting for your approval">
      <p className="approval-kicker">Ready to send. Nothing goes out until you say so.</p>
      <dl className="approval-head">
        <dt>To</dt>
        <dd>{draft.to.join(", ")}</dd>
        {draft.cc.length > 0 && (
          <>
            <dt>Cc</dt>
            <dd>{draft.cc.join(", ")}</dd>
          </>
        )}
        <dt>Subject</dt>
        <dd>{draft.subject}</dd>
      </dl>
      <p className="approval-body">{draft.body}</p>
      {error && <p className="form-error">{error}</p>}
      <div className="approval-actions">
        <button className="button" onClick={() => decide(true)} disabled={busy !== null}>
          {busy === "send" ? <span className="glimmer">Sending</span> : "Send"}
        </button>
        <button className="button quiet" onClick={() => decide(false)} disabled={busy !== null}>
          Discard
        </button>
      </div>
    </section>
  );
}
