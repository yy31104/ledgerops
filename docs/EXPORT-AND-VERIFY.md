# R2-PR01：导出与独立核对

本地已实现一致导出和离线核对。命令需要已经初始化、含业务数据的数据库；本任务不创建
常驻数据库或 HTTP 服务。测试会创建自己的临时 PostgreSQL，并在结束后清理。

## 数据库权限

导出使用独立的 `ledger_reader` 登录。它只有 CONNECT、schema USAGE 和八张指定表的
SELECT 权限，不是所有者，也不属于应用或迁移角色。READ ONLY 事务是第二层边界，不能
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

新增 `002_export_reader.sql`；`001_foundation.sql` 与购买流程不变。初始化新数据库时，
同样需要预先准备 R1 的 migrator/app 角色与 reader。测试夹具完整演示了隔离初始化。

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
或包格式无效。非零预留可能不影响账内 posted 核对，因此仍须检查报告的预留字段。

## 报告能证明什么

| 字段 | 含义 |
|---|---|
| `structure_ok` | 账户、分录表头、借贷、行数和关联满足已声明结构 |
| `projection_ok`（A） | 从全部分录独立重算的余额与投影一致，含零活动账户 |
| `request_intent_ok`（B） | 效果和保留结果符合记账前保存的规范购买请求 |
| `reservation_supported` | 当前没有需要额外生命周期依据的非零预留 |
| `healthy` | 结构、A、B 均通过，指账内 posted 一致性 |
| `rebuild_eligible` | 结构、B 及预留条件通过；余额漂移本身是可修复对象 |
| `issues` | 每个错误的对象、字段、预期值与实际值 |
| `unverified_intent_kinds` | 缺少规范业务意图契约的类型，如 R1 的 OPENING fixture |

例如，payload 写给 M1，分录和余额都记给 M2：A 会通过，B 报 `WRONG_OWNER`。
若 payload 自己也错存成 M2，则三者可以一致；测试的手工原始输入清单用于发现这个错误。
运行时不要求手工清单，B 也不声称独立证明了外部请求捕获正确。

R1 OPENING 是显式测试开户，payload 没有规范充值指令；核对检查摘要、开户结构和
结果并披露这项意图边界，不凭空编造资金来源。历史业务拒绝只核对记录和无财务效果，
不会拿今天的余额重新决定昨天是否该拒绝。

`rebuild_eligible` 不是数据库操作许可，也不能证明文件仍对应当前数据库。PR02 将在
排他屏障内重新读取、核对并现场计算目标余额。PR01 没有维护写入命令。

## 文件与快照边界

同一条连接、同一个 `REPEATABLE READ READ ONLY` 事务读取所有表。普通导出可与购买
并发，文件描述的是被固定的快照，不一定是文件发布时刻的最新状态。每张表单独开启查询
快照会拼出从未存在过的状态；并发测试及隔离级别变异用例明确验证了这点。

版本 1 包含 `data.json` 与 `manifest.json`。UUID 是规范字符串，金额是十进制整数字符串，
状态/版本/行号保留 JSON 整数类型，payload 字节无损十六进制编码。manifest 记录 Git
版本、dirty、行数、字节数和数据摘要。数据摘要检查损坏，不是源码清单，也不是真实性签名。
最多每表 10,000 行，完整包不超过 16 MiB；超过上限失败，不截断。

先写同目录的临时包，flush/fsync、核验后 rename 成全新 UUID 目录，再 fsync 父目录。
旧导出不会覆盖。加载器拒绝临时目录、成员软链接、FIFO、不完整包、额外成员、重复键、
摘要/大小不符、错误作用域与未知版本。rename 之后父目录 fsync 失败时，最终包可能已存在，
调用方须检查文件，不能据失败推断“从未发布”。这不是跨机器发布协议。

当前只支持迁移 001/002 的 R1 财务表。新增冲正关系或维护元数据时必须扩展导出契约；
未知数据库版本直接失败，避免悄悄漏掉新表。
