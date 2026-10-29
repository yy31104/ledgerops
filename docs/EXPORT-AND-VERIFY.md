# R2-PR01：导出与独立核对

本地已实现一致导出和离线核对。PR02 的[维护流程](MAINTENANCE.md)使用独立维护登录。
命令需要已经初始化、含业务数据的数据库；本任务不创建
常驻数据库或 HTTP 服务。测试会创建自己的临时 PostgreSQL，并在结束后清理。

## 数据库权限

导出使用独立的 `ledger_reader` 登录。它有 CONNECT、schema USAGE 和显式列出的账务与维护表的
SELECT 权限；R4 另授予证据读取所需的 pin 增删与指定列锁定权限，不能据此修改账务。
它不是所有者，也不属于应用或迁移角色。READ ONLY 事务是第二层边界，不能
代替数据库权限。测试用真实 reader 凭据，在普通事务中验证写入、DDL、临时表与提权均失败。

已有 R1 数据库升级时，先由本地管理员建立 reader 角色并私下设置密码，再运行迁移器。
例如在管理员的 psql 会话中：

```sql
CREATE ROLE ledger_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;
\password ledger_reader
```

迁移连接需要既有的 `ledger_migrator` 权限。迁移器不负责创建角色或密码：

```sh
# 在本地环境中设置 LEDGEROPS_MIGRATION_DSN，勿把凭据写进版本库或证据日志。
PYTHONPATH=src uv run --locked python -m ledgerops.db.migrations
```

新增 `002_export_reader.sql`；`001_foundation.sql` 与购买流程不变。PR02 再增加
`003_projection_maintenance.sql`，应用前还须准备 maintenance 角色。测试夹具完整演示了隔离初始化。

## 使用命令

在仓库根目录运行；环境中的 `LEDGEROPS_EXPORT_DSN` 使用 reader 凭据，
`LEDGEROPS_WORKSPACE_ID` 是要导出的 workspace UUID：

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli export \
    --workspace "$LEDGEROPS_WORKSPACE_ID" --output-root ./var/exports
```

输出 `{"export_path":".../<导出 UUID>"}`。把这个路径设置为 `LEDGEROPS_EXPORT_DIR`，
即可核对；此步不连接数据库，也不需要 DSN：

```sh
PYTHONPATH=src uv run --locked python -m ledgerops.cli verify "$LEDGEROPS_EXPORT_DIR"
```

源码运行会记录当前 HEAD 与工作区 dirty 状态。安装包运行需要明确传入
`--code-revision <构建所用的完整40位Git提交>`；构建含未提交修改时同时传 `--dirty`。
运行目录刚好在仓库里，不代表安装包就对应那个仓库版本。显式版本是调用者的声明，
不是工具自动证明了包的来源。DSN 不写入导出文件；命令错误只输出错误类别。

退出码：`0` 表示账内结构/A/B 通过；`1` 表示检测到不一致；`2` 表示无法读取、导出失败
或包格式无效。v1–v4 的非零预留不参与完整生命周期核对；v5 的 A 同时检查 reserved 是否等于活动 hold 之和。

## 报告能证明什么

| 字段 | 含义 |
|---|---|
| `structure_ok` | 账户、分录表头、借贷、行数和关联满足已声明结构 |
| `projection_ok`（A） | 从全部分录独立重算借贷合计；v5 另从不可变 hold 事件重算 reserved，含零活动账户 |
| `request_intent_ok`（B） | 效果和保留结果符合保存的规范业务请求；v5 包含提现准入，外部终局另作边界披露 |
| `reservation_supported` | v1–v4 要求无非零预留；v5 已支持 hold 历史，由结构检查验证其合法性 |
| `healthy` | 结构、A、B 均通过，v6 起还要求未发现支付商证据矛盾；不代表外部付款已成功或证据真实性已验证 |
| `rebuild_eligible` | 结构、B 及预留条件通过；余额漂移本身是可修复对象 |
| `issues` | 每个错误的对象、字段、预期值与实际值 |
| `unverified_intent_kinds` | 缺少规范业务意图契约的类型，如 R1 的 OPENING fixture |

例如，payload 写给 M1，分录和余额都记给 M2：A 会通过，B 报 `WRONG_OWNER`。
若 payload 自己也错存成 M2，则三者可以一致；测试的手工原始输入清单用于发现这个错误。
运行时不要求手工清单，B 也不声称独立证明了外部请求捕获正确。

R1 OPENING 是显式测试开户，payload 没有规范充值指令；核对检查摘要、开户结构和
结果并披露这项意图边界，不凭空编造资金来源。历史业务拒绝只核对记录和无财务效果，
不会拿今天的余额重新决定昨天是否该拒绝。

`rebuild_eligible` 不是数据库操作许可，也不能证明文件仍对应当前数据库。PR02 在
排他屏障内重新读取、核对并现场计算目标余额，参见[维护说明](MAINTENANCE.md)。

## 文件与快照边界

同一条连接、同一个 `REPEATABLE READ READ ONLY` 事务读取所有表。普通导出可与购买
并发，文件描述的是被固定的快照，不一定是文件发布时刻的最新状态。每张表单独开启查询
快照会拼出从未存在过的状态；并发测试及隔离级别变异用例明确验证了这点。

版本 1–9 都包含 `data.json` 与 `manifest.json`。UUID 是规范字符串，金额是十进制整数字符串，
状态/版本/行号保留 JSON 整数类型，payload 字节无损十六进制编码。manifest 记录 Git
版本、dirty、行数、字节数和数据摘要。数据摘要检查损坏，不是源码清单，也不是真实性签名。
最多每表 10,000 行，完整包不超过 16 MiB；超过上限失败，不截断。

先写同目录的临时包，flush/fsync、核验后 rename 成全新 UUID 目录，再 fsync 父目录。
旧导出不会覆盖。加载器拒绝临时目录、成员软链接、FIFO、不完整包、额外成员、重复键、
摘要/大小不符、错误作用域与未知版本。rename 之后父目录 fsync 失败时，最终包可能已存在，
调用方须检查文件，不能据失败推断“从未发布”。这不是跨机器发布协议。

导出中断后会残留 `.ledgerops-export-*` 隐藏临时目录；读取时会被拒绝；确认没有导出进程
在运行后，可以手动删除这些临时目录。

版本 1 保留迁移 001/002 的 R1 财务表契约；版本 2 对应 001/002/003，并增加
workspace 的 projection_epoch/current_freeze_id、冻结元数据、修复表头和完整审计行。
新核对器仍接受已保存的 v1 文件，不通过替换 schema 列表来使旧证据失效。
版本 3 对应 001–004，增加冻结记录的 resume_report；v1/v2 仍按原格式解读。
版本 4 对应 001–005，增加 reversal_attempts 与 reversal_effects，B 同时核对购买和冲正。
旧 v1/v2/v3 包保持原规则。未知数据库版本直接失败，避免悄悄漏掉新表。

版本 5 对应 001–006，包含 payouts、reservations、reservation_events、dispatch_intents、transport_attempts 和 repair.fold_version。核对器只依赖标准库，从 retained request 检查提现意图，从事件检查 hold 来源；生产准入/派发代码不参与生成预期。PR02 尚不处理外部金融终局，报告在 `unverified_intent_kinds` 明示 `PAYOUT_EXTERNAL_RESULT`。原始请求自身的真实性、报价/收款方配置的真实性、传输响应的签名权威不由账内 A/B 单独证明。旧格式的能力边界保持原样。

版本 6 对应 001–007，增加保留的支付商观测、收据、隔离记录、原始 incident、终局财务 resolution
及关联 incident。report v2 独立核对规范 envelope、摘要、原请求绑定、终局观测、hold 与财务效果；
`external_evidence` 区分未完成的提现、支付商矛盾和本地 blocker。保留的签名没有附带可信密钥，
因此 `signature_authenticity=NOT_VERIFIED_NO_TRUST_KEYS`；账务导出也不等于独立支付商历史。
本地 blocker 和未完成工作会列入报告；`healthy` 仍只表示声明范围内的一致性。

版本 7 对应 001–008，保持 v6 账务行形状；R4 发布信息和原始输入保存在另行固定的不可变对象中。
版本 8 对应 001–009，保持 v7 行形状，并支持恢复模式的维护审计。认证凭据、token 摘要和权限表不进入账务导出。

版本 9 对应 001–010，增加 `operator_actions`、`operator_approvals`、`operator_executions` 和
`payout_incident_resolutions`。原 incident 保留；只有完整核对通过的
`REPROCESS_TERMINAL_LOCAL_BLOCKER` 执行链，才能让相应 LOCAL_BLOCKER 不再列为未解决 blocker。
核对覆盖 intent/proposal 的原始规范字节及 SHA-256、maker/checker 的不同 actor ID、
proposal/approval/execution 的相同摘要、保留的终局观测、同一提现的一次财务 resolution、
保留的执行结果，以及 proposal 中确切 incident 集合的完整处置。关联支付商矛盾、非本地类别、
缺少执行或不完整的处置不会被隐藏；后来的其他 incident 仍单独报告。

v9 输出 report v3 的 `operator_actions` 计数与能力披露，并支持在历史 resume_report 中保存这份报告。
这些是结构和不透明身份的核对；导出不包含认证历史，不能证明当时的授权、撤销状态或人员身份。
报告明确给出 `historical_authorization=NOT_VERIFIED_NO_AUTH_HISTORY`，支付商 HMAC 真实性仍未验证。
失败的非财务 operator_attempt 保留在受控 action history 中，不进入财务交换包。
旧 v1–v8 的字段、报告版本和各自能力保持不变；换成 v9 的 schema 列表不能为旧文件补出新证据。
