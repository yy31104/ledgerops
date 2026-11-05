"""Investigate, persist one draft, interrupt for review, hand off to the console."""

from dataclasses import asdict, dataclass
from typing import TypedDict
from uuid import UUID

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from ledgerops_assistant.drafts import DraftIntent, DraftStore, WorkflowConflict, digest
from ledgerops_assistant.tools import AuthenticationRequired


class WorkflowState(TypedDict, total=False):
    question: str
    request_id: str
    intent: dict
    investigation: dict
    evidence: list[dict]
    draft: dict
    status: str
    reason: str


@dataclass
class WorkflowContext:
    client: object
    owner_key: str
    cancel: object = None


def fingerprint(observations):
    return [
        {
            "tool": o["tool"],
            "object_id": o["object_id"],
            "sha256": digest({"status": o["status"], "data": o.get("data")}),
        }
        for o in observations
    ]


class DraftWorkflow:
    def __init__(self, dsn, agent):
        self.dsn, self.agent, self.store = dsn, agent, DraftStore(dsn)

    def _authorized(self, context):
        identity = context.client.identity()
        if identity.owner_key != context.owner_key or "approval:propose" not in identity.scopes:
            raise AuthenticationRequired("current maker permission is required")

    def _investigate(self, state, runtime: Runtime[WorkflowContext]):
        intent = DraftIntent.parse(state["intent"])
        question = f"Investigate payout {intent.payout_id} for a draft. {state['question']}"
        result = self.agent.run(question, runtime.context.client, cancel=runtime.context.cancel)
        return {"investigation": result, "evidence": fingerprint(result["observations"])}

    def _prepare(self, state, runtime: Runtime[WorkflowContext]):
        if runtime.context.cancel is not None and runtime.context.cancel.is_set():
            return {"status": "cancelled"}
        self._authorized(runtime.context)
        intent = DraftIntent.parse(state["intent"])
        cases = [
            o["data"]
            for o in state["investigation"]["observations"]
            if o["tool"] == "investigation" and o["status"] == "ok"
        ]
        if not cases:
            return {"status": "refused", "reason": "no_authorized_investigation"}
        case = cases[0]
        observation = next(
            (
                o
                for o in case.get("recent_observations", [])
                if o.get("observation_id") == intent.observation_id
            ),
            {},
        )
        local = any(
            i.get("category") == "LOCAL_BLOCKER"
            and i.get("observation_id") == intent.observation_id
            for i in case.get("recent_incidents", [])
        )
        run = any(
            r.get("run_id") == intent.evidence_run_id
            for r in case.get("current_reconciliation_references", [])
        )
        more = any(
            case.get(k, True)
            for k in ("observations_have_more", "incidents_have_more", "reconciliations_have_more")
        )
        if (
            observation.get("state") not in ("SUCCEEDED", "FAILED", "CLOSED_ABSENT")
            or not local
            or not run
            or more
        ):
            return {"status": "refused", "reason": "incomplete_or_ineligible_evidence"}
        draft = self.store.get_or_create(
            runtime.context.owner_key, state["request_id"], intent, state["evidence"]
        )
        return {"draft": draft, "status": "awaiting_review"}

    def _review(self, state, runtime: Runtime[WorkflowContext]):
        if runtime.context.cancel is not None and runtime.context.cancel.is_set():
            return {"status": "cancelled"}
        decision = interrupt(
            {
                "kind": "review_draft",
                "draft": state["draft"],
                "notice": "Review only; submit through the existing operator console.",
            }
        )
        self._authorized(runtime.context)
        if not isinstance(decision, dict) or set(decision) != {"continue", "draft_sha256"}:
            raise WorkflowConflict("invalid review decision")
        if type(decision["continue"]) is not bool:
            raise WorkflowConflict("invalid review decision")
        if decision["draft_sha256"] != state["draft"]["draft_sha256"]:
            raise WorkflowConflict("draft digest changed")
        if not decision["continue"]:
            return {"status": "dismissed"}
        if self.store.expired(runtime.context.owner_key, state["request_id"]):
            raise WorkflowConflict("draft expired; start a new request for fresh review")
        current = [
            runtime.context.client.read(o["tool"], o["object_id"], runtime.context.owner_key)
            for o in state["evidence"]
        ]
        if fingerprint(current) != state["evidence"]:
            raise WorkflowConflict("evidence changed; fresh review is required")
        self._authorized(runtime.context)
        return {"status": "handoff_ready"}

    def _graph(self, saver):
        graph = StateGraph(WorkflowState, context_schema=WorkflowContext)
        graph.add_node("investigate", self._investigate)
        graph.add_node("prepare", self._prepare)
        graph.add_node("review", self._review)
        graph.add_edge(START, "investigate")
        graph.add_edge("investigate", "prepare")
        graph.add_conditional_edges(
            "prepare", lambda s: s["status"] == "awaiting_review", {True: "review", False: END}
        )
        graph.add_edge("review", END)
        return graph.compile(checkpointer=saver)

    def invoke(
        self,
        thread_id,
        client,
        *,
        question=None,
        request_id=None,
        intent=None,
        decision=None,
        cancel=None,
    ):
        identity = client.identity()
        context = WorkflowContext(client, identity.owner_key, cancel)
        self._authorized(context)
        key = f"{identity.owner_key}/{UUID(thread_id)}"
        config = {"configurable": {"thread_id": key}, "recursion_limit": 30}
        with self.store.lock(key), PostgresSaver.from_conn_string(self.dsn) as saver:
            graph = self._graph(saver)
            previous = graph.get_state(config)
            if decision is None:
                parsed = DraftIntent.parse(intent)
                initial = {
                    "question": question,
                    "request_id": str(UUID(request_id)),
                    "intent": asdict(parsed),
                }
                self.store.register(key, initial)
                if previous.values:
                    return dict(previous.values)
                output = graph.invoke(initial, config, context=context, durability="sync")
            else:
                if not previous.values:
                    raise WorkflowConflict("workflow is unavailable for this identity")
                if (
                    not isinstance(decision, dict)
                    or set(decision) != {"continue", "draft_sha256"}
                    or type(decision["continue"]) is not bool
                    or decision["draft_sha256"]
                    != previous.values.get("draft", {}).get("draft_sha256")
                ):
                    raise WorkflowConflict("invalid or changed review decision")
                if not previous.next:
                    accepted = previous.values.get("status") == "handoff_ready"
                    if decision["continue"] != accepted:
                        raise WorkflowConflict("review already completed with a different decision")
                    return dict(previous.values)
                output = graph.invoke(
                    Command(resume=decision), config, context=context, durability="sync"
                )
            # Interrupt objects are framework internals, never part of the product response.
            return {k: v for k, v in output.items() if k != "__interrupt__"}

    def inspect(self, thread_id, client):
        identity = client.identity()
        context = WorkflowContext(client, identity.owner_key)
        self._authorized(context)
        key = f"{identity.owner_key}/{UUID(thread_id)}"
        with PostgresSaver.from_conn_string(self.dsn) as saver:
            state = self._graph(saver).get_state({"configurable": {"thread_id": key}})
            if not state.values:
                raise WorkflowConflict("workflow is unavailable for this identity")
            return dict(state.values)
