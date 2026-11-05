import { useRef, useState } from "react";
import { reasonError } from "./draft-intent";
import type { Draft, Send, Session } from "./types";

const initial = {
  payout_id: "40000000-0000-4000-8000-000000000006",
  observation_id: "40000000-0000-4000-8000-000000000007",
  evidence_run_id: "40000000-0000-4000-8000-000000000008",
  reason:
    "Review local settlement after independent repair and verify current provider evidence in the console.",
  ttl_seconds: 900,
};

export function DraftPanel({
  send,
  session,
  provider,
}: {
  send: Send;
  session: Session;
  provider: string;
}) {
  const [intent, setIntent] = useState(
    session.demo
      ? initial
      : { ...initial, payout_id: "", observation_id: "", evidence_run_id: "" },
  );
  const [value, setValue] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const ids = useRef<{ thread: string; request: string } | null>(null);
  const invalidReason = reasonError(intent.reason);
  async function prepare(event: React.FormEvent) {
    event.preventDefault();
    if (invalidReason) {
      setError(invalidReason);
      return;
    }
    setBusy(true);
    setError("");
    ids.current ??= {
      thread: crypto.randomUUID(),
      request: crypto.randomUUID(),
    };
    try {
      setValue(
        await send<Draft>(`/v1/drafts/${ids.current.thread}`, {
          question: `Investigate payout ${intent.payout_id} for a local reprocessing draft.`,
          request_id: ids.current.request,
          provider,
          intent,
        }),
      );
    } catch (error) {
      if ((error as Error).name !== "AbortError")
        setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function review(continueReview: boolean) {
    if (!value?.draft || !ids.current) return;
    setBusy(true);
    setError("");
    try {
      setValue(
        await send<Draft>(
          `/v1/drafts/${ids.current.thread}/review?provider=${encodeURIComponent(provider)}`,
          {
            continue_review: continueReview,
            draft_sha256: value.draft.draft_sha256,
          },
        ),
      );
    } catch (error) {
      if ((error as Error).name !== "AbortError")
        setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <details className="draft-panel">
      <summary>
        <span>Prepare a reprocessing draft</span>
        <span className="subtle">Human review required</span>
      </summary>
      <p className="subtle">
        For a terminal observation with a local blocker and fresh reconciliation
        evidence. This creates a local draft for the existing console.
      </p>
      <form onSubmit={prepare}>
        <div className="draft-fields">
          {(["payout_id", "observation_id", "evidence_run_id"] as const).map(
            (field) => (
              <label key={field}>
                {field.replaceAll("_", " ")}
                <input
                  required
                  value={intent[field]}
                  onChange={(e) =>
                    setIntent({ ...intent, [field]: e.target.value })
                  }
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
            ),
          )}
        </div>
        <label>
          Reason
          <textarea
            required
            value={intent.reason}
            maxLength={256}
            onChange={(e) => setIntent({ ...intent, reason: e.target.value })}
          />
          <small>
            {invalidReason ??
              "Up to 256 UTF-8 bytes; the exact text is reviewed."}
          </small>
        </label>
        <div className="actions">
          <button
            className="secondary"
            disabled={busy || !!value?.draft || !!invalidReason}
          >
            {busy ? "Preparing…" : "Prepare draft"}
          </button>
          {busy && ids.current ? (
            <button
              type="button"
              className="secondary"
              onClick={() => {
                void send(
                  `/v1/runs/${ids.current!.request}`,
                  undefined,
                  "DELETE",
                ).catch(() => setError("The request has already finished."));
              }}
            >
              Cancel draft
            </button>
          ) : null}
          <button
            type="button"
            className="text-button"
            disabled={busy}
            onClick={() => {
              ids.current = null;
              setValue(null);
              setError("");
              setCopied(false);
            }}
          >
            Start a new request
          </button>
        </div>
      </form>
      {error ? (
        <p className="error" role="alert">
          {error}
        </p>
      ) : null}
      {value ? (
        <div className="draft-result">
          <span className="status">{value.status.replaceAll("_", " ")}</span>
          {value.reason ? <p>{value.reason.replaceAll("_", " ")}</p> : null}
          {value.draft ? (
            <>
              <p>
                Review these exact bytes. Expiry:{" "}
                {new Date(value.draft.expires_at).toLocaleTimeString()}.
              </p>
              <pre>{JSON.stringify(value.draft.payload, null, 2)}</pre>
              <p className="digest">
                Draft SHA-256: {value.draft.draft_sha256}
              </p>
              <p className="subtle">
                This digest identifies the local draft. The console creates and
                validates its own proposal.
              </p>
              {value.status === "awaiting_review" ? (
                <div className="actions">
                  <button onClick={() => void review(true)} disabled={busy}>
                    Review & continue
                  </button>
                  <button
                    className="secondary"
                    onClick={() => void review(false)}
                    disabled={busy}
                  >
                    Dismiss
                  </button>
                </div>
              ) : null}
              {value.status === "handoff_ready" ? (
                <div className="handoff">
                  <strong>Ready to submit through the console</strong>
                  <p>
                    Copy the payload, then submit it in your LedgerOps operator
                    console. Another actor must review the retained proposal.
                  </p>
                  <div className="actions">
                    <button
                      onClick={() => {
                        void navigator.clipboard
                          .writeText(
                            JSON.stringify(value.draft!.payload, null, 2),
                          )
                          .then(() => setCopied(true))
                          .catch(() =>
                            setError(
                              "Clipboard unavailable; select the payload text to copy it.",
                            ),
                          );
                      }}
                    >
                      {copied ? "Copied" : "Copy payload"}
                    </button>
                    <a
                      className="button secondary"
                      href={session.console_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open console ↗
                    </a>
                  </div>
                </div>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}
    </details>
  );
}
