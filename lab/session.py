"""One learner, one disposable PostgreSQL database, and real ledger operations.

The HTTP harness creates the database and owns its lifetime. This adapter accepts no
DSN or SQL from the browser. Owner SQL only seeds synthetic accounts or deliberately
damages one projection; financial actions call the existing runtime interfaces.
"""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from ledgerops.db.admission import AdmissionError
from ledgerops.db.maintenance import (
    MaintenanceError,
    apply_rebuild,
    freeze_workspace,
    preview_rebuild,
    resume_workspace,
)
from ledgerops.domain import AmountMinor
from ledgerops.export import capture_snapshot, export_workspace, load_export
from ledgerops.verification import verify_snapshot
from tests.integration.helpers import post_fixture
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.reversal_helpers import reversal_input, reverse

_STEPS = [
    ("opening", "开户与合成入金", "创建四个账户，用开户分录给客户记入100。"),
    ("purchase", "购买80", "客户100→20，M1由0→80；新增两行分录。"),
    ("purchase", "余额不足", "再购买80得到422；拒绝被保存，余额不变。"),
    ("reversal", "商户发起全额冲正", "新增反向分录，客户20→100，原分录不改。"),
    ("duplicate_reversal", "新的重复冲正", "新业务ID再次冲正，保存422 ALREADY_REVERSED。"),
    ("drift", "注入余额漂移", "仅把客户投影加20；借贷仍平衡不能证明余额正确。"),
    ("verify", "导出并离线核对", "A发现120与分录重算100不符，B检查业务对象。"),
    ("freeze", "冻结并排空写事务", "维护排他屏障等待协作写事务结束，再置为FROZEN。"),
    ("preview", "预览重建", "查看120→100差异；预览文件不是应用数据源。"),
    ("rebuild", "事务内现场重建", "从分录重新计算，保存审计与epoch，仍保持冻结。"),
    ("resume", "重新核对后恢复", "现场结构与A通过后恢复NORMAL；B与预留写入报告。"),
    ("verify", "再次离线核对", "重新导出，确认结构、A、B通过。"),
]
_TITLES = {action: title for action, title, _ in _STEPS}
_TITLES.update(
    replay_purchase="原身份重放购买",
    replay_reversal="原身份重放冲正",
    conflict_purchase="同一请求键换金额",
)
_ROLES = {
    "opening": "owner（仅建合成账户） → app（开户分录）",
    "drift": "owner（仅此临时库的故障注入）",
    "verify": "reader（导出） → 无数据库连接（离线核对）",
    "freeze": "maintenance",
    "preview": "maintenance",
    "rebuild": "maintenance",
    "resume": "maintenance",
    "reset": "owner（创建新的隔离工作区）",
}


class LabError(ValueError):
    """A safe, explicit local learner action error, without connection details."""

    def __init__(self, code, message, http_status=400):
        self.code, self.message, self.http_status = code, message, http_status
        super().__init__(message)


class LabSession:
    def __init__(self, db, golden, artifact_root, code_revision, dirty=False):
        self.db, self._base = db, deepcopy(golden)
        self.artifact_root = Path(artifact_root)
        self.code_revision, self.dirty = code_revision, dirty
        self.lock = RLock()
        self._reset()

    @property
    def workspace_id(self):
        return self.golden["workspace"]["id"]

    def _reset(self):
        # Composite identities permit a fresh workspace without deleting history.
        self.golden = deepcopy(self._base)
        workspace = str(uuid4())

        def replace(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "workspace_id":
                        value[key] = workspace
                    else:
                        replace(item)
            elif isinstance(value, list):
                for item in value:
                    replace(item)

        replace(self.golden)
        self.golden["workspace"]["id"] = workspace
        with self.db.connect("owner") as conn:
            conn.execute(
                "INSERT INTO ledger.workspace (id,currency) VALUES (%s,%s)",
                (workspace, self.golden["workspace"]["currency"]),
            )
        self.opened = False
        self.events, self.guide_index, self.customized = [], 0, False
        self.verification, self.preview = None, None
        self.last_purchase, self.last_reversal, self.successful_purchase = None, None, None
        self.freeze_id = None
        self.drifted = False
        self._revision = 0

    def _snapshot(self):
        suspended = self.db.suspend() if hasattr(self.db, "suspend") else nullcontext()
        with suspended, self.db.connect("reader") as conn:
            snapshot = capture_snapshot(conn, self.workspace_id)
        labels = {row["id"]: row["label"] for row in self.golden["accounts"]}
        for account in snapshot["accounts"]:
            account["label"] = labels[account["id"]]
        for balance in snapshot["balances"]:
            balance["label"] = labels[balance["account_id"]]
            balance["available"] = str(int(balance["booked"]) - int(balance["reserved"]))
        return snapshot

    def state(self):
        with self.lock:
            snapshot = self._snapshot()
            snapshot.update(
                guide={
                    "index": self.guide_index,
                    "total": len(_STEPS),
                    "steps": [
                        dict(number=i + 1, action=a, title=t, summary=s)
                        for i, (a, t, s) in enumerate(_STEPS)
                    ],
                    "next_action": (
                        _STEPS[self.guide_index][0] if self.guide_index < len(_STEPS) else None
                    ),
                    "customized": self.customized,
                },
                events=deepcopy(self.events),
                last_event=deepcopy(self.events[-1]) if self.events else None,
                verification=self._evidence(self.verification),
                preview=self._evidence(self.preview),
                lab={
                    "actual_postgres": True,
                    "ephemeral": True,
                    "account_opened": self.opened,
                    "seeded_identity": True,
                    "code_revision": self.code_revision,
                    "dirty": self.dirty,
                },
            )
            return snapshot

    def _evidence(self, value):
        if value is None:
            return None
        result = deepcopy(value)
        result["stale"] = result.pop("at_revision") != self._revision
        return result

    def act(self, action, body=None):
        body = {} if body is None else body
        with self.lock:
            guided = action == "next"
            if guided:
                if self.customized or self.guide_index == len(_STEPS):
                    raise LabError("INVALID_STAGE", "导览已完成或已自由操作；重置可重新开始。", 409)
                action = _STEPS[self.guide_index][0]
                body = {"amount_minor": "80", "merchant": "M1"}
            before = self._snapshot()
            self._pending_details = {}
            recording = self.db.record() if hasattr(self.db, "record") else nullcontext([])
            trace = []
            try:
                with recording as trace:
                    if action == "reset":
                        self._reset()
                        result, status, details = {"workspace_id": self.workspace_id}, None, {}
                    else:
                        result, status, details = self._dispatch(action, body)
            except AdmissionError as exc:
                self._event(
                    action,
                    before,
                    {"error": {"code": exc.code}},
                    exc.http_status,
                    trace=trace,
                    **self._pending_details,
                )
                raise LabError(exc.code, "账本拒绝此次请求：" + exc.code, exc.http_status) from exc
            except MaintenanceError as exc:
                self._event(
                    action,
                    before,
                    {"error": {"code": exc.code}, "report": exc.report},
                    409,
                    trace=trace,
                )
                raise LabError(exc.code, "维护前提未满足：" + exc.code, 409) from exc
            except (TypeError, ValueError) as exc:
                error = (
                    exc
                    if isinstance(exc, LabError)
                    else LabError("INVALID_INPUT", "输入不符合账本契约：" + str(exc))
                )
                self._event(
                    action,
                    before,
                    {"error": {"code": error.code}},
                    error.http_status,
                    trace=trace,
                    **self._pending_details,
                )
                raise error from exc if error is not exc else None
            except Exception:
                self._event(
                    action,
                    before,
                    {"error": {"code": "INTERNAL_ERROR"}},
                    500,
                    trace=trace,
                    **self._pending_details,
                )
                raise
            self._event(
                action,
                before,
                result,
                status,
                trace=trace,
                guide_step=self.guide_index + 1 if guided else None,
                **details,
            )
            if guided:
                self._check_expected(action, result, status)
                self.guide_index += 1
            elif action not in {"verify", "preview", "reset"}:
                self.customized = True
            return self.state()

    def _event(self, action, before, result, status, **details):
        after = self._snapshot()
        self.events.append(
            {
                "id": len(self.events) + 1,
                "action": action,
                "title": (
                    "购买成功"
                    if action == "purchase" and status == 201
                    else "购买被拒绝"
                    if action == "purchase" and status == 422
                    else _TITLES.get(action, "重置学习工作区")
                ),
                "at": datetime.now(UTC).isoformat(),
                "http_status": status,
                "result": result,
                "role": _ROLES.get(action, "app"),
                "before": before["balances"],
                "after": after["balances"],
                "before_state": before,
                "after_state": after,
                **details,
            }
        )

    def _check_expected(self, action, result, status):
        expected = {1: 201, 2: 422, 3: 201, 4: 422}.get(self.guide_index)
        if expected is not None and status != expected:
            self.customized = True
            raise LabError("INVALID_STAGE", "实际结果与导览预期不同，请查看账本或重置。", 409)
        expected_code = {2: "INSUFFICIENT_AVAILABLE", 4: "ALREADY_REVERSED"}.get(self.guide_index)
        if expected_code and result.get("error", {}).get("code") != expected_code:
            self.customized = True
            raise LabError("INVALID_STAGE", "实际业务拒绝原因与当前导览阶段不同。", 409)
        if action == "verify":
            report = result["report"]
            healthy = self.guide_index == 11
            if (
                report["projection_ok"] != healthy
                or not report["structure_ok"]
                or not report["request_intent_ok"]
            ):
                self.customized = True
                raise LabError("INVALID_STAGE", "实际核对结果与当前导览阶段不同。", 409)

    def _changed(self):
        self._revision += 1

    def _dispatch(self, action, body):
        if action == "opening":
            return self._opening(), None, {}
        if not self.opened:
            raise LabError("OPENING_REQUIRED", "先开户并记入合成资金。", 409)
        if action in {
            "purchase",
            "replay_purchase",
            "conflict_purchase",
            "reversal",
            "replay_reversal",
            "duplicate_reversal",
        }:
            return self._admit(action, body)
        if action == "drift":
            return self._drift(), None, {}
        if action == "verify":
            with self.db.connect("reader") as conn:
                path = export_workspace(
                    conn,
                    self.workspace_id,
                    self.artifact_root / "exports",
                    code_revision=self.code_revision,
                    dirty=self.dirty,
                )
            # Deliberately close the connection before calling the offline checker.
            report = verify_snapshot(load_export(path)).as_dict()
            self.verification = {
                "report": report,
                "export_id": path.name,
                "at_revision": self._revision,
            }
            return {"report": report, "export_id": path.name}, None, {}
        if action in {"freeze", "preview", "rebuild", "resume"}:
            return self._maintain(action), None, {}
        raise LabError("UNKNOWN_ACTION", "没有这个学习操作。")

    def _opening(self):
        if self.opened:
            raise LabError("ALREADY_OPENED", "账户已开户；重置会创建新的隔离工作区。", 409)
        # Existing fixture data, with only account seeding adapted for the already
        # created empty workspace. OPENING remains the existing synthetic helper.
        with self.db.connect("owner") as conn, conn.transaction():
            for account in self.golden["accounts"]:
                conn.execute(
                    "INSERT INTO ledger.account "
                    "(workspace_id,id,owner_id,class,normal_side,currency) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    tuple(
                        account[k]
                        for k in (
                            "workspace_id",
                            "id",
                            "owner_id",
                            "class",
                            "normal_side",
                            "currency",
                        )
                    ),
                )
                conn.execute(
                    "INSERT INTO ledger.account_balance (workspace_id,account_id,normal_side) "
                    "VALUES (%s,%s,%s)",
                    (self.workspace_id, account["id"], account["normal_side"]),
                )
        journal = post_fixture(self.db, self.golden, "OPENING")
        journal = {**journal, "total_minor": str(journal["total_minor"])}
        self.opened = True
        self._changed()
        return {"synthetic_fixture": True, "opening_amount_minor": "100", "journal": journal}

    def _admit(self, action, body):
        if action == "purchase":
            merchant = body.get("merchant", "M1")
            if merchant not in {"M1", "M2"}:
                raise LabError("INVALID_INPUT", "商户只能选择M1或M2。")
            account = next(a for a in self.golden["accounts"] if a["label"] == merchant)
            arguments = purchase_input(
                self.golden,
                operation_id=str(uuid4()),
                key="learn-" + str(uuid4()),
                amount_minor=body.get("amount_minor", "80"),
                merchant_account_id=account["id"],
            )
            self.last_purchase = arguments
        elif action in {"replay_purchase", "conflict_purchase"}:
            arguments = self.last_purchase
            if arguments is None:
                raise LabError("PURCHASE_REQUIRED", "先提交一次购买，才能重放原请求。", 409)
            if action == "conflict_purchase":
                principal, command, key = arguments
                changed = body.get("amount_minor", str(command.amount_minor.value + 1))
                arguments = (
                    principal,
                    replace(command, amount_minor=AmountMinor.parse(changed)),
                    key,
                )
        elif action == "replay_reversal":
            arguments = self.last_reversal
            if arguments is None:
                raise LabError("REVERSAL_REQUIRED", "先提交一次冲正，才能重放原请求。", 409)
        else:
            if self.successful_purchase is None:
                raise LabError("PURCHASE_REQUIRED", "先完成一笔成功购买。", 409)
            original = self.successful_purchase[1]
            merchant = next(
                a for a in self.golden["accounts"] if a["id"] == str(original.merchant_account_id)
            )
            arguments = reversal_input(
                self.golden,
                operation_id=str(uuid4()),
                key="learn-" + str(uuid4()),
                original_operation_id=str(original.operation_id),
                actor_id=merchant["owner_id"],
            )
            self.last_reversal = arguments
        _, command, key = arguments
        details = {
            "request": {
                "operation_id": str(command.operation_id),
                "key": key.key,
                "route_family": key.route_family,
                "canonical_payload": command.canonical_bytes.decode("utf-8"),
            }
        }
        self._pending_details = details
        purchase = action in {"purchase", "replay_purchase", "conflict_purchase"}
        result = invoke(self.db, *arguments) if purchase else reverse(self.db, *arguments)
        if purchase and result.http_status == 201:
            self.successful_purchase = arguments
        if not result.replayed:
            self._changed()
        return (
            result.body,
            result.http_status,
            {
                "replayed": result.replayed,
                **details,
            },
        )

    def _drift(self):
        if self.drifted:
            raise LabError(
                "DRIFT_ALREADY_INJECTED", "本轮已注入一次漂移；请核对、修复或重置。", 409
            )
        snapshot = self._snapshot()
        if snapshot["workspace"]["mode"] != "NORMAL" or not verify_snapshot(snapshot).healthy:
            raise LabError("CLEAN_NORMAL_REQUIRED", "先确认工作区处于NORMAL且账本健康。", 409)
        with self.db.connect("owner") as conn:
            before, after = conn.execute(
                "UPDATE ledger.account_balance SET credits=credits+20 "
                "WHERE workspace_id=%s AND account_id=%s RETURNING booked-20,booked",
                (self.workspace_id, self.golden["purchase_request"]["customer_account_id"]),
            ).fetchone()
        self.drifted = True
        self._changed()
        return {
            "before": str(before),
            "after": str(after),
            "journal_unchanged": True,
            "synthetic_fault": True,
        }

    def _maintain(self, action):
        actor = self.golden["actors"][0]["id"]
        with self.db.connect("maintenance") as conn:
            if action == "freeze":
                freeze_id = str(uuid4())
                result = freeze_workspace(
                    conn, self.workspace_id, freeze_id, actor, "local learning: inspect projection"
                )
                self.freeze_id = freeze_id
            else:
                if self.freeze_id is None:
                    raise LabError("FREEZE_REQUIRED", "先冻结并排空写事务。", 409)
                if action == "preview":
                    result = preview_rebuild(
                        conn,
                        self.workspace_id,
                        self.freeze_id,
                        self.artifact_root / "previews",
                        code_revision=self.code_revision,
                        dirty=self.dirty,
                    )
                    result = {
                        "changes": result["changes"],
                        "report": result["report"],
                        "export_id": Path(result["export_path"]).name,
                    }
                    self.preview = {**result, "at_revision": self._revision}
                    return result
                if action == "rebuild":
                    if self.preview is None:
                        raise LabError(
                            "PREVIEW_REQUIRED", "先生成差异预览；应用仍会现场重算。", 409
                        )
                    result = apply_rebuild(
                        conn,
                        self.workspace_id,
                        self.freeze_id,
                        str(uuid4()),
                        actor,
                        "local learning: fresh projection rebuild",
                        self.preview["export_id"],
                    )
                else:
                    result = resume_workspace(
                        conn,
                        self.workspace_id,
                        self.freeze_id,
                        actor,
                        "local learning: fresh check and resume",
                    )
        self._changed()
        return result
