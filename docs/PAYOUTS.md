# 提现接入：预留资金、保留指令、事务外派发

这是 R3-PR02 的 Python 服务接口。当前片不提供账本公网 HTTP，也不消费或释放 hold；
支付商返回终局后如何记账由下一片 R3-PR03 接入。模拟支付商始终是合成协议，不能据此声称接通了银行。

## 金额与回执

客户100购买80后：客户20、商户80、清算资产100。商户提现本金60，固定手续费2，
总额62。成功准入在一个 PostgreSQL 事务里创建 payout、HELD reservation、不可变初始事件、
READY dispatch_intent，并把商户 reserved 从0增加到62、version加1。不新增出金分录。
商户此时 booked80 / reserved62 / available18，清算资产仍100。

`submit_payout(conn, principal, command, request_key, policy=..., clock=...)` 在 COMMIT
之后返回202 `accepted_for_processing`。连接要求空闲autocommit；`Principal` 来自受信认证边界。
请求使用 `PayoutCommand` 与 `payout:v1` RequestKey。规范字段是 operation_id、merchant_account_id、
beneficiary_id、provider_namespace、principal_minor、fee_minor、currency、contract_version；
workspace 来自受信作用域，金额使用最小单位的规范十进制整数字符串。

`TrustedPayoutPolicy` 提供 workspace、provider namespace、固定费用、派发窗口和
beneficiary→merchant owner 绑定。不能从不可信 HTTP 请求体直接构造这份策略。
仅原商户主体可以准入或读取；知道 UUID 不能自授角色。新业务检查账户启用与可用额，
不足保存422，不产生hold；身份换内容409、授权404不保留新结果。

新准入才采样时钟并固定 dispatch_deadline，保存精确支付商请求字节与摘要；
客户端规范请求不含这个服务端时间。旧请求重放仍返回原202/正文/截止，即使当前报价、
窗口或后续支付状态变化。`get_payout_state(conn, principal, payout_id)` 单独读当前进度；
它不会重写准入回执。已拒绝业务也不因今天补款改判。

## 派发与恢复

`ledgerops.db.payout_dispatch` 提供 `due_payouts`、`claim_payout`、`finish_claim`、
`dispatch_once`。扫描有批量上限、无行锁；claim 在共享工作区屏障后重新读模式，
按 operation→payout→intent 加锁，写 MAY_HAVE_SENT 尝试、UNKNOWN 认知与下次到期时间，
**提交之后才联网**。没有提交成功的 claim 不发送。进程在提交与发送之间死亡，也按未知处理。

第一次调用为PUT；失去进程的旧 claim 到期后先LOOKUP。截止前查不到/查证传输失败，
按有界调度重发同一个 key 和原始字节；截止后申请 close-if-absent。实际发送前再检查截止，
防止 claim 后暂停跨过截止还新发PUT。在途PUT仍可能先于关单成功，截止不是取消。
ACCEPTED/终局响应继续观察，不能凭HTTP200或传输成功改变资金。

每次 `dispatch_once` 至多一次网络调用；早期轮次至多五次较短退避，之后每次调用至少进入
较慢观察节奏，延迟带有上界和抖动。候选、尝试和响应都有显式大小限制。次数耗尽、
超时、500、NOT_FOUND、工作区冻结都不能释放62。数据库只重试确定已回滚的40P01/40001，
网络调用不放进数据库重试体。

旧 attempt 的迟到完成只补自己的事实，不能覆盖新 claim 的调度状态。FROZEN/RESTORE
挡住新的账本写入与观测落库，不确认已持久接收；恢复后重新观测。冻结不停止已经发出的外部请求。
PUT前预检结算容量；LOOKUP/CLOSE不制造新的支付，可以在本地投影障碍存在时继续取证。
有限预检不保证未来并发不会耗尽累计数值/version容量。

`ProviderHTTPClient` 只接受显式127.0.0.1地址，无自动重试、重定向或环境代理。
`ProviderTrust` 明确 namespace 和最多8个验证密钥；请求字节/密钥/商户/收款方/金额/币种/
协议版本/issuer/事件ID/序列全绑定后才作为已认证观测。NOT_FOUND只证明一次查询暂时缺失。
本片保留有界传输字节并安排后续观察，金融终局尚未应用。

## 角色、迁移与重建

应用006之前，由管理员单独创建 worker 登录并私下设置密码：

```sql
CREATE ROLE ledger_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;
\password ledger_worker
```

不赋予 app/migrator/maintenance 成员资格。worker 可读取指令、领取与完成传输尝试；
不能新建业务、改余额、写分录或修改hold。对 operation 的极窄UPDATE列权限只供
`SELECT ... FOR UPDATE` 使用，既有不可变触发器仍阻止改写已保存的业务。
迁移连接不创建角色；001–005不改。旧库若存在没有hold来源的非零reserved，升级明确失败。

新v5导出包含全部提现/hold/事件/意图/尝试；旧v1–v4继续按原能力读取。
A从journal重算借贷合计，从不可变hold事件重算reserved；B从原始请求检查本地准入意图。
存在合法hold62时可以修复booked/reserved投影漂移：冻结排空→预览→排他事务现场重算
并记完整before/after与epoch→复验→恢复。hold与事件不修改，避免“有预留就永远无法恢复”。
详见[维护](MAINTENANCE.md)和[导出核对](EXPORT-AND-VERIFY.md)。

## 尚不声称的能力

PR02的202不等于外部支付成功；报告明确外部终局未验证。A/B一致不证明原始请求、
报价或收款人资料本身真实。模拟器提供永久key与关单墓碑，真实支付商可能没有这些能力。
真实资金接入、生产身份配置、持续调度服务、结果入账与故障矩阵分别按后续切片验收。
