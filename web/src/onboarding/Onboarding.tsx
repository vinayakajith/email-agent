import { AnimatePresence, motion } from "motion/react";
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { ApiError, get, post, type Job, type Session, type StepName } from "../api";
import type { Mood } from "../pebble/moods";
import { Pebble } from "../pebble/Pebble";
import "./onboarding.css";

type Step = "welcome" | StepName;

export function Onboarding({ session, onDone }: { session: Session; onDone: () => void }) {
  const fresh = !Object.values(session.onboarding.steps).some(Boolean);
  const [step, setStep] = useState<Step>(fresh ? "welcome" : (session.onboarding.next ?? "first_ingest"));
  const [mood, setMood] = useState<Mood>(fresh ? "warm" : "idle");

  const advance = async () => {
    const next = (await get<Session>("/api/session")).onboarding.next;
    if (next) {
      setStep(next);
      setMood("idle");
    } else {
      setMood("happy");
      setTimeout(onDone, 700);
    }
  };

  return (
    <main className="onboarding">
      <Pebble mood={mood} size={128} />
      <AnimatePresence mode="wait">
        <motion.section
          key={step}
          className="onboarding-card"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -8 }}
          transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
        >
          {step === "welcome" && <Welcome onNext={advance} />}
          {step === "api_keys" && <Keys onDone={advance} setMood={setMood} />}
          {step === "gmail" && <Gmail onDone={advance} setMood={setMood} />}
          {step === "profile" && <Profile onDone={advance} setMood={setMood} />}
          {step === "first_ingest" && (
            <FirstSync running={session.onboarding.ingest_running} onDone={advance} setMood={setMood} />
          )}
        </motion.section>
      </AnimatePresence>
    </main>
  );
}

type StepProps = { onDone: () => void; setMood: (m: Mood) => void };

function Welcome({ onNext }: { onNext: () => void }) {
  return (
    <>
      <h1>Hi. I read your mail so you don't have to.</h1>
      <p className="muted">
        Ask about bills, people, subscriptions or whatever needs you. Everything stays on this machine except the
        model calls.
      </p>
      <button className="button" onClick={onNext} autoFocus>
        Get started
      </button>
    </>
  );
}

function Form({ children, onSubmit, busy, cta, error }: {
  children: ReactNode;
  onSubmit: () => Promise<void>;
  busy: boolean;
  cta: string;
  error: string | null;
}) {
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void onSubmit();
  };
  return (
    <form onSubmit={submit} className="onboarding-form">
      {children}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <button className="button" disabled={busy}>
        {busy ? <span className="glimmer">Checking</span> : cta}
      </button>
    </form>
  );
}

function useSubmit(onDone: () => void, setMood: (m: Mood) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = (fn: () => Promise<unknown>) => async () => {
    setBusy(true);
    setError(null);
    setMood("thinking");
    try {
      await fn();
      setMood("happy");
      setTimeout(onDone, 450);
    } catch (e) {
      setMood("sad");
      setTimeout(() => setMood("idle"), 1600);
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run };
}

function Keys({ onDone, setMood }: StepProps) {
  const [openai, setOpenai] = useState("");
  const [voyage, setVoyage] = useState("");
  const { busy, error, run } = useSubmit(onDone, setMood);
  return (
    <>
      <h1>Connect the models</h1>
      <p className="muted">Keys are checked once and kept in your system keychain.</p>
      <Form
        cta="Continue"
        busy={busy}
        error={error}
        onSubmit={run(() => post("/api/onboarding/keys", { openai_key: openai, voyage_key: voyage }))}
      >
        <div className="field">
          <label htmlFor="openai">OpenAI API key</label>
          <input id="openai" className="input" type="password" autoComplete="off" value={openai}
            onChange={(e) => setOpenai(e.target.value)} onFocus={() => setMood("listening")} required autoFocus />
        </div>
        <div className="field">
          <label htmlFor="voyage">Voyage AI API key</label>
          <input id="voyage" className="input" type="password" autoComplete="off" value={voyage}
            onChange={(e) => setVoyage(e.target.value)} onFocus={() => setMood("listening")} required />
        </div>
      </Form>
    </>
  );
}

function Gmail({ onDone, setMood }: StepProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { busy, error, run } = useSubmit(onDone, setMood);
  return (
    <>
      <h1>Connect Gmail</h1>
      <ol className="steps muted">
        <li>Turn on 2-Step Verification for your Google account.</li>
        <li>
          Create an{" "}
          <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer">
            app password
          </a>{" "}
          and paste it below.
        </li>
        <li>Make sure IMAP is enabled in Gmail settings.</li>
      </ol>
      <Form cta="Connect" busy={busy} error={error}
        onSubmit={run(() => post("/api/onboarding/gmail", { email, app_password: password }))}>
        <div className="field">
          <label htmlFor="email">Gmail address</label>
          <input id="email" className="input" type="email" autoComplete="email" value={email}
            onChange={(e) => setEmail(e.target.value)} onFocus={() => setMood("listening")} required autoFocus />
        </div>
        <div className="field">
          <label htmlFor="app-password">App password</label>
          <input id="app-password" className="input" type="password" autoComplete="off" value={password}
            placeholder="xxxx xxxx xxxx xxxx" onChange={(e) => setPassword(e.target.value)}
            onFocus={() => setMood("listening")} required />
        </div>
      </Form>
    </>
  );
}

function Profile({ onDone, setMood }: StepProps) {
  const zones = useMemo(() => Intl.supportedValuesOf("timeZone"), []);
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone);
  const [about, setAbout] = useState("");
  const { busy, error, run } = useSubmit(onDone, setMood);
  return (
    <>
      <h1>A little about you</h1>
      <p className="muted">This is the only thing I keep in mind in every conversation, so keep it short.</p>
      <Form cta="Continue" busy={busy} error={error}
        onSubmit={run(() => post("/api/onboarding/profile", { name, timezone, about }))}>
        <div className="field">
          <label htmlFor="name">What should I call you?</label>
          <input id="name" className="input" value={name} maxLength={80} required autoFocus
            onChange={(e) => (setName(e.target.value), setMood(e.target.value ? "warm" : "listening"))}
            onFocus={() => setMood("listening")} />
        </div>
        <div className="field">
          <label htmlFor="tz">Timezone</label>
          <select id="tz" className="input" value={timezone} onChange={(e) => setTimezone(e.target.value)}>
            {zones.map((z) => (
              <option key={z}>{z}</option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="about">Anything I should always know? <span className="faint">(optional)</span></label>
          <input id="about" className="input" value={about} maxLength={300}
            placeholder="e.g. Founder at Acme; I prefer short answers"
            onChange={(e) => setAbout(e.target.value)} onFocus={() => setMood("noting")} />
        </div>
      </Form>
    </>
  );
}

const RANGES = [
  { id: "30d", label: "30 days" },
  { id: "90d", label: "90 days" },
  { id: "1y", label: "1 year" },
  { id: "all", label: "Everything" },
];

const STAGE_LABEL: Record<string, string> = {
  fetch: "Fetching your mail",
  enrich: "Reading each email",
  embed: "Making it searchable",
};

function FirstSync({ running, onDone, setMood }: StepProps & { running: boolean }) {
  const [range, setRange] = useState("90d");
  const [jobId, setJobId] = useState<number | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (jobId === null) return;
    setMood("reading");
    const timer = setInterval(async () => {
      const j = await get<Job>(`/api/jobs/${jobId}`);
      setJob(j);
      if (j.status === "done") {
        clearInterval(timer);
        setMood("happy");
        setTimeout(onDone, 900);
      } else if (j.status === "failed") {
        clearInterval(timer);
        setMood("sad");
        setError(j.error ?? "The sync failed.");
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [jobId, onDone, setMood]);

  const start = async () => {
    setError(null);
    try {
      setJobId((await post<{ job_id: number }>("/api/onboarding/ingest", { range })).job_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't start the sync.");
    }
  };

  if (jobId !== null || running) {
    const pct = job && job.stage_total ? Math.min(100, (job.stage_done / job.stage_total) * 100) : 0;
    return (
      <>
        <h1>Reading your mail</h1>
        <p className="muted">
          This runs in the background. You can start chatting once it finishes; new mail keeps arriving on its own.
        </p>
        <div className="sync">
          <div className="sync-label">
            <span className="glimmer">{(job?.stage && STAGE_LABEL[job.stage]) || "Starting"}</span>
            {job && job.stage_total > 0 && (
              <span className="faint tabular">
                {job.stage_done.toLocaleString()} of {job.stage_total.toLocaleString()}
              </span>
            )}
          </div>
          <div className="sync-bar">
            <motion.div className="sync-fill" animate={{ width: `${pct}%` }} transition={{ type: "spring", stiffness: 60, damping: 18 }} />
          </div>
          {error && <p className="form-error" role="alert">{error}</p>}
        </div>
      </>
    );
  }

  return (
    <>
      <h1>How far back should I read?</h1>
      <p className="muted">You can always widen this later. More mail means a longer first read.</p>
      <div className="segmented" role="radiogroup" aria-label="How far back">
        {RANGES.map((r) => (
          <button key={r.id} role="radio" aria-checked={range === r.id}
            className={range === r.id ? "active" : ""} onClick={() => setRange(r.id)}>
            {r.label}
          </button>
        ))}
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <button className="button" onClick={start}>
        Start reading
      </button>
    </>
  );
}
