# Opus 审查：LedgerOps R1（PR01–PR03）

日期：2026-10-24。范围：R1-PR01 到 PR03 的本地实现。这是审查意见，不是用户已采纳的决定。

方法：
- 独立重跑 `make test`：237 passed，用时 20.5 秒，结束后没有残留测试容器。与 PR03 证据记录一致。
- 逐行读了 `src/ledgerops/db/admission.py`、`src/ledgerops/db/sql/001_foundation.sql`、`domain/identities.py`、`domain/requests.py`。
- 没有逐个审查测试文件，也没有审查 Chronicle。

## 结论

R1 的三项目标都已达到：记账、按请求键重放、并发扣款。我没有找到正确性问题。
开始 R2 之前先做 F1 和 F2；F3、F4 可选。

## 核对过、确认成立的要点

| 要点 | 依据 |
|---|---|
| 两笔 80 扣 100 只成功一笔 | 按账户 UUID 排序加 `FOR UPDATE OF b`；READ COMMITTED 下等到锁后会读到最新已提交余额。Python 的余额判断即使写错，`booked >= reserved` 的 CHECK 也会在数据库层拒绝透支。 |
| 同键并发不重复记账 | `ON CONFLICT DO NOTHING` 会等同键的在途事务结束再判断。所有请求都先抢请求键、再抢业务 ID、最后锁余额，顺序固定，锁等待不会成环。 |
| 已提交分录不能追加 | 延迟约束触发器在 COMMIT 时比对表头声明的行数、借方合计、贷方合计。已提交分录再插入任何一行，实际行数都会超出声明。 |
| 重试边界 | 只重试 40P01/40001。回滚后先确认连接回到 IDLE 再退避；超时、取消、断线都不在内部重试。 |
| 冻结屏障对得上 | Python 侧锁键是 `f"ledgerops:workspace:{workspace_id}"`，`WorkspaceId.__str__` 输出标准 UUID 文本，与 SQL 侧 `'ledgerops:workspace:' \|\| uuid::text` 相同。 |
| 不泄露他人记录 | 碰到已有请求或业务 ID 时，先校验调用者是否拥有**已保存**命令里的客户账户，再决定返回原结果还是冲突。 |

## 必须做（R2 之前）

### F1 两个仓库都没有任何 git 提交

`ledgerops` 和 `chronicle` 都显示 `main does not have any commits yet`，全部文件未跟踪。

- 风险：一次 `git clean -fdx` 或误操作就会丢掉三个 PR 的工作，而且无法恢复。
- 所谓 PR01/02/03 只存在于文档里，代码层面无法区分，也无法查看某个 PR 改了什么。
- 证据记录只能用自建的 SHA-256 清单代替提交哈希。HANDOFF 已承认 PR01 的清单与当前文件对不上。

建议：两个仓库各做一次初始提交，只在本地，不推送。以后每个 PR 一个提交，证据记录引用提交哈希。

### F2 `_submit_purchase_once` 太长，不利于讲清楚

这一个函数有 210 行（第 204–422 行），执行 16 次 SQL，代码最深缩进 8 层。

当初把逻辑从 PL/pgSQL 移到 Python，就是为了面试时能对着代码讲清楚。现在这个函数很难一口气讲完。另外，R2 的冲正会复用屏障、身份抢占和重放这三段逻辑；不先拆开，就只能复制一份。

建议做一次纯重构，行为不变，测试一行不改，作为 R1-PR04：

```
_submit_purchase_once
  _open_admission(cur, principal, command)          # 事务设置、共享屏障、模式检查、客户授权
  _replay_by_request_key(cur, principal, scope, payload)    # 请求键已存在
  _replay_by_operation(cur, principal, scope, command)      # 新请求键，但业务 ID 已存在
  _post_new_purchase(cur, principal, command, scope)        # 加锁 → 判断 → 写分录/余额/结果
```

完成标准：237 项测试照常通过，原有测试文件不改；每个函数都能用一句话说清用途。

## 建议做

### F3 一条命令的演示（可选）

Chronicle 已有 `make demo-postgres`，LedgerOps 还没有演示命令。建议加 `make demo`：
启动临时 PostgreSQL，初始余额 100；并发发起两笔 80，打印一个 201、一个 422；
再在 COMMIT 时切断一条连接，用原身份重新提交，打印“没有重复记账”。

测试里已有对应的辅助代码，成本很小。这就是面试时现场演示的内容。

### F4 掌握度仍是“未评估”（不作为开工门槛）

按已定规则，学习进度不限制 AI 开发进度，这里不改。只是到目前为止，用户拍板的方式都是“沿用默认”。
建议 F2 完成后，用户花 30–45 分钟口头回答下面三题；答不上来的就是复习重点：

1. 为什么两笔 80 扣 100 恰好一笔成功？如果去掉 `FOR UPDATE` 会怎样？
2. COMMIT 时连接断了，调用者怎么知道钱扣没扣？为什么不能换一个新业务 ID 重试？
3. 为什么死锁会重试，锁超时却不重试？

## 建议顺序（交给 Codex）

1. F1：用户确认后，两个仓库各做一次本地初始提交。
2. R1-PR04：F2 的纯重构，可选加上 F3。
3. R2 任务契约，以及 3–5 项的 R2 决定包，再开始 R2 编码。
