import type { Investigation } from "./types";

export function AnswerPanel({
  result,
  text,
  busy,
  phase,
}: {
  result: Investigation | null;
  text: string;
  busy: boolean;
  phase: string;
}) {
  return (
    <section
      className="answer-panel"
      aria-labelledby="answer-title"
      aria-busy={busy}
    >
      <div className="section-head">
        <h2 id="answer-title">Investigation</h2>
        {busy ? (
          <span className="status working">{phase}</span>
        ) : result ? (
          <span className="status">
            {result.answer.refused ? "Evidence insufficient" : "Response ready"}
          </span>
        ) : null}
      </div>
      {text ? (
        <p className="answer-text" aria-live="polite">
          {text}
        </p>
      ) : (
        <div className="empty-answer">
          <span className="empty-symbol" aria-hidden="true">
            ↳
          </span>
          <h3>Start with the evidence.</h3>
          <p>
            Ask about an incident or choose a synthetic case. Current facts come
            from the API; operating rules come from the documentation.
          </p>
        </div>
      )}
      {result ? (
        <>
          <div className="answer-meta">
            <span>{result.steps} model steps</span>
            <span>{result.answer.citations.length} checked references</span>
            <span>Est. ${result.cost_usd.toFixed(5)}</span>
          </div>
          {result.answer.uncertainty.length ? (
            <aside className="uncertainty">
              <strong>Evidence boundary</strong>
              {result.answer.uncertainty.map((note) => (
                <p key={note}>{note}</p>
              ))}
            </aside>
          ) : null}
          {result.answer.refused ? (
            <p className="refusal">
              {result.answer.reason.replaceAll("_", " ")}
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

export function Sources({ result }: { result: Investigation | null }) {
  const citations = result?.answer.citations ?? [];
  return (
    <aside className="sources" aria-labelledby="sources-title">
      <div className="section-head">
        <h2 id="sources-title">References</h2>
        <span className="count">{citations.length}</span>
      </div>
      <p className="subtle">Only passages supplied to the answer are shown.</p>
      {citations.length ? (
        citations.map((cite) => (
          <details className="source-card" key={cite.chunk_id}>
            <summary>
              <span className="source-type">
                {cite.chunk_id.startsWith("api:") ? "LIVE API" : "DOCUMENT"}
              </span>
              <span className="source-path">{cite.section_id}</span>
              <span className="source-open">Read excerpt ↓</span>
            </summary>
            <pre>{cite.excerpt}</pre>
            {cite.revision ? <small>Revision {cite.revision}</small> : null}
          </details>
        ))
      ) : (
        <div className="source-empty">
          References appear after an investigation.
        </div>
      )}
      <div className="boundary-note">
        <span className="line-icon" aria-hidden="true">
          ⊞
        </span>
        <strong>Human control stays in place</strong>
        <p>
          The assistant reads and drafts. The existing console handles
          submission, approval and execution.
        </p>
      </div>
    </aside>
  );
}
