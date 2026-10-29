# 提现接入：预留资金、保留指令、事务外派发

这是 R3-PR02/03 的 Python 服务接口，已接入认证观测和一次本地资金终结。账本公网 HTTP 与常驻 worker 另按后续切片交付。模拟支付商始终是合成协议，不能据此声称接通了银行。

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
所有响应、查询、关单、回调和历史恢复都进入同一个认证判定入口。HTTP 状态不是支付权威：即使传输状态异常，也必须检查证据本身；无效证据不能改钱。

## 角色、迁移与重建

应用006之前，由管理员单独创建 worker 登录并私下设置密码：

```sql
CREATE ROLE ledger_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS;
\password ledger_worker
```

不赋予 app/migrator/maintenance 成员资格。worker 可读取指令、领取与完成传输尝试；
006 阶段不能新建业务、改余额、写分录或修改hold；007 为终局判定增加所需的分录、余额和 hold 状态权限。仍不能新建业务或改写历史；这些权能须由受信 Python 判定流程使用，数据库并不替它验证 HMAC。对 operation 的极窄UPDATE列权限只供
`SELECT ... FOR UPDATE` 使用，既有不可变触发器仍阻止改写已保存的业务。
迁移连接不创建角色；001–005不改。旧库若存在没有hold来源的非零reserved，升级明确失败。

v5 导出包含提现/hold/事件/意图/尝试，v6 继续增加观测、收据、隔离、incident 与终结关系；旧v1–v5继续按原能力读取。
A从journal重算借贷合计，从不可变hold事件重算reserved；B从原始请求检查本地准入意图。
存在合法hold62时可以修复booked/reserved投影漂移：冻结排空→预览→排他事务现场重算
并记完整before/after与epoch→复验→恢复。hold与事件不修改，避免“有预留就永远无法恢复”。
详见[维护](MAINTENANCE.md)和[导出核对](EXPORT-AND-VERIFY.md)。

## 尚不声称的能力

202 不等于支付成功；查询状态才反映后续处理。没有秘密验证密钥的离线导出不能自行证明 HMAC 真实性；v6 报告明确区分账内一致、未决、矛盾和外部真实性未验证。A/B一致不证明原始请求、报价或收款人资料本身真实。模拟器提供永久key与关单墓碑，真实支付商可能没有这些能力。真实资金接入、生产身份配置、持续调度进程与完整崩溃矩阵分别按后续切片验收。

## 认证观测与一次终结（007）

`ledgerops.db.payout_resolution.receive_observation(conn, workspace, payout_id, raw,
trust=..., channel=..., transport_attempt_id=...)` 接收至多64KiB原始响应，返回结构化判定。
签名字节使用原始 payload、key ID 和协议域；持久保存精确签名 payload/签名及规范 envelope。
外层 JSON 的排版变化不制造新事件；同一个逻辑事件换接收通道不改变签名字节。

- SUCCEEDED：同事务写观测、Dr商户62/Cr清算62、CONSUMED事件、余额与DONE，终值商户18/0/18、清算38。
- FAILED/CLOSED_ABSENT：同事务保留证据并RELEASED，商户80/0/80，无出金journal。
- NOT_FOUND/ACCEPTED：保留HELD；已经ACCEPTED后查询失败仍只观察，不能退回未知新派发。
- 重复兼容终局或旧非终局：保留必要收据，不重复改钱，不重写原202或终结序列。
- 认证后的事件ID/序列复用、不兼容终局、终局之后更高序列的ACCEPTED：保留两份证据与incident，停止该提现自动处理。旧序列的矛盾终局也不能忽略。

未认证或绑定不符的消息进入有界隔离，不设金融incident。每笔提现隔离最多100份不同原文，
满额后拒收新增诊断，合法终局仍可处理。认证观测最多1000份；达到证据容量时明确保留摘要并
停止自动判定，不能假装证据完整。重复同一事件不会耗尽这份容量。

锁序为工作区共享屏障→支付商namespace锁→operation/payout/intent→hold→按UUID排序的余额。
namespace锁把同支付商的事件比较串行化，是明确的吞吐代价。跨工作区碰撞只写当前工作区的
incident，另一笔通过关联观测停止后续处理；不绕过另一个工作区的冻结屏障去改它的行。
对方导出只见本地引用和冲突摘要，不泄露另一工作区的请求/身份。已经在途的外部调用不被撤回。
工作区冻结固定的是该工作区的金融写入；其他工作区后来收到的全局支付商碰撞证据仍可能出现在
其关联诊断中。每次导出用自己的同一数据库快照读取它，不声称工作区屏障也冻结全局证据。

终结前检查源分录与当前借贷投影、活动hold总额和数值/version容量。商户后来disabled不否定
已发生的外部支付。明确的投影/容量障碍先回滚金融尝试，再单独留存证据与LOCAL_BLOCKER，
保留原资金状态并停止自动处理。任意SQL/COMMIT错误仍整体回滚并传播，不能吞成业务失败。
已产生的blocker不会因为修复投影就自动清除；受控重处理留给R5审批。活动hold修复后首次
正常收到成功证据仍可结算，二者不可混淆。

FROZEN/RESTORE不确认观测持久接收；调用者须重投或恢复后查询。v6报告的 `healthy` 不表示
所有提现已付款，须同时读取 `external_evidence` 中的未决、incident、blocker和已终结数量。
