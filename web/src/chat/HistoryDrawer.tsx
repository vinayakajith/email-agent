import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { get } from "../api";
import { Compose } from "./icons";
import type { Conversation } from "./useChat";

export function HistoryDrawer({ open, current, onClose, onPick }: {
  open: boolean;
  current: string | null;
  onClose: () => void;
  onPick: (id: string | null) => void;
}) {
  const [items, setItems] = useState<Conversation[]>([]);
  useEffect(() => {
    if (open) get<Conversation[]>("/api/conversations").then(setItems, () => setItems([]));
  }, [open]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div className="scrim" onClick={onClose} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} />
          <motion.nav className="drawer" aria-label="Conversations" initial={{ x: -320 }} animate={{ x: 0 }} exit={{ x: -320 }}
            transition={{ type: "spring", stiffness: 380, damping: 38 }}>
            <button className="drawer-new" onClick={() => onPick(null)}>
              <Compose /> New conversation
            </button>
            <ul>
              {items.map((c) => (
                <li key={c.id}>
                  <button className={c.id === current ? "active" : ""} onClick={() => onPick(c.id)}>
                    <span className="drawer-title">{c.title || "Untitled"}</span>
                    <span className="faint">{relative(c.updated_at)}</span>
                  </button>
                </li>
              ))}
            </ul>
          </motion.nav>
        </>
      )}
    </AnimatePresence>
  );
}

function relative(iso: string): string {
  const mins = (Date.now() - new Date(iso).getTime()) / 60000;
  if (mins < 60) return `${Math.max(1, Math.round(mins))}m`;
  if (mins < 1440) return `${Math.round(mins / 60)}h`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
