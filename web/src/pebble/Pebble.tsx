import {
  motion,
  useAnimationFrame,
  useMotionValue,
  useReducedMotion,
  useSpring,
  useTransform,
} from "motion/react";
import { useEffect, useId, useRef } from "react";
import { POSES, type Mood } from "./moods";
import "./pebble.css";

type Props = {
  mood: Mood;
  size?: number;
  /** Nudges a soft nod while text is being revealed. */
  talking?: boolean;
};

const SPRING = { stiffness: 170, damping: 19, mass: 0.9 };
const EYE_SPRING = { stiffness: 260, damping: 22 };

/** Superellipse body, slightly fuller at the bottom: a pebble, not a circle or a box. */
function bodyPath(squash: number, breath: number): string {
  const cx = 60;
  const cy = 66;
  const a = 43 * (1 + squash * 0.6) * (1 + breath * 0.012);
  const b = 33 * (1 - squash) * (1 + breath * 0.02);
  const n = 2.6; // between an ellipse (2) and a squircle (4): a river stone
  const pts: string[] = [];
  for (let i = 0; i < 72; i++) {
    const t = (i / 72) * Math.PI * 2;
    const c = Math.cos(t);
    const s = Math.sin(t);
    // Fuller and a touch wider at the bottom, slightly fuller on the left: not perfectly symmetric.
    const wide = (s > 0 ? 1 + 0.05 * s : 1 - 0.03 * -s) * (c < 0 ? 1.015 : 1);
    const x = cx + a * wide * Math.sign(c) * Math.abs(c) ** (2 / n);
    const y = cy + b * (s > 0 ? 1.08 : 1) * Math.sign(s) * Math.abs(s) ** (2 / n);
    pts.push(`${x.toFixed(2)},${y.toFixed(2)}`);
  }
  return `M${pts.join("L")}Z`;
}

export function Pebble({ mood, size = 112, talking = false }: Props) {
  const reduce = useReducedMotion();
  const uid = useId().replace(/:/g, "");

  const squash = useSpring(0, SPRING);
  const tilt = useSpring(0, SPRING);
  const lift = useSpring(0, SPRING);
  const eyeOpen = useSpring(1, EYE_SPRING);
  const arc = useSpring(0, EYE_SPRING);
  const gazeX = useSpring(0, EYE_SPRING);
  const gazeY = useSpring(0, EYE_SPRING);
  const light = useSpring(0, { stiffness: 80, damping: 20 });
  const breath = useMotionValue(0);
  const blink = useSpring(1, { stiffness: 900, damping: 40 });
  const sweep = useMotionValue(0);

  const moodRef = useRef(mood);
  const talkingRef = useRef(talking);
  moodRef.current = mood;
  talkingRef.current = talking;
  const life = useRef({ nextBlink: 2500, nextGlance: 1800, glance: [0, 0] as [number, number], hopUntil: 0 });

  // A little hop when Pebble becomes warm or happy.
  useEffect(() => {
    if (mood === "warm" || mood === "happy") life.current.hopUntil = performance.now() + 420;
  }, [mood]);

  useAnimationFrame((t) => {
    const p = POSES[moodRef.current];
    const L = life.current;
    if (reduce) {
      squash.set(p.squash);
      tilt.set(0);
      lift.set(0);
      eyeOpen.set(p.eyeOpen);
      arc.set(p.arc);
      gazeX.set(p.gazeX * 0.5);
      gazeY.set(p.gazeY * 0.5);
      light.set(p.light);
      return;
    }
    const sec = t / 1000;
    breath.set(Math.sin(sec * 1.6));

    // Blink every 3-7 s, occasionally twice.
    if (t > L.nextBlink && p.arc < 0.5) {
      blink.set(0.08);
      setTimeout(() => blink.set(1), 90);
      const twice = Math.random() < 0.18;
      if (twice) setTimeout(() => (blink.set(0.08), setTimeout(() => blink.set(1), 90)), 240);
      L.nextBlink = t + 3000 + Math.random() * 4000;
    }

    let gx = p.gazeX;
    let gy = p.gazeY;
    let liftExtra = 0;
    let tiltExtra = 0;
    switch (moodRef.current) {
      case "idle":
        if (t > L.nextGlance) {
          L.glance = Math.random() < 0.45 ? [0, 0] : [(Math.random() - 0.5) * 1.2, (Math.random() - 0.5) * 0.6];
          L.nextGlance = t + 1800 + Math.random() * 3200;
        }
        [gx, gy] = L.glance;
        break;
      case "searching":
        gx = Math.sin(sec * 2.3) * 0.85; // scanning left and right
        break;
      case "reading":
        gx = Math.sin(sec * 1.15) * 0.45;
        break;
      case "counting":
        gx = -0.6 + 0.4 * (Math.floor(sec * 4.5) % 4); // quick ticks
        liftExtra = Math.floor(sec * 4.5) % 2 ? 0.6 : 0;
        break;
      case "thinking":
      case "recalling":
        tiltExtra = Math.sin(sec * 0.9) * 2;
        break;
    }
    if (talkingRef.current) liftExtra += Math.max(0, Math.sin(sec * 7)) * 1.4;
    if (performance.now() < L.hopUntil) liftExtra += 5 * Math.sin(((L.hopUntil - performance.now()) / 420) * Math.PI);

    squash.set(p.squash + breath.get() * 0.004);
    tilt.set(p.tilt + tiltExtra);
    lift.set(p.lift + liftExtra);
    eyeOpen.set(p.eyeOpen);
    arc.set(p.arc);
    gazeX.set(gx);
    gazeY.set(gy);
    light.set(p.light);
    sweep.set(((sec % 1.6) / 1.6) * 2 - 0.5); // same period as the text glimmer
  });

  // Function-form transforms re-run whenever a motion value they read changes.
  const d = useTransform(() => bodyPath(squash.get(), breath.get()));
  const transform = useTransform(
    () => `translate(0 ${(-lift.get()).toFixed(2)}) rotate(${tilt.get().toFixed(2)} 60 70)`,
  );
  const eyeH = useTransform(() => Math.max(1.2, 15 * eyeOpen.get() * blink.get()));
  const eyeY = useTransform(() => 58 + gazeY.get() * 4.5 - eyeH.get() / 2);
  const leftX = useTransform(() => 44 + gazeX.get() * 5.5 - 3.5);
  const rightX = useTransform(() => 69 + gazeX.get() * 5.5 - 3.5);
  const capsuleOpacity = useTransform(() => 1 - arc.get());
  const arcLeft = useTransform(() => arcPath(47.5 + gazeX.get() * 5, 60 + gazeY.get() * 3));
  const arcRight = useTransform(() => arcPath(72.5 + gazeX.get() * 5, 60 + gazeY.get() * 3));
  const lightX = useTransform(() => 20 + sweep.get() * 80);
  const lightOpacity = useTransform(() => light.get() * 0.55);

  return (
    <div className="pebble" style={{ width: size, height: size }} aria-hidden="true">
      <svg viewBox="0 0 120 120" width={size} height={size}>
        <defs>
          <radialGradient id={`body-${uid}`} cx="38%" cy="30%" r="80%">
            <stop offset="0%" stopColor="var(--stone-light)" />
            <stop offset="55%" stopColor="var(--stone)" />
            <stop offset="100%" stopColor="var(--stone-shade)" />
          </radialGradient>
          <radialGradient id={`light-${uid}`}>
            <stop offset="0%" stopColor="var(--ember)" stopOpacity="0.9" />
            <stop offset="100%" stopColor="var(--ember)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id={`shadow-${uid}`}>
            <stop offset="0%" stopColor="var(--ink)" stopOpacity="0.16" />
            <stop offset="100%" stopColor="var(--ink)" stopOpacity="0" />
          </radialGradient>
          <clipPath id={`clip-${uid}`}>
            <motion.path d={d} />
          </clipPath>
        </defs>
        <ellipse cx="60" cy="108" rx="34" ry="5" fill={`url(#shadow-${uid})`} />
        <motion.g transform={transform}>
          <motion.path d={d} fill={`url(#body-${uid})`} />
          <g clipPath={`url(#clip-${uid})`}>
            <motion.ellipse cx={lightX} cy="70" rx="26" ry="34" fill={`url(#light-${uid})`} opacity={lightOpacity} />
          </g>
          <motion.g opacity={capsuleOpacity} fill="var(--pebble-eye)">
            <motion.rect x={leftX} y={eyeY} width="7" height={eyeH} rx="3.5" />
            <motion.rect x={rightX} y={eyeY} width="7" height={eyeH} rx="3.5" />
          </motion.g>
          <motion.g opacity={arc} stroke="var(--pebble-eye)" strokeWidth="3.4" strokeLinecap="round" fill="none">
            <motion.path d={arcLeft} />
            <motion.path d={arcRight} />
          </motion.g>
        </motion.g>
      </svg>
    </div>
  );
}

function arcPath(x: number, y: number): string {
  return `M${(x - 5.5).toFixed(2)} ${(y + 2).toFixed(2)} Q${x.toFixed(2)} ${(y - 5).toFixed(2)} ${(x + 5.5).toFixed(2)} ${(y + 2).toFixed(2)}`;
}
