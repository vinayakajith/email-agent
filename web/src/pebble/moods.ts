/**
 * Pebble's moods as target poses. The renderer springs toward these; procedural motion
 * (breathing, blinking, scanning) is layered on top per mood in Pebble.tsx.
 */
export type Mood =
  | "idle"
  | "listening" // you're typing
  | "warm" // greeting, thanks
  | "focused" // money / numbers
  | "alert" // urgent, important
  | "writing" // composing a reply
  | "noting" // remember / forget
  | "thinking"
  | "searching"
  | "reading"
  | "counting"
  | "recalling"
  | "answering"
  | "waiting" // waiting for your approval
  | "happy" // done well
  | "sad"; // something failed

export type Pose = {
  squash: number; // + flattens, - stretches
  tilt: number; // degrees
  lift: number; // px up (negative = down)
  eyeOpen: number; // 0..1.2
  arc: number; // 0 capsule eyes .. 1 happy arcs
  gazeX: number; // -1..1
  gazeY: number; // -1 (up) .. 1 (down)
  light: number; // inner light 0..1
};

const base: Pose = { squash: 0, tilt: 0, lift: 0, eyeOpen: 1, arc: 0, gazeX: 0, gazeY: 0, light: 0 };

export const POSES: Record<Mood, Pose> = {
  idle: base,
  listening: { ...base, tilt: -3, lift: -1, gazeY: 0.75, gazeX: -0.1 },
  warm: { ...base, arc: 1, lift: 2, squash: -0.02 },
  focused: { ...base, eyeOpen: 0.45, gazeY: 0.3, squash: 0.02 },
  alert: { ...base, eyeOpen: 1.15, lift: 3, squash: -0.04 },
  writing: { ...base, tilt: -6, gazeY: 0.8, gazeX: -0.35 },
  noting: { ...base, eyeOpen: 0.85, gazeY: 0.4 },
  thinking: { ...base, gazeX: 0.45, gazeY: -0.7, tilt: 4, light: 0.55 },
  searching: { ...base, eyeOpen: 0.95, light: 0.65 },
  reading: { ...base, gazeY: 0.65, eyeOpen: 0.8, light: 0.45 },
  counting: { ...base, eyeOpen: 0.48, gazeY: 0.25, light: 0.6 },
  recalling: { ...base, gazeX: -0.55, gazeY: -0.75, tilt: -4, light: 0.5 },
  answering: { ...base, gazeY: 0.1, light: 0.2 },
  waiting: { ...base, gazeY: 0.9, eyeOpen: 0.9 },
  happy: { ...base, arc: 1, lift: 1.5 },
  sad: { ...base, squash: 0.07, lift: -2, eyeOpen: 0.35, gazeY: 0.6, tilt: 3 },
};
