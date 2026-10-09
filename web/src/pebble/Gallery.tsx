// Development-only: every mood side by side, for design review. Not included in production builds.
import { POSES, type Mood } from "./moods";
import { Pebble } from "./Pebble";

export function Gallery() {
  return (
    <main style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 28, padding: 40 }}>
      {(Object.keys(POSES) as Mood[]).map((m) => (
        <figure key={m} style={{ margin: 0, display: "grid", justifyItems: "center", gap: 6 }}>
          <Pebble mood={m} size={120} />
          <figcaption className="faint" style={{ fontSize: 13 }}>{m}</figcaption>
        </figure>
      ))}
    </main>
  );
}
