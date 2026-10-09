import { useMemo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Source } from "../api";
import { renderable } from "../stream/smoother";

/** Turn [#123] citations into numbered chips (in order of first appearance). */
function withCitationLinks(text: string, order: Map<number, number>): string {
  return text.replace(/\[#(\d+)\]/g, (_, id: string) => {
    const n = Number(id);
    if (!order.has(n)) order.set(n, order.size + 1);
    return `[${order.get(n)}](#cite-${n})`;
  });
}

export function Answer({ text, sources, streaming = false }: { text: string; sources: Source[]; streaming?: boolean }) {
  const byId = useMemo(() => new Map(sources.map((s) => [s.id, s])), [sources]);
  const order = new Map<number, number>();
  const md = withCitationLinks(streaming ? renderable(text) : text, order);
  return (
    <div className="answer">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) =>
            href?.startsWith("#cite-") ? (
              <Citation source={byId.get(Number(href.slice(6)))} label={children} />
            ) : (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            ),
        }}
      >
        {md}
      </Markdown>
      {!streaming && sources.length > 0 && <Sources sources={sources} order={order} />}
    </div>
  );
}

function Citation({ source, label }: { source?: Source; label: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="cite" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button type="button" className="cite-chip" onFocus={() => setOpen(true)} onBlur={() => setOpen(false)}
        aria-label={source ? `Source: ${source.subject}` : "Source"}>
        {label}
      </button>
      {open && source && (
        <span className="cite-card" role="tooltip">
          <span className="cite-subject">{source.subject || "(no subject)"}</span>
          <span className="cite-meta">
            {source.from}
            {source.date && ` · ${formatDate(source.date)}`}
          </span>
        </span>
      )}
    </span>
  );
}

function Sources({ sources, order }: { sources: Source[]; order: Map<number, number> }) {
  const sorted = [...sources].sort((a, b) => (order.get(a.id) ?? 99) - (order.get(b.id) ?? 99));
  return (
    <ol className="sources">
      {sorted.map((s) => (
        <li key={s.id}>
          <span className="sources-n">{order.get(s.id)}</span>
          <span className="sources-subject">{s.subject || "(no subject)"}</span>
          <span className="faint">
            {s.from}
            {s.date && ` · ${formatDate(s.date)}`}
          </span>
        </li>
      ))}
    </ol>
  );
}

function formatDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
