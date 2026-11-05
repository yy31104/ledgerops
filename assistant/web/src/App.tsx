import { useRef, useState } from "react";
import { AnswerPanel, Sources } from "./AnswerPanel";
import { DraftPanel } from "./DraftPanel";
import { readEvents } from "./sse";
import type { Investigation, Send, Session } from "./types";

const cases = [
  {
    name: "Payout refused",
    code: "LIMIT_EXCEEDED",
    detail: "Read the retained admission result.",
    question:
      "Why was payout 40000000-0000-4000-8000-000000000004 refused with LIMIT_EXCEEDED?",
  },
  {
    name: "Dispatch timed out",
    code: "UNKNOWN · HELD",
    detail: "A timeout leaves the outcome open.",
    question: "Is payout 40000000-0000-4000-8000-000000000003 paid right now?",
  },
  {
    name: "Reconciliation mismatch",
    code: "MISSING_INTERNAL",
    detail: "Find what the statement and ledger disagree on.",
    question:
      "What does MISSING_INTERNAL mean, and should I post a correcting journal?",
  },
  {
    name: "Local settlement blocked",
    code: "HUMAN REVIEW",
    detail: "Review a draft before opening the console.",
    question:
      "Investigate payout 40000000-0000-4000-8000-000000000006 and explain the local settlement blocker.",
  },
];

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [credential, setCredential] = useState("");
  const [selected, setSelected] = useState(1);
  const [question, setQuestion] = useState(cases[1].question);
  const [provider, setProvider] = useState("fake");
  const [result, setResult] = useState<Investigation | null>(null);
  const [text, setText] = useState("");
  const [phase, setPhase] = useState("");
  const [busy, setBusy] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState("");
  const token = useRef("");
  const generation = useRef(0);
  const active = useRef<{ id: string; controller: AbortController } | null>(
    null,
  );

  const send: Send = async <T,>(
    path: string,
    data?: unknown,
    method?: string,
  ): Promise<T> => {
    const version = generation.current;
    const response = await fetch(path, {
      method: method ?? (data === undefined ? "GET" : "POST"),
      cache: "no-store",
      headers: {
        Authorization: `Bearer ${token.current}`,
        ...(data === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(data === undefined ? {} : { body: JSON.stringify(data) }),
    });
    const payload = await response.json();
    if (version !== generation.current)
      throw new DOMException("Session changed", "AbortError");
    if (!response.ok)
      throw new Error(
        typeof payload.detail === "string"
          ? payload.detail
          : "The request was rejected.",
      );
    return payload as T;
  };

  async function connect(value: string) {
    setConnecting(true);
    setError("");
    setCredential("");
    const version = ++generation.current;
    try {
      const response = await fetch("/v1/session", {
        headers: { Authorization: `Bearer ${value}` },
        cache: "no-store",
      });
      if (!response.ok)
        throw new Error("Could not verify a current LedgerOps credential.");
      const identity: Session = await response.json();
      if (version !== generation.current) return;
      token.current = value;
      setSession(identity);
      setProvider(identity.providers[0]);
    } catch (error) {
      if (version === generation.current) setError((error as Error).message);
    } finally {
      if (version === generation.current) setConnecting(false);
    }
  }

  async function demo() {
    setError("");
    try {
      const response = await fetch("/v1/demo", { cache: "no-store" });
      if (!response.ok)
        throw new Error("Demo mode is not enabled on this server.");
      const data = await response.json();
      await connect(data.token);
    } catch (error) {
      setError((error as Error).message);
    }
  }

  function stop() {
    const run = active.current;
    if (run) {
      void send(`/v1/runs/${run.id}`, undefined, "DELETE").catch(
        () => undefined,
      );
      run.controller.abort();
    }
  }

  function disconnect() {
    stop();
    generation.current++;
    token.current = "";
    active.current = null;
    setSession(null);
    setCredential("");
    setResult(null);
    setText("");
    setBusy(false);
    setError("");
  }

  async function investigate(event: React.FormEvent) {
    event.preventDefault();
    if (!session) return;
    const version = generation.current;
    const run = { id: crypto.randomUUID(), controller: new AbortController() };
    active.current = run;
    setBusy(true);
    setText("");
    setResult(null);
    setError("");
    setPhase("Starting");
    let completed = false;
    try {
      const response = await fetch("/v1/assist", {
        method: "POST",
        signal: run.controller.signal,
        cache: "no-store",
        headers: {
          Authorization: `Bearer ${token.current}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ question, request_id: run.id, provider }),
      });
      if (!response.ok || !response.body)
        throw new Error(
          response.status === 401
            ? "Your credential is no longer current."
            : "The investigation could not start.",
        );
      for await (const frame of readEvents(response.body)) {
        if (version !== generation.current) return;
        const data = JSON.parse(frame.data);
        if (frame.event === "step")
          setPhase(
            (
              {
                decide: "Choosing evidence",
                read: "Reading API",
                answer: "Checking answer",
              } as Record<string, string>
            )[data.phase] ?? "Working",
          );
        if (frame.event === "text")
          setText((previous) => previous + data.delta);
        if (frame.event === "result") {
          setResult(data);
          setText(data.answer.text);
          completed = true;
        }
        if (frame.event === "error")
          throw new Error(data.reason.replaceAll("_", " "));
      }
      if (!completed)
        throw new Error("The response ended before a checked answer arrived.");
    } catch (error) {
      if (version === generation.current)
        setError(
          (error as Error).name === "AbortError"
            ? "Investigation cancelled."
            : (error as Error).message,
        );
    } finally {
      if (version === generation.current) {
        setBusy(false);
        active.current = null;
      }
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/">
          <span className="brand-mark" aria-hidden="true">
            L<span>↗</span>
          </span>
          <span>
            LedgerOps<small>OPERATOR ASSISTANT</small>
          </span>
        </a>
        <div className="workspace-label">INVESTIGATION WORKSPACE</div>
        <h2 className="case-heading">Synthetic cases</h2>
        <nav aria-label="Example investigations">
          {cases.map((item, index) => (
            <button
              key={item.code}
              className={`case ${selected === index ? "selected" : ""}`}
              disabled={busy}
              onClick={() => {
                setSelected(index);
                setQuestion(item.question);
              }}
            >
              <span className="case-number">0{index + 1}</span>
              <span>
                <strong>{item.name}</strong>
                <small>{item.code}</small>
                <span className="case-detail">{item.detail}</span>
              </span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <span className="live-dot" />
          Read-only tools
          <p>
            Documentation explains the rules.
            <br />
            The API reports current facts.
          </p>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div>
            <span className="eyebrow">LEDGER OPERATIONS</span>
            <h1>Operator assistant</h1>
          </div>
          <div className="topbar-status">
            <span className="status">Read-only</span>
            {session ? (
              <button className="text-button" onClick={disconnect}>
                Disconnect
              </button>
            ) : (
              <span className="subtle">Not connected</span>
            )}
          </div>
        </header>
        <div className="content-grid">
          <main>
            <div className="intro">
              <span className="eyebrow">INVESTIGATE BEFORE ACTING</span>
              <h2>What does the evidence say?</h2>
              <p>
                Follow an incident from operating rules to current observations,
                with references you can inspect.
              </p>
            </div>
            {!session ? (
              <section
                className="connect-card"
                aria-label="Connect an operator"
              >
                <div className="section-head">
                  <h3>Connect your operator</h3>
                  <span className="status">Session only</span>
                </div>
                <p>
                  Your LedgerOps credential stays in memory and is checked
                  against your current permissions.
                </p>
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    void connect(credential);
                  }}
                >
                  <label>
                    Operator token
                    <input
                      type="password"
                      autoComplete="off"
                      value={credential}
                      onChange={(event) => setCredential(event.target.value)}
                      placeholder="lo1_…"
                      required
                    />
                  </label>
                  <div className="actions">
                    <button disabled={connecting}>
                      {connecting ? "Connecting…" : "Connect"}
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => void demo()}
                      disabled={connecting}
                    >
                      Try synthetic demo
                    </button>
                  </div>
                </form>
              </section>
            ) : (
              <div className="session-strip">
                <span className="live-dot" />
                <strong>
                  {session.demo ? "Synthetic demo" : "Operator connected"}
                </strong>
                <span>
                  {
                    session.scopes.filter((scope) => scope.endsWith(":read"))
                      .length
                  }{" "}
                  read scopes
                </span>
                <span className="mono">
                  {session.workspace_id.slice(0, 8)}…
                </span>
              </div>
            )}
            <form
              className="composer"
              onSubmit={(event) => void investigate(event)}
            >
              <label htmlFor="question">Investigation question</label>
              <textarea
                id="question"
                rows={3}
                value={question}
                maxLength={8000}
                onChange={(event) => setQuestion(event.target.value)}
                disabled={busy}
              />
              <div className="composer-controls">
                <label className="provider-label">
                  Answer model
                  <select
                    aria-label="Answer model provider"
                    value={provider}
                    onChange={(event) => setProvider(event.target.value)}
                    disabled={!session || busy}
                  >
                    {(session?.providers ?? ["fake"]).map((name) => (
                      <option key={name} value={name}>
                        {name === "fake"
                          ? "Fake · no model spend"
                          : name === "openai_api"
                            ? "OpenAI API"
                            : "Ollama · local"}
                      </option>
                    ))}
                    <option disabled value="chatgpt_subscription">
                      ChatGPT Pro · local CLI
                    </option>
                  </select>
                </label>
                {busy ? (
                  <button type="button" className="secondary" onClick={stop}>
                    Cancel
                  </button>
                ) : (
                  <button disabled={!session || !question.trim()}>
                    Investigate <span aria-hidden="true">↗</span>
                  </button>
                )}
              </div>
              {provider === "fake" ? (
                <small className="model-note">
                  Deterministic fake generation. Answer quality has not been
                  measured with a real model.
                </small>
              ) : null}
            </form>
            {error ? (
              <p className="error" role="alert">
                {error}
              </p>
            ) : null}
            <AnswerPanel
              result={result}
              text={text}
              busy={busy}
              phase={phase}
            />
            {session?.scopes.includes("approval:propose") ? (
              <DraftPanel send={send} session={session} provider={provider} />
            ) : null}
            <footer>
              Rules, observations and suggestions. Human approval stays in the
              operator console.
            </footer>
          </main>
          <Sources result={result} />
        </div>
      </div>
    </div>
  );
}
