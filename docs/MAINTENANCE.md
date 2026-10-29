# 冻结、核对、重建与恢复（含 R3 活动预留）

大型工作区会自动转入完整磁盘核验，旧小数据路径保持不变。核验在已经获得排他屏障的
READ COMMITTED事务内执行，避免锁前固定的旧快照；修复仍现场重算并在同一事务中独立后验。
新报告v4和预览标明该捕获上下文，B失败仍阻止重建、但不阻止结构/A通过后的恢复。
详见[大型维护与资源边界](MAINTENANCE-CAPACITY.md)。

已实现的本地维护流程：冻结并排空合作写事务 → 导出、查看差异 → 排他事务内现场重算
并应用 → 再导出核对 → 显式恢复。没有候选余额表，也没有从预览文件导入余额的接口。
数据库冻结不代表外部付款停止；R3 的在途模拟支付商调用可能继续执行。冻结期间不接下新的准入、派发 claim 或观测落库，恢复后须继续查证外部事实。

## 准备与身份

在已有 R1 数据库上，先由本地管理员建立独立角色，再使用既有迁移连接应用 003：

```sql
CREATE ROLE ledger_maintenance LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;
\password ledger_maintenance
```

迁移 004 追加恢复报告字段；已应用的 003 不改。

角色没有 app/migrator 成员资格、历史账务写权限或 DDL。迁移 006 为活动 hold 重建增加 reserved 的投影写权限，不能据此修改 hold 或其事件。
它可以修改模式、余额投影和维护元数据，因此仍须通过 Python 流程正确操作。
直接拿维护凭据执行任意 SQL 不会自动遵守屏障和核对规则。

```sh
# 私下设置 LEDGEROPS_MIGRATION_DSN；不要将密码写入 Git 或证据日志。
PYTHONPATH=src uv run --locked python -m ledgerops.db.migrations
```

迁移器不创建角色或密码。迁移 006 起，新数据库需事先准备 migrator、app、reader、maintenance、worker 五个
角色。worker 的创建与边界见[提现说明](PAYOUTS.md)。实际业务工作区与数据也须已初始化；本任务未创建常驻本地数据库。

维护命令使用 `LEDGEROPS_MAINTENANCE_DSN`，普通导出仍用 reader 的
`LEDGEROPS_EXPORT_DSN`。actor UUID 来自受信调用边界，仅作为审计身份，不代替认证。

首次操作前固定并保存以下值，之后重试必须复用：工作区 `LEDGEROPS_WORKSPACE_ID`、
本次冻结 `LEDGEROPS_FREEZE_ID`、本次修复 `LEDGEROPS_REPAIR_ID`、操作人
`LEDGEROPS_ACTOR_ID`。每个都是规范 UUID。原因文本原样保留，不会去空格或归一化。

## 顺序执行

在仓库根目录运行。冻结会等待持有该工作区共享屏障的在途购买完成，再提交 FROZEN：

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli freeze \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --freeze-id "$LEDGEROPS_FREEZE_ID" \
    --actor "$LEDGEROPS_ACTOR_ID" --reason "核对余额漂移"
```

之后新购买（包括需要写新请求关联的重放）返回 503，且不保留新业务结果。冻结失败不会
伪造冻结成功；锁超时或取消后，用原身份确认结果。RESTORE 不能当作普通冻结处理。

查看预览；可将 stdout JSON 重定向到自己的 `var/preview-report.json` 保存：

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli preview-rebuild \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --freeze-id "$LEDGEROPS_FREEZE_ID" \
    --output-root ./var/exports
```

预览返回 `export_path`、`report`、`changes`。changes 包含全部账户的 before/after，
包括零活动和未改变金额的账户；版本字段展示下一次修复会写入的版本。保存导出目录末尾
的 UUID 为 `LEDGEROPS_PREVIEW_EXPORT_ID`。它是后续审计的引用，不是可执行余额数据。

v5 起，结构（含合法 hold 生命周期）和 B 通过时，余额漂移可以修复：借贷合计来自 journal，reserved 来自不可变 HELD 事件对应的活动 hold。B 失败不能用重建掩盖；缺失或非法的 hold 源记录同样阻止应用。旧 v1–v4 没有 hold 历史，保留原先非零预留不支持重建的边界。

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli apply-rebuild \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --freeze-id "$LEDGEROPS_FREEZE_ID" \
    --repair-id "$LEDGEROPS_REPAIR_ID" --preview-export-id "$LEDGEROPS_PREVIEW_EXPORT_ID" \
    --actor "$LEDGEROPS_ACTOR_ID" --reason "从分录重建余额"
```

应用时重新取得排他屏障，检查当前冻结身份，重新读取并核对账本，再用 SQL 独立聚合
全部账户。更新余额、完整 before/after 审计、账户版本和 projection epoch，在一个事务
里全成或全败。应用没有目标余额参数，也不读取旧预览计算结果。preview ID 由调用者
声明，工具不认证它确实指向已读报告。成功后仍为 FROZEN。

现有账户 version 加 1；缺失余额行从 1 开始；epoch 加 1。booked 仍是数据库生成列。
源分录、业务结果、hold 和事件不改；reserved 是会被修复的投影。修复日志包含全部账户及 reserved 的前后值，提交后不能增删改审计行。历史 fold_version=1 仍要求预留为零，新修复使用 fold_version=2。

重新导出，用返回的新路径进行离线核对（详见[导出说明](EXPORT-AND-VERIFY.md)）：

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli export \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --output-root ./var/exports
PYTHONPATH=src uv run --locked python -m ledgerops.cli verify "$LEDGEROPS_POST_EXPORT_DIR"
```

确认结果后，显式恢复。resume 会在排他事务内再次现场核对，不信任旧报告：

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli resume \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --freeze-id "$LEDGEROPS_FREEZE_ID" \
    --actor "$LEDGEROPS_ACTOR_ID" --reason "应用后核对通过"
```

健康账本也可冻结、核对后直接恢复，无需做无意义的修复。恢复只要求结构正确与余额核对 A
通过；B 不通过时，完整报告仍写入不可再改的 `resume_report`，不会被恢复改成通过。合法非零 hold 不阻止恢复；v5 的 reserved 若与权威 hold 不符，就是 A 失败，须先重建。应用重建要求结构和 B 通过，允许合法活动 hold。
服务能证明现场核对通过，不能证明操作人读过预览或另存了导出报告。

## 失败和重试

- 相同修复 ID、相同字段会返回原修复元数据，不会再次增加余额版本或 epoch。字段不同
  报 `IDENTITY_CONFLICT`。COMMIT 断线时不能推断失败；重新连接并复用同一 ID。
- 已关闭冻结／恢复身份的重放只返回历史元数据，不会操作新冻结会话。成功读回历史结果
  不表示工作区现在一定是 NORMAL；当前状态应另查新快照。
- 新操作必须使用当前 freeze ID。其他工作区的、上一轮的身份都不能作用于当前会话。
- 业务前置条件不满足时，CLI 返回退出码 2 和结构化错误，例如 `INVALID_SOURCE`、
  `RESERVED_UNSUPPORTED`、`FREEZE_CONFLICT`、`PROJECTION_DRIFT`。preview 命令运行
  成功可返回 0，同时报告漂移，是否可应用要看报告字段。verify 发现不一致返回 1。
- SQL、约束、锁超时、取消和连接错误不会内部自动重试。数据库事务原子回滚；连接错误
  可能结果未知，由稳定 ID 解决。有限故障测试不证明任意故障都已穷尽。

## 旧包兼容与范围

新数据库导出格式 v5（迁移 001–006）增加提现、hold、意图、尝试及审计 fold_version；v4 冲正和 v3 恢复报告契约继续保留。
旧 v1（001/002）、v2（001–003）、v3（001–004）、v4（001–005）数据继续
按原规则加载、核对，冻结的 PR01 导出包有独立回归。未知新版本仍拒绝。
源码命令自动记录 Git 版本与 dirty；安装包预览需显式 `--code-revision`，同导出命令。
文件发布和防护沿用 PR01，不添加另一套文件协议。

[R2-PR03 冲正](REVERSAL.md)已实现；冲正后的完整历史同样参与现场核对和重建。
测试中的合成开户／预留不是公共业务接口。

升级前如果旧库 reserved 非零却没有权威 hold 历史，006 明确拒绝；应先调查旧值来源，不能由迁移自动清零或伪造历史。

007 起的跨工作区支付商冲突属于全局诊断：另一个工作区新收到的冲突可能在本工作区
冻结期间出现。排他屏障固定本工作区 journal/hold 金融来源，不能冻结所有支付商证据；
维护报告中的关联诊断反映当次读取，`source_witness` 不证明这些跨工作区诊断拥有同一快照。
普通只读导出仍在独立的 REPEATABLE READ 事务中取得一致快照。关联诊断不作为重建金额来源。
