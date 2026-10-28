# R2-PR03：原商户发起全额冲正

冲正是一笔新业务、新分录：原购买“借客户80／贷商户80”，冲正“借商户80／贷客户80”。
原购买仍是成功，原分录、表头和响应都不改。这里只处理平台内部购买的全额回退，不涉及
支付渠道退款、部分退款、客户单方面扣商户余额或运维审批。

## Python 入口

接口与购买一致：空闲 autocommit 的 psycopg 连接、受信 Principal、类型化命令和请求键。
金额、币种和两个账户都从已成功的原购买核实，不允许调用者另填。

```python
from ledgerops.db.admission import Principal, submit_reversal
from ledgerops.domain import ActorId, ClientId, ReversalCommand, RequestKey, WorkspaceId

# workspace_uuid / merchant_actor_uuid / client_uuid 来自受信调用边界；
# UUID 本身不是认证。original_purchase_uuid 是已经提交的成功购买。
workspace = WorkspaceId.parse(workspace_uuid)
principal = Principal(workspace, ClientId.parse(client_uuid), ActorId.parse(merchant_actor_uuid))
command = ReversalCommand.from_payload(workspace, {
    "operation_id": reversal_uuid,
    "original_operation_id": original_purchase_uuid,
    "reason": "customer_refund",
})
key = RequestKey(workspace, principal.client_id, "refund-transport-key", "reversal:v1")
result = submit_reversal(conn, principal, command, key)
```

只有原商户账户的 owner 能发起或读取冲正结果。原客户、其他商户、仅知道购买 UUID 的
调用者都不因此获得权限。工作区、client、请求路由必须匹配。当前没有 HTTP 或 token
认证适配器，调用方负责认证 Principal，不能直接信任请求里的 actor 字段。

reason 原样进入规范字节，需非空、合法 UTF-8、不含 NUL、最多256字节。新业务 ID 表示
一次新决定；请求键表示一次传输身份。购买和冲正的业务 ID 在同一 workspace 内共用
唯一空间，请求键按 workspace/client/route/key 隔离。

## 结果与重试

| 情况 | 结果 |
|---|---|
| 原交易合格，原商户可用余额足够 | 保存201、新冲正分录与成功关系 |
| 可用余额不足 | 保存422 `INSUFFICIENT_AVAILABLE`，没有分录或成功关系 |
| 累计或账户版本达到边界 | 保存422 `LIMIT_EXCEEDED`，没有财务效果 |
| 已有一次成功冲正，再用新业务 ID 尝试 | 保存422 `ALREADY_REVERSED`，不再次扣款 |
| 同身份、相同内容再提交 | 返回原成功或拒绝，原诊断 request_id 也不变 |
| 有权限查看的请求键／业务 ID 被换内容 | 未保留409身份冲突；临时认领回滚 |
| 原购买不存在、不合格或无权查看 | 通用404，无新认领 |
| 原请求、原分录或结果损坏 | 内部不变量错误并回滚，不伪造成业务拒绝 |

201 响应包含新 operation_id、original_operation_id、journal_id、字符串 amount_minor、
currency、admission、`effect="purchase_reversed"`、整数 contract_version=1，不包含当前余额。
身份冲突也先校验已保存业务归属；传入一个自己有权处理的对象不能探查另一个对象的结果。

商户账面140、预留100，可用40，冲正80被拒；补款40之后，用旧业务 ID 仍返回原422，
用新业务 ID 才能重新判断并成功。拒绝尝试保留多条，成功关系最多一条。预留不改，
成功后商户账面100／预留100、客户100。测试的合成补款和预留设置不是新公共接口。

40P01／40001 最多重试三个完整事务；超时、取消、任意完整性错误和连接丢失不自动重试。
COMMIT 断线可能结果未知。调用方重连并复用原业务 ID、原请求内容和请求键，不能换新
业务身份猜测结果。新键／同业务 ID 也可找回结果，但原键有助于同时确认传输关联。

## 锁、唯一性与独立核对

购买与冲正共用设置、工作区共享屏障、模式检查、身份认领、重放和有限重试。
新冲正顺序是：请求键 → 自己的业务 ID → 原购买行 FOR UPDATE → 按账户 UUID 排序的
余额行。只有已经提交的成功购买能作为原交易。维护先拿工作区排他屏障，不提前拿余额锁。

两个新冲正 ID 同时处理一笔购买，后者等原购买锁；前者提交后，后者看到成功关系并
保存 ALREADY_REVERSED。成功关系的主键是最后一道数据库保护，意外23505不会被吞成成功
或正常重放。相同请求键的竞争仍使用 R1 的唯一认领等待规则。

`reversal_attempt` 保存每笔合格尝试；`purchase_reversal_effect` 只保存成功关系。
复合外键限制工作区、类型和原交易绑定，延迟约束要求成功冲正同时有完整分录和成功关系、
拒绝冲正没有财务效果。新历史表禁止修改或删除；已提交分录仍禁止事后追加。
账户 owner、规范请求和实际分录一致性由 Python 及独立核对器检查，不能只靠借贷平衡证明。

导出格式 v4 包含全部尝试与成功关系。核对器只使用标准库重新解析原购买、冲正请求，
检查反向账户、金额、响应和关系；同一错误退款对象的分录和余额即使通过 A，也会被 B
拒绝。旧格式 v1/v2/v3 的冻结文件继续可读、可核对。

冲正后的账本可以继续走[维护流程](MAINTENANCE.md)。应用重建仍要求 B 和零预留，
恢复只要求结构/A，并保存 B／预留报告。全额冲正仅处理本来正确的购买，不是对任意
损坏原账或所有历史业务错误的通用修正器。有限测试也不代表全部交错和外部故障都已穷尽。
