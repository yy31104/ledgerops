# 服务身份与当前权限

凭据是256位随机不透明字符串，服务器只保存SHA-256摘要。每个凭据绑定workspace、稳定client、
actor、显式scope、期限和恢复generation；Principal由这些记录派生。请求里的actor/角色不能授权。
凭据文件只能保存到受保护路径，不进Git、日志、URL或浏览器持久存储。

## 数据库身份

管理员先建立NOLOGIN组`ledger_auth_reader`、`ledger_auth_admin`和实际登录账号，再执行迁移009。
应用服务登录账号继承`ledger_app`、`ledger_reader`、`ledger_auth_reader`；身份管理员登录账号
只继承`ledger_auth_admin`。两者都应是NOSUPERUSER/NOBYPASSRLS/NOCREATEDB/NOCREATEROLE/NOREPLICATION，
不属于ledger_migrator。应用可以读取凭据摘要来验证身份，不能管理凭据或更改授权。
身份管理员可以维护当前身份/授权，不能改金融余额。迁移账号和恢复管理员另行保管。

这是受信服务代码使用的数据库角色，不直接分发给终端用户。原有Python API接受受信Principal；
HTTP适配器必须始终传入认证模块的guard，不能接受客户端选择guard或省略guard。
管理员直接用SQL绕过管理helper属于另一个受信边界，不能声称协作式锁能限制数据库所有者。

## 引导、轮换和撤销

设置`LEDGEROPS_AUTH_ADMIN_DSN`指向专用身份管理员账号。命令会拒绝普通应用和超级用户登录，
不在命令行传DSN/密码。actor必须是实际业务所有者或运维身份，client是稳定的调用应用身份。
首次创建不传expected-version，修改已存在的membership/client须传当前version，防止覆盖别人变更。

```sh
python -m ledgerops.auth_cli membership --workspace "$WORKSPACE_ID" --actor "$ACTOR_ID" \
  --admin-actor "$ADMIN_ACTOR_ID" --reason 'initial customer grant' --scope purchase:write
python -m ledgerops.auth_cli client --workspace "$WORKSPACE_ID" --actor "$ACTOR_ID" \
  --client "$CLIENT_ID" --admin-actor "$ADMIN_ACTOR_ID" --reason 'initial client'
python -m ledgerops.auth_cli issue --workspace "$WORKSPACE_ID" --client "$CLIENT_ID" \
  --admin-actor "$ADMIN_ACTOR_ID" --reason 'credential rotation' --scope purchase:write \
  --ttl-seconds 3600 --output /absolute/private/new-credential.json
python -m ledgerops.auth_cli revoke --workspace "$WORKSPACE_ID" --credential "$CREDENTIAL_ID" \
  --admin-actor "$ADMIN_ACTOR_ID" --reason 'credential retired'
```

issue的目标文件必须不存在；以0600独占创建并fsync，只在文件里写明文token，stdout只返回凭据ID、
期限和路径。每client最多20个未过期未撤销凭据，TTL为1秒至30天。中断可能留下已签发但不可用的
凭据；通过身份审计找到它并撤销，不会由此发生金融动作。文件写失败会尝试撤销本次凭据。
轮换保留原client ID，所以原Idempotency-Key仍处于同一命名空间，不会因为换token重复扣款。

scope必须同时出现在凭据和当前membership里；提高membership权限不会扩大旧token的scope。
禁用member/client、撤销凭据、过期或恢复generation改变都会拒绝继续使用。
管理审计追加保存操作、管理员数据库账号/actor、理由和前后状态，包含凭据ID而没有明文token。

## 撤销与交易的先后

仅在HTTP入口验证一次存在竞态：验证后到提交前可能撤权。金融入口在自己的READ COMMITTED
事务里，先拿共享workspace屏障，再拿共享auth屏障，检查当前凭据/权限，然后才处理业务键和资金。
购买、冲正、提现分别由核心固定检查purchase:write、reversal:write、payout:write；每次完整事务重试
重新验证。管理变更拿同workspace屏障和排他auth屏障，因此先获得锁的一方决定这次的顺序。
交易先到，撤销等它提交；撤销先到，交易在写入身份前失败。过期检查使用DB决策时钟；
不承诺“在COMMIT瞬间过期会撤销已经受理的交易”。重放原结果也需要当前权限。

只读查询用独立连接先取得session共享auth锁，再开始RR READ ONLY事务并复核权限。
这样不会先固定旧快照、等撤销提交后还读旧权限。查询持锁期间撤销等待，结束或异常时解锁并关闭连接。
该上下文拥有传入的连接，不应把已关闭连接返还池中。锁等待/语句和事务有界，HTTP服务还需外层资源限制。
`/v1/me`仅返回当前公开身份/scope/期限，允许没有accounts:read的运维用户连接；
账户和金融查询仍分别检查固定scope，金融guard不允许缺失scope。

## 恢复旧备份

恢复旧数据库可能复活已撤销token和旧审批。因此开放runtime连接之前，恢复管理员执行
`invalidate_restored_access`：排空写入、设置RESTORE、撤销旧凭据、生成新generation并追加审计。
actor/client和旧历史保留，重新引导新凭据。相同restore ID幂等重试不会撤销首次恢复后新建的凭据。
旧FROZEN状态的维护记录和current freeze引用保留，v8核对器能表示RESTORE中保留的freeze；
这不代表该freeze已恢复营业。之前的mode/freeze引用也留在恢复审计里。

独立进程的恢复/禁止派发配置仍是必要条件，不能从备份中的NORMAL或新token推断可以营业。
HTTP服务见HTTP-SERVICE.md；双人受控重处理与审批失效见OPERATOR-ACTIONS.md。
实际pg_dump恢复仍单独验收。金融导出v8声明009，v9声明010并增加批准/执行/处置关系；
凭据与身份管理审计始终排除，旧v1–v8保留原契约。
