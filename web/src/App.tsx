import { useCallback, useEffect, useState } from "react";
import { get, type Session } from "./api";
import { Chat } from "./chat/Chat";
import { Onboarding } from "./onboarding/Onboarding";
import { Pebble } from "./pebble/Pebble";

export function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(() => {
    get<Session>("/api/session").then(setSession, () => setFailed(true));
  }, []);
  useEffect(load, [load]);

  if (failed) {
    return (
      <main className="center-screen">
        <Pebble mood="sad" />
        <p className="muted">Can't reach the assistant. Is <code>email-agent app</code> running?</p>
      </main>
    );
  }
  if (!session) return <main className="center-screen" />;
  if (session.onboarding.next) return <Onboarding session={session} onDone={load} />;
  return <Chat session={session} />;
}
