# R2 综合验收

日期：2026-10-28。基线：`82fa8ffef3ae0c2cfe5fd5883c58ab9a73cc4595`。
范围是本地合成账本的核对、排空后重建和全额内部冲正；最终运行结果及提交身份见
[PR04 验证记录](../evidence/R2-PR04/2026-10-28/README.md)。

**结论：581 项全部通过，零失败／错误／跳过；其中268项是真实 PostgreSQL。
R2 在下述本地边界内通过验收。**

PR04 增加交错、变异和进程中断证据；生产代码、迁移、独立参考模型及冻结输入保持基线字节。

## 验收项与具体证据

测试文件中的函数名可直接用 `pytest <文件>::<函数>` 单独运行。参数化函数覆盖其声明的
全部情况。每项的最终结果以本轮完整 JUnit 为准，旧记录保留，不把历史测试次数累加。

| 契约验收项 | 具体测试与结论 |
|---|---|
| R2-CHECK | [手算完整轨迹](../tests/integration/test_reversal_admission.py)：`test_hand_frozen_rejected_funded_success_trace_and_unchanged_original`；[独立命令模型](../tests/reference/test_command_model.py)：`test_frozen_rejected_funded_new_success_trace_and_original_history_unchanged`。预先冻结命令和字面结果，逐步检查拒绝、补款、新成功、重放、累计余额及零活动账户。 |
| R2-DRIFT | [实际 SQL 有效的漂移](../tests/integration/test_export_verification.py)：`test_committed_projection_defect_is_found_without_failing_stored_request_check`。实际提交后 A 给出字段／账户差异，B 可通过。 |
| R2-OWNER | 同文件 `test_committed_wrong_merchant_matches_fold_but_saved_request_blocks_rebuild`；[应用重查](../tests/integration/test_maintenance.py)：`test_fresh_wrong_owner_source_blocks_apply_even_when_preview_was_eligible`。分录与余额一致记错商户，A 通过、B 失败，不能应用修复。 |
| R2-EXPORT | [一致快照](../tests/integration/test_export_snapshot.py)：`test_all_tables_share_one_snapshot_even_when_purchase_commits_between_reads`（含移除隔离的变异）和 `test_workspace_filter_covers_every_exported_relation`。写入发生在读取表之间，仍导出同一有效状态。 |
| R2-FILES | [包校验](../tests/unit/test_export_bundle.py)覆盖损坏、缺成员、额外成员、重复键、作用域及发布故障；[实际进程中断](../tests/integration/test_r2_export_interruption.py)：`test_killed_exporter_never_publishes_partial_bundle_or_changes_old_exports`。data 或 manifest fsync 后实际终止进程，临时包不能加载，旧完成包字节不变。 |
| R2-DRAIN | [排空及变异](../tests/integration/test_maintenance.py)：`test_freeze_waits_for_real_inflight_purchase_and_next_purchase_is_rejected`；[混合等待链](../tests/integration/test_r2_mixed.py)：`test_freeze_drains_purchase_reversal_and_original_replay_chain`。观察共享／排他锁依赖，业务提交后冻结才完成，新写入503且无新记录。 |
| R2-REPAIR | [修复](../tests/integration/test_maintenance.py)：`test_repair_restores_drift_and_missing_idle_row_with_complete_immutable_audit`、`test_apply_recomputes_current_source_instead_of_using_preview_targets`。现场计算含缺失空闲账户，版本、完整审计和 epoch 正确。 |
| R2-APPLY | 同文件 `test_repair_sql_failures_roll_back_balances_audit_and_epoch_then_same_id_recovers`、`test_ambiguous_repair_commit_recovers_with_same_repair_identity`、`test_stale_preview_target_mutation_is_rejected_by_independent_postcheck`、`test_same_repair_id_race_replays_one_atomic_effect`。余额／审计／epoch／COMMIT 故障全回滚；两种实际 COMMIT 丢失结果可按同 ID 恢复。 |
| R2-RESERVED | 同文件 `test_nonzero_reserved_blocks_apply_without_erasing_reservations`；[恢复条件修正](../tests/integration/test_resume_policy.py)：`test_nonzero_reservation_blocks_repair_but_not_resume_and_is_retained`。预留不能从分录推算或清零，阻止应用，但不阻止结构／A 正确的恢复。 |
| R2-REV-REJECT | [手算轨迹](../tests/integration/test_reversal_admission.py)：商户账面140／预留100／可用40，冲正80保存422，成功关系为空，原购买不改。 |
| R2-REV-RECOVER | 同一测试：合成开户补40，旧 R-A 仍422，新 R-B 成功；客户100／商户100／预留100，只有一条成功关系。 |
| R2-REV-RACE | [冲正竞争](../tests/integration/test_reversal_concurrency.py)：`test_original_serializes_reversals_and_identity_races_replay`。不同 ID 一201一422，相同键／业务重放，始终一条成功关系。 |
| R2-REV-MIX | [混合调度](../tests/integration/test_r2_mixed.py)：`test_purchase_and_reversal_follow_the_observed_balance_queue`、`test_freeze_drains_purchase_reversal_and_original_replay_chain`、`test_distinct_original_reversals_share_balances_without_cross_original_waits`。两种先后顺序、四方等待链、不同原购买共享余额均匹配预定结果；独立 fold／B 与原始记录检查通过。 |
| R2-REV-FAULT | [冲正故障](../tests/integration/test_reversal_faults.py)：`test_every_reversal_effect_rolls_back_after_sql_failure_and_same_id_recovers`、`test_reversal_unknown_commit_recovers_original_identity_without_duplicate_effect`、`test_reversal_uses_bounded_whole_transaction_retry_only_for_definite_aborts`。八个 SQL 阶段、两种真实 wire 结果及有限重试；意外23505不吞成业务结果。 |
| R2-AUTH | [授权](../tests/integration/test_reversal_admission.py)：`test_only_original_merchant_can_submit_or_replay_without_disclosing_result`、`test_foreign_workspace_original_is_not_eligible_even_when_id_is_known`、`test_reversal_scope_mismatch_is_rejected_before_any_claim`、`test_owned_new_original_cannot_expose_another_merchants_retained_reversal`，以及两项跨类型身份冲突测试。 |
| R2-PERM | [运行权限](../tests/integration/test_privileges.py)；[维护权限](../tests/integration/test_maintenance.py)：`test_business_and_export_credentials_cannot_forge_maintenance_metadata`、`test_actual_maintenance_credential_has_no_history_ddl_or_reservation_authority`、`test_committed_repair_audit_cannot_be_extended_with_a_later_account`；[新冲正表](../tests/integration/test_reversal_admission.py)：`test_nonbusiness_roles_cannot_create_reversal_relations`。真实角色拒绝改历史、伪造维护、写预留、DDL／提权，完整审计提交后不能追加。 |

## 交错的字面结果

初始开户100、购买80后，客户20／商户80。期望在运行 SUT 前就写定，而不是从 SUT 输出
反推。所有金额是最小货币单位；不能把100解释成实际100 PLN。

| 观察到的执行顺序 | 业务结果 | 最终客户／商户 |
|---|---|---|
| 新购买40先拿到余额锁，冲正80随后 | 购买保存422不足，冲正201 | 100／0 |
| 冲正80先拿到余额锁，新购买40随后 | 两笔201 | 60／40 |
| 新购买10、冲正80、原购买新键重放、冻结形成等待链 | 三个业务调用201，重放保持原结果；随后冻结成功 | 90／10，FROZEN |
| 两笔原购买各50，两个不同原交易的冲正共享余额 | 两个冲正201，各有独立成功关系 | 100／0 |

这些测试使用服务器实际锁等待、事件握手及 NOWAIT 探针；超时只是失败看门狗。没有用
“睡一会儿，所以一定已经发生”证明顺序。重放不拿余额锁，原购买行锁也不会绕回新的
购买业务；维护先拿排他工作区屏障，等合作写事务完成后再检查模式和来源。

## 变异证明哪些保护确实必要

[PR04 变异](../tests/integration/test_r2_mutations.py)保留同一业务／拒绝断言：

- 所有尝试的原购买 ID 都唯一：失败 R-A 占住位置，使补款后新 R-B 的201断言失败。
  确认是指定错误约束的23505且事务回滚；移除错误约束后，同一 R-B 成功。
- 只移除原购买 FOR UPDATE：两笔实际都先读到“无成功关系”。正常一201一422断言失败，
  后者触发真实成功关系主键错误；数据库仍阻止第二次效果。23505是业务协议退化证据，
  不能算作正确的 ALREADY_REVERSED。恢复正常代码后，原失败身份可保存正确422。
- 跳过维护的 B 核对：同一“错误来源必须被拒”断言失败，修复错误地应用。独立且未被
  修改的 B 仍报 WRONG_OWNER，证明仅有结构／A不够保护重建。

已有变异继续覆盖导出隔离、共享屏障和旧预览目标。SQL 阶段故障与延迟声明约束覆盖
部分余额／审计不能提交。所有注入只存在于隔离测试数据库、测试游标或局部猴子补丁。
恢复库代码和约束之后，同一身份的正常行为仍有验证；没有修改生产保护来让测试通过。

## 审查要求保留的边界

原客户账户已停用时，**新冲正返回通用404，整笔回滚，不保留请求、业务或尝试记录**；
不是一条422资金拒绝。[明确回归](../tests/integration/test_r2_review_boundary.py)：
`test_new_reversal_with_disabled_original_customer_is_404_and_retains_nothing`。
重新启用后，同一身份可以成功；已成功冲正在账户后来停用时仍能授权重放原结果。

恢复与重建的门槛不同：恢复要求结构／A通过，B与非零预留照常保存在恢复报告；应用
重建仍要求结构／B通过、预留为零，余额漂移本身是可修复对象。详见[维护说明](MAINTENANCE.md)。

本验收只能支持当前单工作区、每次至多一个原交易、合作屏障和统一账户顺序的锁图与
已观察交错内未形成循环等待，不能声称所有未来代码或任意 SQL 永不死锁。40P01／40001
有限重试、锁／语句超时与稳定身份恢复仍需要保留。管理员、直接 SQL、未来批处理、外部
付款和跨系统一致性超出此结论。数据库冻结不证明外部发送已经停止。

## 冷启动与交接

在仓库根目录安装锁定环境并运行：

```sh
make bootstrap
make check
make test
make build
```

需要 Python3.12、uv、make、Docker；首次下载固定 PostgreSQL18.6 镜像可能需要网络。
Docker 不可用时测试失败，不跳过。测试集建立独立容器和角色，每个数据库测试建立新数据库并应用迁移，结束后
清理，仓库不需要访问真实业务数据库。新机器还需具备锁文件依赖和镜像下载条件。

独立安装检查强制重新构建当前包、导入冲正／维护接口和五份迁移；旧 v1／v2／v3冻结包
可被新安装的核对器接受。用户可从[入口](../README.md)、[当前交接](../HANDOFF.md)、
[导出](EXPORT-AND-VERIFY.md)、[维护](MAINTENANCE.md)、[冲正](REVERSAL.md)接着工作。

这是仓库工作流和全新临时数据库验收；不代表已建立常驻演示数据库、完成发布源码包的
全部发行验收、部署生产服务或接入认证。`make demo` 与三道自测题可以后续单独决定。
实现与验证完成也不等于个人已能独立解释或修改；个人掌握仍未评估。
