# 认证 HTTP 服务与调查台

服务入口是`python -m ledgerops.service`（或`make service`），由锁定的Gunicorn26.2.0运行。
`lab/`仍是独立学习工具；真实服务不会创建测试身份、重置账本或自动运行演示。
本页描述当前本地接口；受控审批见[操作契约](OPERATOR-ACTIONS.md)，Linux部署/实际恢复另行验收。

## 配置与启动

先用管理员建立数据库/角色并执行迁移010，按AUTHENTICATION.md签发当前身份凭据。
服务登录账号须继承ledger_app/ledger_reader/ledger_auth_reader，禁止超级用户、migrator、
auth_admin或worker成员。每次数据库连接验证角色和本地SQL迁移版本/摘要，错误配置不就绪。

| 配置 | 默认/意义 |
|---|---|
| LEDGEROPS_SERVICE_DSN | 必填，秘密外置，禁止放在命令行/Git |
| LEDGEROPS_ENV | production；development仅允许127.0.0.1明文调试，身份鉴权始终真实 |
| LEDGEROPS_TLS_TERMINATED | production必须显式true，表示已经配置可信TLS入口 |
| LEDGEROPS_SERVICE_BIND | 127.0.0.1:8080；production可显式0.0.0.0以接入私有代理网络 |
| LEDGEROPS_SERVICE_HOSTS | localhost,127.0.0.1；最多16个明确主机名，无通配符 |
| LEDGEROPS_TRUSTED_PROXIES | 127.0.0.1,::1；最多16个明确代理IP，无任意来源信任 |
| LEDGEROPS_SERVICE_WORKERS | 2，范围1–4，每worker同步处理一个请求 |
| LEDGEROPS_RESTORE_MODE | true；开启时金融写入和回调均拒绝 |
| LEDGEROPS_API_WRITES_ENABLED | false；必须显式true且恢复模式false才能写入 |
| LEDGEROPS_PAYOUT_POLICY_FILE | 可选，所有者私有常规JSON文件；缺失时拒绝新的提现入口 |
| LEDGEROPS_CALLBACK_DSN/NAMESPACE/KEYS_FILE | 可选、三项须完整；单独worker账号和签名信任，未配置则无回调入口 |
| LEDGEROPS_OPERATOR_DSN/OBJECTS/NAMESPACE/KEYS_FILE | 可选、四项须完整；独立operator账号/对象存储/签名信任，未配置则无运维入口 |

production的业务页面/API必须经HTTPS访问。Gunicorn只接受配置过的代理IP提供的
X-Forwarded-Proto:https；普通客户端自报头不能授权身份或TLS。不要将后端明文端口暴露公网。
TLS_TERMINATED是对部署者配置的明确要求，不是服务替你检查证书/域名的证明。

本地明文开发须显式`LEDGEROPS_ENV=development`并使用loopback；仍需真实数据库凭据和签发token。
这不会关闭鉴权，也不会注入fixture。恢复/写入标志不从数据库NORMAL推断，恢复旧备份不会自动开放。

提现政策文件精确包含`version:1`和`workspaces`数组，每项字段：workspace_id、provider_namespace、
fixed_fee_minor、dispatch_ttl_seconds、beneficiary_owners（beneficiary UUID→owner actor UUID）。
费用与收款方归属来自管理员文件，正文不能替代政策。文件拒绝软链接/FIFO、其他所有者或组/世界权限，
最大64KiB；最大128工作区、每项1000收款方。签名key文件复用worker的私有文件校验，最多8个key。

## API

所有/v1业务接口使用`Authorization: Bearer <token>`，workspace/client/actor从凭据派生。
金融POST还必须有`Idempotency-Key`，HTTP边界限定1–128个ASCII字母、数字或`._~-`。
这比内部Python RequestKey允许的字符范围窄，避免重复HTTP头被合并后产生含糊的新身份。
请求体严格UTF-8 JSON，拒绝重复字段、非有限数字、未知业务字段；金额必须是最小单位十进制字符串。
外层正文最大16KiB，Content-Length必填，不接受Transfer-Encoding。业务身份不含凭据或传输重试信息。

| 路由 | 当前权限/结果 |
|---|---|
| GET /health/live | 无需凭据；进程存活，不因数据库短暂故障改为失败 |
| GET /health/ready | 实际角色/SQL能力和物理开关；未就绪503，响应不含秘密/工作区清单 |
| POST /v1/purchases | purchase:write，201成功/422业务拒绝；正文沿PurchaseCommand |
| POST /v1/reversals | reversal:write，原商户全额冲正；正文沿ReversalCommand |
| POST /v1/payouts | payout:write，政策匹配后202受理/冻结资金；202不是支付成功 |
| GET /v1/payouts/UUID | payout:read，原商户归属检查和单快照当前状态/证据引用 |
| GET /v1/operations/UUID | operations:read，仅本client提交过的业务原回执；GET200内保留原http_status |
| GET /v1/me | 当前有效凭据；仅公开身份、有效scope、期限和是否启用运维面板，不要求账户读取权限 |
| GET /v1/accounts?after=UUID | accounts:read，仅actor自有账户；每页100、明确next_after |
| GET /v1/investigation/operations/UUID | evidence:read，工作区内调查；原请求摘要/分录/hold/最近证据引用 |
| GET /v1/metrics | evidence:read，工作区未决数量/年龄、BLOCKED派发记录和隔离数量 |
| /v1/operator/actions及子路由 | 独立配置才启用；proposal/approval/execute各有固定scope，见操作契约 |

调查记录最多100条分录；观察20条、事件32条，明确have_more，不能把“最近”说成全历史。
金融金额、版本、处理尝试数和外部来源序号用字符串传输，避免浏览器浮点整数精度丢失。数量/行号/HTTP状态为小整数。
内部一致、存有摘要或受理202都不证明外部支付已经完成。UNKNOWN/ACCEPTED仍须继续调查。

401表示无当前凭据，403表示scope不足，404不披露未授权对象，409表示身份与内容冲突，
422是稳定业务结论，503表示暂不可用/恢复未开放/可能需要用原身份恢复。
断线可能发生在COMMIT之后：重试必须保留原operation ID和Idempotency-Key，不生成新业务身份。
核心只对确定已中止的40P01/40001重试，HTTP层不自动重试结果未知的交易。

## 回调

配置完整时，`POST /v1/provider/callbacks/<workspace UUID>/<payout UUID>`接受已有provider-event:v1
签名envelope，不使用普通用户Bearer。先验证精确payload字符串的HMAC域，再连接数据库；
无效签名401，不查询目标、不写隔离。通过签名后仍复用R3完整身份/金额/namespace绑定、证据留存、
矛盾阻断和一次性结算。外层JSON重排不改变签名涵盖的payload字节；不会拿重新生成的业务内容验证签名。
受理返回202，status区分APPLIED/REPLAYED/INCIDENT/BLOCKED等；收到回调不等于无条件付款成功。
恢复物理开关同样阻断回调金融写入。回调连接仅使用独立worker角色，不扩大普通用户服务角色。

## 调查台

根路径`/`提供捆绑的调查台：连接身份、账户、按业务UUID调查和未决状态查询。
当前结算run/epoch引用来自同一授权快照，最多20条，明确have_more；它们不证明对象文件已经核验。
独立运维实例还提供提案→另一人核对精确原文→执行面板，只有配置和当前scope均允许才启用控件。
凭据只存页面内存，输入框在连接时清空；退出/刷新/离开页面后清除。异步会话代次阻止退出后旧响应
重新显示数据。界面只用textContent，不执行记录中的HTML；没有外部脚本、浏览器持久存储或操作回调。
原文`proposal_json`以字符串保留canonical字节，浏览器SHA-256核验后绑定其摘要，避免大整数重算漂移。
静态资源仅三个固定路径，拒绝任意文件读取。CSP、自身来源、nosniff和no-store作为默认响应头。
本次实际浏览器自动化被Work设置关闭；真实HTTP资产/API和node语法检查通过，视觉/交互需后续实际浏览器验收。

## 资源与停机

固定1–4同步workers、backlog64；请求行4094、头数量32、每头2048字节；每次请求只持有本次连接，
连接3秒、语句10秒、锁5秒、事务30秒、事务空闲15秒。Gunicorn请求40秒、graceful45秒；
SIGTERM停止接新请求并允许有界在途链结束。启动时输出真实worker初始化事件供运行监控。
源代码不会用每请求无限线程、无界连接池或进程内后台支付任务。

JSON请求日志写服务器request ID、固定route/status与耗时，以及成功结果中经过UUID格式校验的
operation/action/execution/observation关联编号；不写任意正文/路径。关闭原始access log，HTTP解析警告/异常
使用固定事件，避免头、URL查询、DSN、payload和密钥泄露。解析器在进入应用前的400/431可能是服务器标准错误页。
指标取当前授权工作区数据库，数量不会把多个worker的内存计数冒充全局统计。
当前工作区导出/证据容量边界仍按R4文档执行；本片没有生产吞吐、HA或任意容量保证。

主要实现依据：Gunicorn26.2.0实际安装代码与[官方配置](https://gunicorn.org/reference/settings/)、
[锁定发行版](https://pypi.org/project/gunicorn/26.2.0/)，数据库期限按PostgreSQL18实际服务器验证。
