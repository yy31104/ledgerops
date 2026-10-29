# 终局证据的受控重处理

R5-PR03只开放一种操作：`REPROCESS_TERMINAL_LOCAL_BLOCKER`。支付商已有经过认证的
SUCCEEDED、FAILED或CLOSED_ABSENT观测，但本地资金处理因LOCAL_BLOCKER停止；管理员完成
独立修复并发布新的R4结算证据后，两人复核，再用原观测重试本地结算。本入口不向支付商发请求。
UNKNOWN、矛盾证据和EVIDENCE_LIMIT不能用这个动作绕过。

## 用62说明完整流程

客户100，购买80后商户80。商户提现60、手续费2，预留62，可用18。支付商报告SUCCEEDED，
但本地结算失败；原观测、LOCAL_BLOCKER、hold和202全部保留。

1. 修复本地故障，不修改原观测或事故。重新导出并发布完整R4版本；付款成功的来源必须在实际
   支付商清单中出现，旧的空清单不能凭“版本是新的”授权。
2. 提议者提交提现、原终局观测、当前run UUID和理由。服务器读取金额、支付键、完整事故集合、
   目标账户/hold/观测版本，生成不可变提案及SHA-256。重复相同action ID保持原文和绝对期限。
3. 另一位actor读取提案并审批。提议者换token、换client仍不能自批；审批本身不转账。
4. 执行时重新检查期限、恢复代次、maker/checker当前权限版本、当前workspace证据epoch、
   所引用run和全部文件、source HMAC、当前完整目标和关联矛盾。全部成立才调用已有settle。
5. SUCCEEDED消费62，商户账面18、预留0、可用18，产生原有商户借/清算贷分录。
   FAILED/CLOSED_ABSENT只释放62，不产生付款分录。结算、执行结果、尝试和事故处置关系同事务提交。

原事故永不删除；`payout_incident_resolution`只追加本动作处理的精确LOCAL_BLOCKER。
worker和查询通过`unresolved_payout_incident`区分未处置与已处置；不能用最近32条摘要授权清除。

## 服务配置与接口

普通HTTP实例默认没有运维入口。仅在独立运维实例配置全部四项：

| 配置 | 要求 |
|---|---|
| LEDGEROPS_OPERATOR_DSN | 独立login，继承ledger_operator；拒绝超级用户、migrator、auth_admin和ledger_app成员 |
| LEDGEROPS_OPERATOR_OBJECTS | 已存在、非软链接的R4对象根目录；必须保留所引用全部文件 |
| LEDGEROPS_OPERATOR_NAMESPACE | 明确的支付商namespace UUID |
| LEDGEROPS_OPERATOR_KEYS_FILE | 所有者私有密钥文件；复用provider trust规则，不能从正文传入信任根 |

ledger_operator包含worker/reader/auth_reader的受信代码权限，不发给终端用户。
HTTP仍由Bearer的数据库映射决定workspace/actor/client和scope；数据库login不能从请求选择。
普通服务DSN仍按HTTP-SERVICE配置，公共实例不得挂载运维DSN和密钥。恢复物理开关阻止所有运维POST，
读取既有审计不受写开关影响。部署时将运维实例放在受限入口，不能靠前端隐藏按钮隔离权限。

| 接口 | 权限与内容 |
|---|---|
| POST /v1/operator/actions | approval:propose；精确字段action_id/action_type/payout_id/observation_id/evidence_run_id/reason/ttl_seconds |
| GET /v1/operator/actions/UUID | evidence:read；提案、审批、执行和最近20次尝试，附attempts_have_more |
| POST /v1/operator/actions/UUID/approve | approval:approve；仅proposal_sha256，actor必须不同 |
| POST /v1/operator/actions/UUID/execute | approval:execute；仅proposal_sha256和稳定execution_id |

新提案和新审批返回201，原身份重放200。执行成功200。409表示版本/证据等已变化；422表示此业务
不能执行。确定的409/422执行拒绝在金融事务回滚后，用新的受控事务保留稳定失败尝试。
未知数据库/COMMIT错误直接传播，不伪造一条“执行失败”；同一action/execution ID重查或重试。
一旦action已有成功执行，只返回保留结果，不能再次转账。

金融POST的Idempotency-Key不用于此处：提案身份是action_id，执行尝试身份是execution_id。
任何重试都保留原编号；改变提议内容需要新的action ID，不能把不同意图塞进旧身份。

## 页面审核

调查台按当前`/v1/me`权限开放控件；只有运维scope、没有accounts:read的用户也可以连接。
页面给出“提出→另一人读取原文→执行”的线性流程。金额与epoch摘要用字符串传输，
`proposal_json`保留精确canonical JSON字符串，不经JavaScript数字解析再序列化；浏览器计算原文字节
SHA-256，匹配后才能审批。提案哈希不是“人已读完”的证明；数据库仍执行全部当前性检查。

凭据、当前提案和执行编号只在页面内存中；断开/离开会清除，旧异步响应不能复活会话。
换成checker后需重新输入保留的action UUID，读取原提案。不要把审批token写进URL或文件分享。
浏览器自动化受当前Work设置限制：实际HTTP流程和脚本语法已验证，视觉与真实浏览器交互未验收。

## 锁、失效与资源边界

协作代码锁序：workspace共享屏障→registry→auth共享→provider namespace→action→原业务/payout/
intent→hold→按账户UUID排序的余额。冻结持有排他workspace屏障，等这些事务退出；外部付款是否停止
仍要另查支付商。当前实测覆盖两执行者、callback/跨workspace矛盾两种先后顺序和冻结等待，
不宣称任意未来SQL都无死锁；只重试确定中止的40P01/40001。

提案绑定workspace全局evidence epoch，另一日期的新发布也使其失效。目标余额花掉、观测增加、
恢复generation改变、maker/checker membership/client版本改变或DB期限到期，均要求重新审查。
单独轮换token不改变同一个人的权限版本；当前executor凭据每次仍须有效。

提案最多512KiB、完整LOCAL_BLOCKER最多100条，101条整体拒绝；单action最多100次执行尝试，
期限1–3600秒，页面15分钟。审核前核验引用对象总字节数最多64MiB，超限在读取前拒绝；
旧工作区导出仍有独立容量上限，分块导出在后续运行切片处理。没有自动GC或静默丢弃历史。

## 独立核对与不能证明的事

财务导出v9增加批准、执行与处置关系，report v3独立校验canonical SHA、distinct actor、精确
终局/effect/事故绑定；旧v1–v8保持原字节与能力。只有验证成立的LOCAL_BLOCKER处置才不再阻断。
导出不包含凭据、auth表或运维attempt理由的秘密配置。当前数据库里的原始请求仍不是外部原始意图的证明。

离线核对器没有权限历史/信任密钥，不能证明当时maker/checker确实有权，也不能声称HMAC已认证；
报告明确保留OPERATOR_HISTORICAL_AUTHORIZATION和PAYOUT_PROVIDER_AUTHENTICITY未核实项。
线上执行会使用实际当前权限和配置的信任根。有限故障测试不证明任意故障时序、银行终局或多地域HA。
