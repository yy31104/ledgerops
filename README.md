# LedgerOps

A local laboratory for synthetic transaction integrity, replay and recovery.
**R1-PR01 through R1-PR04 are implemented locally:** typed money/identity, PostgreSQL
safeguards, an explicit-SQL purchase service, bounded transaction retries, and
concurrency / response-loss recovery tests with independent financial checks.
**R2 is complete within the declared local scope:** a dedicated reader exports one consistent
snapshot, and an offline verifier separately checks journal structure, projected
balances and fidelity to retained purchase requests. Maintenance can drain writers,
preview drift, atomically recompute/audit balances and explicitly resume. Original
merchants can now submit full internal reversals with retained failures and one success.
All financial examples use synthetic PLN minor units.

## Run locally

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), make and a running Docker engine.
The lockfile uses psycopg 3.3.6; tests pin PostgreSQL 18.6 by image digest. Docker may
need to download that image on the first run.

```sh
make bootstrap         # install the locked environment
make check             # Ruff plus installed-package / SQL-resource checks
make test              # complete suite, including real PostgreSQL
make test-unit         # domain and independent-reference tests; no Docker
make test-integration  # actual PostgreSQL tests
make build             # wheel and source distribution
make demo              # complete narrated story in temporary PostgreSQL; auto cleanup
make lab               # interactive local learning page; Ctrl-C / end button cleans up
```

Integration tests create one uniquely named disposable container on an ephemeral
localhost port, with random credentials and a fresh database per test. They remove
their own resources afterward and never use an existing application database.
Missing Docker fails the suite instead of skipping tests.

Tests load current `src/` explicitly. The package check forces a fresh non-editable install:
on this Mac, Python ignores hidden `.pth` files, making editable-only import checks
unreliable. Build archives include migration SQL and exclude private reference docs.
No persistent database is provisioned. The optional source-checkout learning lab
below runs a temporary loopback teaching adapter; the production package has no HTTP API.

## 交互式学习工作台

在仓库根目录运行 `make lab`，打开终端打印的 `http://127.0.0.1:端口/`。
页面左侧是12步导览，每次点“执行这一步”只执行一步；右侧可以切换
“这一步 / 执行过程 / 代码与面试”。先读解释，再操作，再查看余额、分录、
业务身份和原始返回结果，最后对照当前仓库里的函数。

“执行过程”记录这次调用实际发出的客户端 SQL、参数、事务命令、SQLSTATE
和重试次数，排除页面刷新的查询。它不展开数据库触发器内部语句，也不代表
已经观测到锁等待。自动提交的单条故障注入不会凭空补上BEGIN/COMMIT。
自由实验支持更换金额和商户、原身份重放、原键改金额409、新业务重复冲正422、
漂移与维护；自由写操作后请重置再按标准导览学习。重放的是最近一次提交的购买，
包括已保存的拒绝；冲正针对最近一次成功购买。

所有操作只连接本次新建的临时 PostgreSQL，不接受浏览器传入DSN或任意SQL。
身份来自固定的合成夹具，**不是登录认证**。页面只监听127.0.0.1，不进入wheel或
源码发行包，不加运行时依赖，生产代码和迁移保持原样。结束时使用“结束并清理”
或终端Ctrl-C；关闭浏览器标签页不会停止服务。重置创建新的工作区，清空页面进度；
退出才删除整个临时数据库和导出文件。Docker不可用会明确失败。
余额卡片下面的“并发单步”可展开两个真实场景：两笔80竞争100、购买未结束时
冻结等待。启动会创建新的合成工作区，每点一次推进一个阶段；观察实际PID、
`pg_blocking_pids`和阻塞SQL，再释放余额行锁。结束后显示真实结果和独立核对。
刷新会先清理未完成的场景；也可用“清理场景”或顶部重置。
教学会话五分钟后自动取消；worker额外执行十分钟数据库超时后备SQL，给清理
留出余量。原入口的5秒/10秒配置仍先执行，额外配置在追踪中可见，且仅用于
教学连接。超时不用于决定正常执行顺序。详见[并发切片](docs/tasks/LAB-CONCURRENCY.md)。
完整范围、解释文案和验证记录见 [学习工作台任务](docs/tasks/LAB.md)。

## Narrated local demo

Run `make demo` from this repository root. It starts its own temporary PostgreSQL,
uses the frozen synthetic opening and existing service interfaces, then prints12
short steps: purchase success/refusal, reversal success/duplicate422, deliberate
projection drift, offline A failure, freeze/preview/rebuild/resume and final healthy
verification. All amounts are minor units. Duplicate422 uses a new business ID;
same-ID replay still returns its original result.

The temporary database/container and export files are cleaned when the run ends;
cleanup completion is printed only after fixture teardown succeeds. Docker absence
fails clearly. Existing user databases/DSNs are not used. The script is a pytest
fixture-backed demo (`tests/demo_story.py`), with normal subprocess smoke coverage,
not a new production command or migration. See [the slice contract](docs/tasks/DEMO.md)
and [actual output](evidence/DEMO/2026-10-28/demo.log).

## Purchase service

`ledgerops.db.admission.submit_purchase(conn, principal, command, request_key)` takes
an idle autocommit psycopg connection, trusted typed principal, validated
`PurchaseCommand` and scoped `RequestKey`. The caller is responsible for authenticating
the workspace/client/actor context. It returns `AdmissionResult` after COMMIT.
The [service integration test](tests/integration/test_purchase_admission.py) gives
executable inputs and checks independently calculated results.

The transaction takes the shared workspace barrier, authorizes the customer,
reserves request/business identities, locks balances in account-ID order, then
writes the journal, relative balance deltas and both stored results atomically.
Same bytes replay the original result; changed bytes conflict. Authorization also
covers the stored operation, so knowing its ID cannot reveal another customer's result.
Insufficient available funds persists a stable rejection; new funding requires a
new operation ID for a new decision. Success contains no mutable current balance.

The historical PR04 extraction reduced the purchase orchestrator to 63 source lines: workspace
admission, request-key replay, business-ID replay and new purchase posting each have
an explicit helper. Request/business claim order and the COMMIT boundary remain
visible in the orchestrator. [PR04 evidence](evidence/R1-PR04/2026-10-25/README.md)
compares that extraction to the committed R1 baseline without changing tests.
PR03 now shares one transaction orchestrator and retry boundary between purchase
and reversal; request-before-business ordering and the COMMIT boundary remain visible.

## Full reversal

`ledgerops.db.admission.submit_reversal` accepts a typed `ReversalCommand` with
new operation ID, original purchase ID and exact reason. Only the original merchant
actor can reverse an eligible accepted purchase. Amount/accounts come from verified
original records. [Usage and contract](docs/REVERSAL.md).

Reversal takes the original row lock before sorted balance locks and adds a new
merchant-debit/customer-credit journal. Original purchase rows stay immutable.
Rejected attempts survive; only success occupies the unique original→reversal relation.
Funding does not change a retained refusal: use a new business ID for a new decision.
Already reversed under a fresh ID is a retained422 ALREADY_REVERSED. Replay uses the
same shared admission paths and original result, without another financial effect.

## Retry and recovery

Deadlock 40P01 and serialization failure 40001 retry at most three whole transactions
with unchanged identities. Each rollback must restore an open IDLE connection before
backoff (10–50 ms, then 20–100 ms). Lock timeouts and query cancellation are returned as
unretained retryable errors; arbitrary integrity faults propagate unchanged.

Connection failure can leave COMMIT outcome unknown. The service propagates it and
the caller reconnects with the same business ID and command. Tests sever the actual
COMMIT before PostgreSQL receives it, withhold acknowledgement after it commits,
and kill a committed worker before its business reply. Recovery returns or creates
one effect according to committed state; no automatic reconnect is hidden in service.

## Export and independent verification

See [export/verify usage](docs/EXPORT-AND-VERIFY.md) for role setup, commands,
report fields and the bounded bundle contract. Existing databases must provision
`ledger_reader` before applying migration 002. Application grants and migration
001 are unchanged.

Export reads all workspace tables in one REPEATABLE READ READ ONLY transaction.
A folds entries into balances; B decodes retained `operation.payload` and compares
exact effects and saved results, without a hand-authored business manifest.
A consistent transfer to the wrong merchant can pass A and fail B. A test-only
manifest additionally detects a request itself captured with the wrong merchant.

Reports disclose the legacy OPENING fixture's opaque funding intent. Nonzero
reservations prohibit rebuild eligibility even when posted balances agree.
Eligibility is a source-check result, not permission to mutate a live DB.
Format v4 adds reversal attempts/effects to the saved resume reports and maintenance
metadata. Old v1/v2/v3 bundles remain accepted under their original schema rules. Unknown future schemas fail until explicitly supported.

## Maintenance

[PR02 workflow](docs/MAINTENANCE.md): freeze/drain → preview → live transactional
recompute/apply → post-export/check → explicit resume. Apply takes a stable repair
ID and current freeze identity, never a list of target balances. It keeps the
workspace FROZEN, records every account's before/after values, advances versions
and projection epoch atomically, and verifies the result independently. Same-ID
recovery never applies twice; failed/unknown COMMIT uses the original repair ID.
Nonzero reservations and source/intent errors block application. Resume gates only
structure/A; full B/reservation findings are saved in its immutable resume report.
No candidate tables.

The maintenance credential has projection/metadata authority, not financial-history,
reservation, DDL or owner authority. All cooperating writers must take the shared
barrier; raw privileged SQL remains outside that protocol. Database freeze does
not establish that external payments stopped.

## Evidence and boundaries

[Demo verification](evidence/DEMO/2026-10-28/README.md): the actual narrated run
completed successfully; **583 regression cases pass**, including the two new
make-target/Docker-unavailable smokes. Production code and migrations are unchanged.

[R2 integrated acceptance](docs/R2-ACCEPTANCE.md) maps every R2 requirement to exact
tests and states the trust limits and reviewed disabled-customer404 boundary.
[PR04 evidence](evidence/R2-PR04/2026-10-28/README.md): **581 passed**, zero failures/
errors/skips (272 unit, 41 reference, 268 real PostgreSQL). Four mixed schedules,
three targeted mutations, two actual exporter interruptions and one explicit
review-boundary case close the final gaps. Production/migration/reference/fixture
bytes are unchanged from PR03. Package checks and disposable-database setup pass;
this is not production deployment or proof of every possible failure schedule.

[R2-PR03 verification](evidence/R2-PR03/2026-10-28/README.md) records **571 passing
cases**: 272 unit, 41 reference, 258 real PostgreSQL integration. 123 new cases cover
full-reversal requests, independent original/effect checks, the literal failed→funded
trace, merchant authorization, observed identity/original/balance waits, deferred DB
bindings, eight SQL failure stages and both actual COMMIT-loss outcomes. A reversal
history also passes export/repair/resume without changing its original purchase.
The first full run's obsolete intent-scope assertion and corrected rerun are retained.

[Resume correction](evidence/R2-RESUME/2026-10-28/README.md) is separate commit
`3d4901f`, with 448 passing tests. It permits resume with B/reserve warnings while
persisting the report and leaving the stricter repair admission unchanged.

[R2-PR02 verification](evidence/R2-PR02/2026-10-27/README.md) records **432 passing
tests**: 189 unit, 41 reference, 202 real PostgreSQL integration. The 74 new cases
cover format compatibility, maintenance, actual writer drain/identity races, stale
preview mutation, SQL/COMMIT rollback and actual wire-level ambiguous repair recovery.
The previous R2 and R1 records remain historical evidence.

[R2-PR01 verification](evidence/R2-PR01/2026-10-27/README.md) records **358 passing
tests**: 165 unit, 41 reference and 152 real PostgreSQL integration. The 121 new
cases include reader privileges, coherent concurrent exports, isolation mutation,
offline corruption detection, bundle faults and a test-only reversal model.
Source identity uses Git commits and trees.


[PR03 verification](evidence/R1-PR03/2026-10-23/README.md) records **237 passing tests**,
including 128 real PostgreSQL integration cases. The new cases force balance/identity
waits, a real deadlock, retry exhaustion, cancellation, lock timeout and the three
controlled connection/process faults. PostgreSQL lock relations and wire-frame
observations establish the tested schedules; timeouts are not treated as success.

[PR02 verification](evidence/R1-PR02/2026-10-23/README.md) covers the actual service,
including injected SQL failures through deferred COMMIT, identity/scope isolation,
retained-result corruption and workspace barrier ordering. The earlier
[PR01 record](evidence/R1-PR01/2026-10-23/README.md) remains historical evidence.

The frozen example funds 100 and purchases 80: clearing/customer/M1/M2 end at
100/20/80/0. Independent tests detect both missed projection updates and a transfer
to the wrong merchant even when journal and projection agree. Database-valid writes
can still be business-wrong; the trusted runtime credential can bypass service rules.
Replay consistency checks do not re-audit every historical entry or projection.

These bounded schedules and fault locations do not exhaust every possible failure.
Connection loss remains potentially ambiguous; never substitute a new identity.
There is no automatic reconnect, durable client command store, HTTP authentication adapter,
external provider, partial reversal, hold lifecycle, deployment or production-readiness claim.

## R2 scope

The [R2 task contract](docs/tasks/R2.md) and
[five-item Chinese decision packet](docs/DECISION-PACKET-R2.md) were adopted with user amendments on 2026-10-27. They cover independent export/checking, writer-drained maintenance rebuild,
and success-only full purchase reversal. B checks retained canonical requests, and repair recomputes in one exclusive
transaction without candidate tables. [R2-PR01](docs/tasks/R2-PR01.md) and
[R2-PR02](docs/tasks/R2-PR02.md) plus resume correction `3d4901f` and
[R2-PR03](docs/tasks/R2-PR03.md) and [PR04 integrated acceptance](docs/tasks/R2-PR04.md)
are complete. R2 acceptance covers the bounded local ledger core. Persistent demo,
HTTP authentication, external providers and later-stage work remain outside scope.

## Continue here

- [Current handoff](HANDOFF.md): implemented / verified / personal ownership.
- [Accepted decision packet](docs/DECISION-PACKET-R1.md): four defaults confirmed by Yi.
- [Foundation ADR](docs/adr/0002-r1-foundation.md), [purchase ADR](docs/adr/0003-purchase-admission.md)
  [retry/recovery ADR](docs/adr/0004-contention-and-recovery.md) and
  [working contract](docs/working/R1-CONTRACT.md).
- [PR02 task](docs/tasks/R1-PR02.md), [PR03 task](docs/tasks/R1-PR03.md) and
  [PR04 extraction task](docs/tasks/R1-PR04.md).
- [Agent rules](AGENTS.md) and [source provenance](docs/reference/PROVENANCE.md).

The repository has local Git commits and no remote. The received R1 implementation
is preserved in baseline `feb4588`; the PR04 extraction is the next independent
commit. R2 adoption is separate documentation commit `328733b`, followed by a
bounded PR01 `a067785`, PR02 `d825d41`, resume correction `3d4901f`, PR03 `82fa8ff`
and a separate PR04 acceptance commit. PR numbers identify
work slices; no GitHub pull request, deployment or tracker update has been made.
