const base = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true };

export const ArrowUp = () => (
  <svg {...base}>
    <path d="M12 19V5M5.5 11.5 12 5l6.5 6.5" />
  </svg>
);

export const Stop = () => (
  <svg {...base} fill="currentColor" stroke="none">
    <rect x="7" y="7" width="10" height="10" rx="2.2" />
  </svg>
);

export const History = () => (
  <svg {...base}>
    <path d="M4 6h16M4 12h10M4 18h13" />
  </svg>
);

export const Compose = () => (
  <svg {...base}>
    <path d="M12 5H6.5A2.5 2.5 0 0 0 4 7.5v10A2.5 2.5 0 0 0 6.5 20h10a2.5 2.5 0 0 0 2.5-2.5V12" />
    <path d="M17.6 4.4a1.9 1.9 0 0 1 2.7 2.7L12.5 15l-3.4.8.8-3.4 7.7-8Z" />
  </svg>
);

export const Chevron = ({ open }: { open: boolean }) => (
  <svg {...base} width={14} height={14} style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .2s" }}>
    <path d="m9 6 6 6-6 6" />
  </svg>
);
