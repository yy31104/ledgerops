"""Explicit make-demo story; excluded from normal test_*.py discovery.

The ordinary smoke test invokes this file in a child pytest run, reusing the exact
disposable PostgreSQL/role/database fixtures. No permanent database is accepted.
"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from ledgerops.cli import _revision
from ledgerops.db.maintenance import (
    apply_rebuild,
    freeze_workspace,
    preview_rebuild,
    resume_workspace,
)
from ledgerops.export import export_workspace, load_export
from ledgerops.verification import verify_snapshot
from tests.integration.helpers import post_fixture, seed_accounts
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.reversal_helpers import assert_balances, reversal_input, reverse

pytestmark = pytest.mark.integration


def _narrate(step, english, chinese):
    language = os.environ.get("LEDGEROPS_DEMO_LANG", "en")
    if language not in {"en", "zh"}:
        raise ValueError("LEDGEROPS_DEMO_LANG must be en or zh")
    print(f"[{step:02}] {chinese if language == 'zh' else english}", flush=True)


def test_demo_story(db, golden):
    """Execute real operations and check each fact before narrating it."""
    workspace = golden["workspace"]["id"]
    customer_id = golden["purchase_request"]["customer_account_id"]
    revision, dirty = _revision()
    seed_accounts(db, golden)
    post_fixture(db, golden, "OPENING")
    assert_balances(db, golden, [100, 100, 0, 0])
    _narrate(
        1,
        "Open accounts: customer 100, merchant 0 (synthetic funds; all amounts are minor units).",
        "开户：客户100、商户0（合成夹具；金额均为最小货币单位）。",
    )

    bought = invoke(db, *purchase_input(golden))
    assert bought.http_status == 201 and not bought.replayed
    assert_balances(db, golden, [100, 20, 80, 0])
    _narrate(
        2,
        "Purchase 80 succeeds: 201; customer 20, merchant 80.",
        "购买80成功：201，客户20、商户80。",
    )

    refused = invoke(
        db, *purchase_input(golden, operation_id=str(uuid4()), key="demo-insufficient-80")
    )
    assert refused.http_status == 422
    assert refused.body["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    assert_balances(db, golden, [100, 20, 80, 0])
    _narrate(
        3,
        "Another purchase of 80 is refused: 422 INSUFFICIENT_AVAILABLE; balances unchanged.",
        "再购买80被拒：422 INSUFFICIENT_AVAILABLE，余额不变。",
    )

    reversed_result = reverse(db, *reversal_input(golden, key="demo-reverse-80"))
    assert reversed_result.http_status == 201 and not reversed_result.replayed
    assert_balances(db, golden, [100, 100, 0, 0])
    _narrate(
        4,
        "The original merchant reverses 80: 201; customer 100, merchant 0.",
        "原商户冲正80成功：201，客户100、商户0。",
    )

    duplicate = reverse(db, *reversal_input(golden, key="demo-duplicate-reversal"))
    assert duplicate.http_status == 422
    assert duplicate.body["error"]["code"] == "ALREADY_REVERSED"
    assert_balances(db, golden, [100, 100, 0, 0])
    _narrate(
        5,
        "Repeat reversal with a new business ID: 422 ALREADY_REVERSED; no second debit.",
        "用新业务ID重复冲正：422 ALREADY_REVERSED，未再次扣款。",
    )

    # Intentional demo-only corruption, inside this fresh disposable database.
    with db.connect("owner") as owner:
        row = owner.execute(
            "UPDATE ledger.account_balance SET credits=credits+20 "
            "WHERE workspace_id=%s AND account_id=%s RETURNING booked",
            (workspace, customer_id),
        ).fetchone()
        assert row == (120,)
    _narrate(
        6,
        "Inject projection drift: customer balance 100 -> 120; journal entries unchanged.",
        "人为制造漂移：客户余额投影由100改为120，分录不改。",
    )

    with TemporaryDirectory(prefix="ledgerops-demo-") as directory:
        root = Path(directory)
        with db.connect("reader") as reader:
            damaged = export_workspace(
                reader, workspace, root / "before", code_revision=revision, dirty=dirty
            )
        # This verifier has no connection: it reads only the completed local files.
        report = verify_snapshot(load_export(damaged))
        assert report.structure_ok and report.request_intent_ok
        assert not report.projection_ok and report.rebuild_eligible
        assert any(issue.code == "PROJECTION_DRIFT" for issue in report.issues)
        _narrate(
            7,
            "Export and check offline: balance check A fails, "
            "request check B passes; drift detected.",
            "导出并离线核对：A失败、B通过，发现客户余额漂移。",
        )

        freeze_id, repair_id, actor_id = (str(uuid4()) for _ in range(3))
        with db.connect("maintenance") as maintenance:
            freeze_workspace(maintenance, workspace, freeze_id, actor_id, "demo inspect drift")
            assert maintenance.execute(
                "SELECT mode FROM ledger.workspace WHERE id=%s", (workspace,)
            ).fetchone() == ("FROZEN",)
            _narrate(
                8, "Freeze and drain writers: workspace FROZEN.", "冻结并排空写事务：工作区FROZEN。"
            )

            preview = preview_rebuild(
                maintenance,
                workspace,
                freeze_id,
                root / "preview",
                code_revision=revision,
                dirty=dirty,
            )
            change = next(c for c in preview["changes"] if c["account_id"] == customer_id)
            assert preview["report"]["rebuild_eligible"]
            assert change["before"]["booked"] == "120" and change["after"]["booked"] == "100"
            _narrate(9, "Preview rebuild: customer 120 -> 100.", "预览重建差异：客户120→100。")

            repaired = apply_rebuild(
                maintenance,
                workspace,
                freeze_id,
                repair_id,
                actor_id,
                "demo rebuild projection",
                Path(preview["export_path"]).name,
            )
            assert repaired["old_epoch"] == 1 and repaired["new_epoch"] == 2
            assert maintenance.execute(
                "SELECT mode,projection_epoch FROM ledger.workspace WHERE id=%s", (workspace,)
            ).fetchone() == ("FROZEN", 2)
            assert_balances(db, golden, [100, 100, 0, 0])
            _narrate(
                10,
                "Recompute and rebuild: customer 100; audit saved, "
                "projection version 2; still FROZEN.",
                "现场重算并重建：客户100，审计已保存，epoch=2，仍为FROZEN。",
            )

            resumed = resume_workspace(
                maintenance, workspace, freeze_id, actor_id, "demo fresh check and resume"
            )
            assert resumed["resume_report"]["healthy"]
            assert maintenance.execute(
                "SELECT mode FROM ledger.workspace WHERE id=%s", (workspace,)
            ).fetchone() == ("NORMAL",)
            _narrate(
                11,
                "Check live state again and resume: workspace NORMAL.",
                "再次现场核对并恢复：工作区NORMAL。",
            )

        with db.connect("reader") as reader:
            final = export_workspace(
                reader, workspace, root / "after", code_revision=revision, dirty=dirty
            )
        healthy = verify_snapshot(load_export(final))
        assert healthy.healthy and healthy.reservation_supported and not healthy.issues
        _narrate(
            12,
            "Export and check offline again: A and B pass; healthy result.",
            "再导出并离线核对：A、B均通过，结果健康。",
        )
