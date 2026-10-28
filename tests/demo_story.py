"""Explicit make-demo story; excluded from normal test_*.py discovery.

The ordinary smoke test invokes this file in a child pytest run, reusing the exact
disposable PostgreSQL/role/database fixtures. No permanent database is accepted.
"""

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


def test_demo_story(db, golden):
    """Execute real operations and check each fact before narrating it."""
    workspace = golden["workspace"]["id"]
    customer_id = golden["purchase_request"]["customer_account_id"]
    revision, dirty = _revision()
    seed_accounts(db, golden)
    post_fixture(db, golden, "OPENING")
    assert_balances(db, golden, [100, 100, 0, 0])
    print("[01] 开户：客户100、商户0（合成夹具；金额均为最小货币单位）。", flush=True)

    bought = invoke(db, *purchase_input(golden))
    assert bought.http_status == 201 and not bought.replayed
    assert_balances(db, golden, [100, 20, 80, 0])
    print("[02] 购买80成功：201，客户20、商户80。", flush=True)

    refused = invoke(
        db, *purchase_input(golden, operation_id=str(uuid4()), key="demo-insufficient-80")
    )
    assert refused.http_status == 422
    assert refused.body["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    assert_balances(db, golden, [100, 20, 80, 0])
    print("[03] 再购买80被拒：422 INSUFFICIENT_AVAILABLE，余额不变。", flush=True)

    reversed_result = reverse(db, *reversal_input(golden, key="demo-reverse-80"))
    assert reversed_result.http_status == 201 and not reversed_result.replayed
    assert_balances(db, golden, [100, 100, 0, 0])
    print("[04] 原商户冲正80成功：201，客户100、商户0。", flush=True)

    duplicate = reverse(db, *reversal_input(golden, key="demo-duplicate-reversal"))
    assert duplicate.http_status == 422
    assert duplicate.body["error"]["code"] == "ALREADY_REVERSED"
    assert_balances(db, golden, [100, 100, 0, 0])
    print("[05] 用新业务ID重复冲正：422 ALREADY_REVERSED，未再次扣款。", flush=True)

    # Intentional demo-only corruption, inside this fresh disposable database.
    with db.connect("owner") as owner:
        row = owner.execute(
            "UPDATE ledger.account_balance SET credits=credits+20 "
            "WHERE workspace_id=%s AND account_id=%s RETURNING booked",
            (workspace, customer_id),
        ).fetchone()
        assert row == (120,)
    print("[06] 人为制造漂移：客户余额投影由100改为120，分录不改。", flush=True)

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
        print("[07] 导出并离线核对：A失败、B通过，发现客户余额漂移。", flush=True)

        freeze_id, repair_id, actor_id = (str(uuid4()) for _ in range(3))
        with db.connect("maintenance") as maintenance:
            freeze_workspace(maintenance, workspace, freeze_id, actor_id, "demo inspect drift")
            assert maintenance.execute(
                "SELECT mode FROM ledger.workspace WHERE id=%s", (workspace,)
            ).fetchone() == ("FROZEN",)
            print("[08] 冻结并排空写事务：工作区FROZEN。", flush=True)

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
            print("[09] 预览重建差异：客户120→100。", flush=True)

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
            print("[10] 现场重算并重建：客户100，审计已保存，epoch=2，仍为FROZEN。", flush=True)

            resumed = resume_workspace(
                maintenance, workspace, freeze_id, actor_id, "demo fresh check and resume"
            )
            assert resumed["resume_report"]["healthy"]
            assert maintenance.execute(
                "SELECT mode FROM ledger.workspace WHERE id=%s", (workspace,)
            ).fetchone() == ("NORMAL",)
            print("[11] 再次现场核对并恢复：工作区NORMAL。", flush=True)

        with db.connect("reader") as reader:
            final = export_workspace(
                reader, workspace, root / "after", code_revision=revision, dirty=dirty
            )
        healthy = verify_snapshot(load_export(final))
        assert healthy.healthy and healthy.reservation_supported and not healthy.issues
        print("[12] 再导出并离线核对：A、B均通过，结果健康。", flush=True)
