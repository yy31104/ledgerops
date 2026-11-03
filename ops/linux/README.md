# LedgerOps 本地 Linux 运行包

这份运行包面向一台受信机器上的合成服务。镜像只安装构建出的 wheel、单独的
`provider_sim` 和受控运维启动器；运行时不挂载仓库、测试或 lab。默认只向
`127.0.0.1` 发布 TLS，服务处于 RESTORE、金融写入关闭、worker 不启动。
当前实际平台是 Docker Desktop 的 Linux arm64 容器；其他架构和真实远程环境仍需各自验收。

构建固定官方 Python 3.12.15、PostgreSQL 18.6、Caddy 2.11.7 的镜像摘要，构建依赖和
运行依赖使用完整版本及下载哈希。`source-provenance.json` 只记录 Git 提交、dirty 标志和
基础镜像，不另建源码逐文件 SHA256 清单。镜像 ID 是构建产物身份，不能替代源代码审查。
Caddy 的官方二进制带有低端口 file capability；本包在构建时去掉该属性，运行时仍然
`cap_drop: ALL`，使用 8443/9443。[官方镜像](https://github.com/caddyserver/caddy-docker)
和[官方版本](https://github.com/caddyserver/caddy/releases/tag/v2.11.7)可核查上游来源。

## 构建、初始化和默认启动

需要可用 Docker Engine、Compose 和 Python 3.12。实际验收使用 Engine 29.4.3 / Compose 5.1.3。
在已提交且干净的仓库中执行，状态与构建目录必须是新的路径；状态文件不含数据库密码或 token。

```sh
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  build --source "$PWD" --context /protected/ledgerops/build-context
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json init
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json up --operator
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  ca --output /protected/ledgerops/local-ca.crt
curl --cacert /protected/ledgerops/local-ca.crt https://localhost:8443/health/live
curl --cacert /protected/ledgerops/local-ca.crt https://localhost:8443/health/ready
```

默认 live 返回 200，ready 返回 503。默认 `up` 也会停止此前明确启动的live worker；
省略 `--operator` 时停止此前的operator实例，不让旧进程沿用旧启用开关。`init` 是明确的一次性管理员作业：先初始化自己拥有的
卷和随机秘密，再以 UID999 启动 PostgreSQL，最后创建 NOLOGIN 权限组、受限登录和迁移。
请求服务启动不会迁移、创建角色或引导身份。重复初始化保留既有密码、provider namespace、
HMAC key、SQLite、数据库和对象；中断导致的部分秘密目录不会被自动覆盖或换钥。

Python 组件以 UID/GID10001 运行；秘密文件由该 UID 拥有、0600，分别放在能力专属卷。
API 不挂载 operator、worker、provider 或管理员秘密。实际登录 `ledger_service` 只有
app/reader/auth_reader；`ledger_action_operator` 才继承 operator 组所需 worker/reader/auth_reader。
迁移登录只在管理员作业使用，运行账户不能继承 schema owner。DSN、Bearer、HMAC 不进入
Compose environment、命令行、URL、浏览器存储或日志；私有 JSON 启动器在进程内装载允许的配置。

代理独占 ingress 网络并连接带固定受信 IP 的 private 网络；数据库、API、provider 留在
private internal 网络，数据库没有主机发布端口。9443 是单独 operator 实例，默认仍只绑定
主机 loopback。内部数据库流量是此受信单机网络上的 PostgreSQL/SCRAM；没有端到端网络 TLS 或
跨主机隔离的验收声明。Caddy 使用自己的本地 CA，客户端显式信任导出的 CA。
`skip_install_trust` 避免容器修改系统信任库；不使用 `-k` 或浏览器证书警告绕过。
[配置语义](https://caddyserver.com/docs/caddyfile/options#skip-install-trust)。

## 一次可丢弃的合成验收

以下步骤只在新建的 `ledgerops-test-*` 项目执行。`seed-synthetic` 明确创建合成 opening100
及受控客户/商户/maker/checker 身份。它不是正常服务的默认初始化数据。

```sh
python ops/linux/stack.py --project ledgerops-test-example --state /protected/test/stack.json \
  build --source "$PWD" --context /protected/test/context --https-port 18443 --operator-port 19443
python ops/linux/stack.py --project ledgerops-test-example --state /protected/test/stack.json init
python ops/linux/stack.py --project ledgerops-test-example --state /protected/test/stack.json seed-synthetic
python ops/linux/stack.py --project ledgerops-test-example --state /protected/test/stack.json up --operator
python ops/linux/acceptance.py --project ledgerops-test-example --state /protected/test/stack.json \
  --output /protected/test/linux-verification.json --dispose-owned-test-data
```

验收读取秘密到临时的本机 0600 文件，结束后删除，输出只保留非秘密证据。实际执行：UID、
mode0600、capabilities、no-new-privileges、只读 rootfs、资源上限、无仓库挂载；显式 CA 验证的
TLS purchase80、同键重放/改内容409；先观察 hold62，再启动真实 worker/provider 完成一次62的
终局，商户18/reserved0/available18；重启 provider+worker 后仍一笔 effect；独立账本 A/B；
数据库停机时 live200/ready503；私有 operator GET 在只读 ObjectStore 卷上返回404；日志脱敏；
有限 SIGTERM 停机；原生 pg_dump/pg_restore 和关闭的恢复目标。容量步骤通过显式私有管理员作业
接收129条各64KiB的无效观测，验证超过16MiB的完整stream10及超过32MiB的SQLite临时索引；
隔离且只读、无网络的consumer在4MiB tmpfs上必须返回SCRATCH_RESOURCE_LIMIT。该故障注入代码
只通过验收进程的stdin提供，既不安装在运行镜像，也不开放公开HTTP控制面。
随后同一大源做明确的管理员projection故障18→19，实际freeze/preview/apply/resume修回18，
核对完整format10、reportv4的EXCLUSIVE_WORKSPACE_BARRIER_READ_COMMITTED_V1捕获上下文、
原始hold与journal不变、稳定ID重放。原生备份/恢复必须继续验证持久化的resume_report v4。

worker 与 provider **只共享网络 namespace**。provider 始终绑定127.0.0.1:8765，既有
`ProviderHTTPClient` 的 loopback 限制没有放宽；worker 不挂载 provider 的 SQLite 卷。
重新创建 provider 时必须同时重新创建 worker，否则后者仍指向旧网络 namespace。
这是一项单机耦合限制，不是 HA 调度方案。

`--allow-dirty` 和 `--skip-backup-preliminary` 只供明确标记的开发检查，不能作为最终干净产物
或完整恢复的验收证据。小合成样例的内存峰值只说明该样例，没有构成容量、吞吐或SLO承诺。

## 资源、临时盘和清理

| 组件 | worker/进程配置 | memory | CPU | 可写空间 |
|---|---|---:|---:|---|
| API | 2 Gunicorn sync workers | 192MiB | 0.5 | 32MiB tmpfs |
| operator | 1 Gunicorn sync worker | 512MiB | 1 | 专用磁盘 scratch + 32MiB tmpfs |
| worker / provider | 各1个受控进程 | 各192MiB | 各0.5 | worker tmpfs；provider 独立 SQLite 卷 |
| PostgreSQL | max_connections40/shared_buffers32MiB | 256MiB | 1 | 独立 PG 卷 |
| reconciler | 显式单作业，默认1 worker | 512MiB | 1 | 独立磁盘 scratch/对象卷 |
| backup | 显式管理员作业 | 768MiB | 1 | 独立磁盘 scratch/备份卷 |
| proxy | Caddy | 96MiB | 0.25 | 独立 CA 卷 + 16MiB tmpfs |

每个容器还有 pids 上限、5MiB×2日志轮转、默认全部 capability 删除。API liveness 不访问数据库，
数据库不可用不会因 liveness 抖动触发进程重启；ready 检查实际 schema/角色与物理恢复配置。
停机给 Gunicorn45秒排空、容器50秒上限；这些期限不代表所有任意工作负载都已测过。

operator、reconciler、backup 和管理员 snapshot 检查使用独立的磁盘卷，并由启动器固定设置
`TMPDIR=/var/lib/ledgerops-scratch`。启动时至少需要2GiB可用磁盘；这是一项空闲空间检查，
**不是已预留的空间或 Docker local-volume quota**。stream consumer 的 SQLite 索引有512MiB自身
上限和2MiB缓存；额外的源文件/closure/SQLite临时文件仍需要磁盘。每个作业只运行一个大型检查，
监控 Docker 数据盘可用空间，达到阈值时保持写入/dispatch关闭再处理容量，不通过丢弃外部事实腾空间。

正常退出会清理自己创建的 `ledgerops-stream-*` 临时目录。SIGKILL/宿主故障可能留下目录。
清理前停止该专属卷的所有 operator/reconciler/backup/管理员作业，核实没有卷使用者，再仅删除
该 scratch 卷中已确认属于终止作业的临时目录。不能把相同规则用于不可变 ObjectStore、RUN pins、
provider历史、账本、备份或任何权威记录。磁盘满、权限错误或解析上限应失败并保留未决状态，不能
释放UNKNOWN的hold或绕过独立检查。具体大文件及ENOSPC证据以最终验收记录为准。

`down` 保留数据卷；`dispose-owned-test-data` 才明确删除当前项目的数据。删除前逐个核验所有
期望命名卷，以及项目容器/网络的 `com.docker.compose.project` 和 `io.ledgerops.scope` 标签；
仅有相同名字前缀或没有标签的已有卷会导致拒绝。不能用 `docker system prune` 代替这套清理。

```sh
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json down
# 仅用于自己刚创建且明确可丢弃的测试项目：
python ops/linux/stack.py --project ledgerops-test-example --state /protected/test/stack.json dispose-owned-test-data
```

## 备份、恢复及明确的重新开放决定

备份/恢复使用独立管理员镜像中的 PostgreSQL18.6原生工具，不挂 Docker.sock。
秘密只挂管理员服务；备份含身份敏感数据，不能放进公开证据交换、web目录或Git。
先停掉该namespace所有 admission/dispatch进程，再做恢复调查；禁止原库与恢复库同时派发。

```sh
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  backup backup /var/lib/ledgerops-backups/new-backup --objects /var/lib/ledgerops-objects
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  backup inspect /var/lib/ledgerops-backups/new-backup
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  backup --database postgres restore /var/lib/ledgerops-backups/new-backup \
  --database ledgerops_restore_example_20261029 --objects /var/lib/ledgerops-backups/new-objects \
  --restore-id RESTORE_UUID --admin-actor ADMIN_ACTOR_UUID --reason 'reviewed local recovery drill'
python ops/linux/stack.py --project ledgerops-local --state /protected/ledgerops/stack.json \
  backup --database ledgerops_restore_example_20261029 history \
  --provider-url http://127.0.0.1:8765 --namespace NAMESPACE_UUID \
  --keys-file /run/ledgerops/provider-keys.json \
  --history-file /var/lib/ledgerops-backups/new-signed-history.json
```

外层 `backup --database` 只选择管理员连接的数据库；内层 restore 的 `--database` 只允许新的
`ledgerops_restore_*` 目标。密码和数据库角色不由命令参数替换。原始命令/202归档与pg_dump来自
同一数据库快照，不是独立的指令归档。缺少受理前的请求键/原始202时，即便provider已支付，结果仍是
RESTORE_GAP，禁止猜造业务身份。恢复保持 runtime CONNECT关闭、workspace RESTORE、旧token撤销、
旧approval generation失效；不会自动重新启用HTTP或dispatch。

重新开放是**受信管理员的明确决定**，不是下面配置开关自动证明恢复完整。保留一份0600的恢复审查
记录，引用备份ID、restore_id、目标数据库、当前generation、签名history artifact、水位、独立报告和
管理员身份/理由。按以下有限步骤作出决定：

1. 核查备份完整性、对象清单和安装的迁移恰好匹配；不要在恢复库调用迁移入口，迁移设置会重新授权CONNECT。
2. 在runtime仍无有效CONNECT且没有外部会话时，获得最新完整的签名namespace历史和每笔LOOKUP；必须为
   RECOVERED，没有RESTORE_GAP、UNRESOLVED、BLOCKED、矛盾、证据限额或缺失验证key。发生任何新的外部
   事件后重新捕获，不把旧hash当作新鲜性证明。
3. 以关闭访问的管理员连接重新导出并独立验证structure/projection(A)/request-intent(B)，检查活动hold、
   原始ownership/202与终局effect。超过旧格式行数/16MiB能力时，必须使用完整format10验证并要求
   `verification_complete=true`；旧格式报错或不完整不能当作通过。保留报告；任一失败都继续关闭。
4. 核实 `auth_restore_workspace` 的 restore_id、current generation、previous_mode/previous_freeze_id
   与当前workspace吻合，所有旧凭据已撤销。通过现有身份管理Python API在该关闭的管理员连接上明确复核并
   引导新generation中的权限和token，保留稳定actor/client；不能复活旧token或自动照搬已过时的人类批准。
5. 对每个workspace取得排他workspace屏障，复核以上记录。若有仍未结束的 `current_freeze_id`，保留同一个指针，
   只把RESTORE改为FROZEN；提交后调用现有
   `resume_workspace(conn, workspace_id, same_freeze_id, admin_actor_id, reason)`，让它重新检查当前源并追加
   `resumed_at/resumed_by/resume_report`。不能删除指针、伪造已resume或直接跳NORMAL。
   若恢复审计记录此前是NORMAL且没有freeze，审查通过后可由该受信管理员在排他屏障内明确RESTORE→NORMAL；
   把决定与前后状态写入外部0600审查记录。此前就是RESTORE或记录不一致时继续关闭并调查。
6. mode处理完成后仍保持CONNECT关闭。明确评审需要哪些内部登录，再针对目标库授权CONNECT；例如API组
   `ledger_app,ledger_reader,ledger_auth_reader`。worker组单独开放。schema-owner/superuser管理员仍属于受信边界。
   修改新的私有profile只指向已审查的新库；先以RESTORE=true、writes=false启动，确认实际schema/角色和旧token失败。
   最后在另一次明确的本机决定中设置RESTORE=false、API_WRITES_ENABLED=true；仅对该已审查合成namespace再启动
   DISPATCH_ENABLED=true的worker。不能从备份manifest恢复这些物理开关。

第5步的受信管理员模式切换必须使用SQL参数和既有屏障，核心步骤如下；它本身不替代前4步：

```python
# admin 是当前关闭恢复库的闲置 autocommit 管理连接；值来自已保留的审查记录。
from ledgerops.authentication import _workspace_lock
from ledgerops.db.maintenance import resume_workspace
from ledgerops.backup import _closed_runtime

_closed_runtime(admin)
with admin.transaction():
    with admin.cursor() as cur:
        _workspace_lock(cur, workspace_uuid, shared=False)
        cur.execute(
            "SELECT mode,current_freeze_id FROM ledger.workspace WHERE id=%s FOR UPDATE",
            (workspace_uuid,),
        )
        mode, freeze_uuid = cur.fetchone()
        if mode != "RESTORE":
            raise ValueError("恢复库的当前mode必须是RESTORE")
        cur.execute(
            "SELECT a.previous_mode,a.previous_freeze_id,a.generation,s.generation "
            "FROM ledger.auth_restore_workspace a JOIN ledger.auth_workspace_state s "
            "USING(workspace_id) WHERE a.restore_id=%s AND a.workspace_id=%s",
            (restore_uuid, workspace_uuid),
        )
        audit = cur.fetchone()
        if audit is None or audit[2] != audit[3] or audit[1] != freeze_uuid:
            raise ValueError("restore审计、freeze和当前generation必须一致")
        if freeze_uuid is not None:
            if audit[0] != "FROZEN":
                raise ValueError("继续关闭并调查此前非FROZEN的freeze来源")
            cur.execute("UPDATE ledger.workspace SET mode='FROZEN' WHERE id=%s", (workspace_uuid,))
        else:
            if audit[0] != "NORMAL":
                raise ValueError("此前非NORMAL的无freeze恢复库继续关闭")
            cur.execute("UPDATE ledger.workspace SET mode='NORMAL' WHERE id=%s", (workspace_uuid,))
if freeze_uuid is not None:
    resume_workspace(admin, str(workspace_uuid), str(freeze_uuid), admin_actor_id, reviewed_reason)
_closed_runtime(admin)
# 将实际前后状态/独立报告/同一freeze的resume结果保留到0600审查记录，再单独决定CONNECT。
```

这段管理员操作可以绕过普通应用协议，因此它的证据与人类决定必须分别记录。它不是新的金融HTTP接口，
也不声称限制拥有数据库所有者权限的人。遇到提交响应丢失，保持物理开关关闭，先查询真实mode/freeze/audit再继续。

## worker 的停机取舍与值班处理

worker 遇到任一提现的 `InvariantViolation` 会记录 `worker_invariant_error` 并以退出码2停止整个
进程，因而同一进程负责的所有workspace都暂停派发。这是有意的完整性取舍。容器自动重启后，
如果原记录没有处理，可能再次遇到同一错误；不要把不停重启视为恢复，也不要删除原始指令、
事故、hold或修改已提交分录来跳过它。普通容量不足仍记录 `payout_blocked`，不会按不变量故障退出。

接入实际值班监控时，至少配置三类信号：

- 从worker结构化日志匹配 `event=worker_invariant_error`，立即告警并带上不透明
  `workspace_id/payout_id`；`worker_database_error`、非正常 `worker_stopped` 退出和持续容器重启
  是补充信号。不要把bearer、DSN或支付商原文转发到告警。
- 按每个workspace使用具备 `evidence:read` 的凭据轮询 `GET /v1/metrics`，跟踪
  `unresolved_count`、`oldest_unresolved_seconds` 和 `blocked_count`。持续有未决且年龄增长、
  `payout_attempt` 消失，说明派发可能停止；健康的HTTP存活探针不能证明worker仍在工作。
- `blocked_count` 只统计持久 `dispatch_intent.state='BLOCKED'`；它不保证包含令worker退出的
  不变量错误。即使该值为0，也不能忽略上述错误日志。阈值和通知接收方需由实际部署设置，
  此本地包没有替用户安装监控或值班服务。

收到告警后：先关闭该部署的dispatch开关并停止worker，保留日志、镜像身份和数据库备份；
用只读调查入口及同快照导出定位记录，并独立核对结构、A/B和hold。外部结果未知时保留hold，
按原provider key查询证据，不新发另一个提现。若属于支持的终局 `LOCAL_BLOCKER`，按现有
当前证据与不同actor的maker/checker流程处理；其他数据不一致需修复原因、复现测试并审查，
不能伪装成可自动跳过的业务拒绝。确认独立核对通过、未决外部事实得到解释后，再明确重新开启
worker，观察该记录与其余workspace的实际推进。

候选发现按workspace交错，每个workspace先取一笔，再取第二笔；运行中的worker用循环UUID游标
轮转下一批的起点，即使上一批全部因容量不足跳过，也会前进。这样A的21笔受阻提现不会挤掉
同批B的一笔；超过batch_size个workspace也会在后续扫描获得机会。批大小和网络调用仍有界，
容量跳过不会写transport_attempt、改变knowledge或发送请求。游标仅属于本进程，重启后从头开始；
该策略保证跨workspace轮转，不承诺同一workspace中每笔受阻工作的持久退避或任意负载下的延迟SLO。

## 升级和回滚

先保留备份和当前镜像ID，再关闭dispatch/金融写入、排空服务，使用明确迁移管理员作业应用新迁移；
历史迁移字节不能修改。新镜像的readiness核对实际迁移。保存上一版状态文件和镜像ID以便回退配置。
没有新迁移且旧代码与当前schema兼容时，可以换回已验证的旧镜像；涉及迁移时不能假装down-migration安全。
需要恢复旧数据库时只能恢复到新库，并重新走上面的provider领先/缺失历史/身份generation/冻结检查。

备份加密、异机保留、磁盘/宿主故障隔离、自动排程、容量告警和真实域名证书尚需实际部署环境配置。
本包没有开启任何远程端点、真实银行、云服务或多机HA。
