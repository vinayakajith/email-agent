import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { Chevron } from "./icons";

/** The live thinking row: what the agent is doing, with a glimmer of light moving across it. */
export function Thinking({ activity, trail, thought, startedAt, answeredAt }: {
  activity: string | null;
  trail: string[];
  thought: string;
  startedAt: number;
  answeredAt: number | null;
}) {
  const elapsed = useElapsed(startedAt, answeredAt);

  if (answeredAt !== null) {
    const seconds = Math.max(1, Math.round((answeredAt - startedAt) / 1000));
    return <ThoughtFor seconds={seconds} thought={thought} trail={trail} />;
  }

  return (
    <div className="thinking" aria-live="off">
      <div className="thinking-now">
        <AnimatePresence mode="wait" initial={false}>
          <motion.span key={activity ?? "thinking"} className="glimmer" initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.2 }}>
            {activity ?? "Thinking"}
          </motion.span>
        </AnimatePresence>
        <span className="faint tabular">{elapsed}s</span>
      </div>
      {trail.length > 0 && <p className="thinking-trail">{trail.join(" · ")}</p>}
    </div>
  );
}

/** A finished turn: "Thought for 6s · Added up your bills", expandable to the reasoning summary. */
export function ThoughtFor({ seconds, thought, trail }: { seconds: number; thought: string; trail: string[] }) {
  const [open, setOpen] = useState(false);
  if (!thought && !trail.length) return null;
  return (
    <div className="thinking done">
      <button type="button" className="thinking-toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open}
        disabled={!thought}>
        Thought for {seconds}s{trail.length > 0 && <span> · {trail.join(" · ")}</span>}
        {thought && <Chevron open={open} />}
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div className="thinking-detail" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}>
            <p className="thinking-text">{thought}</p>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function useElapsed(startedAt: number, stoppedAt: number | null): number {
  const [now, setNow] = useState(performance.now());
  useEffect(() => {
    if (stoppedAt !== null) return;
    const t = setInterval(() => setNow(performance.now()), 1000);
    return () => clearInterval(t);
  }, [stoppedAt]);
  return Math.floor(((stoppedAt ?? now) - startedAt) / 1000);
}
