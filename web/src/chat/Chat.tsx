import type { Session } from "../api";
import { Pebble } from "../pebble/Pebble";

// Placeholder until the chat screen lands in the next stage.
export function Chat({ session }: { session: Session }) {
  return (
    <main className="center-screen">
      <Pebble mood="idle" />
      <p className="muted">{session.status.message}</p>
    </main>
  );
}
