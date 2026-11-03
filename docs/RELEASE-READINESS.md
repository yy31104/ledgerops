# LedgerOps 本地交付与运行入口

本项目已从单条记账示例扩展为带认证、异步提现、独立核对、受控人工操作和恢复工具的服务。
运行包仍只使用合成支付商，在本机 Linux 容器中验收；没有把真实资金、银行终局或云端运行当作
本地测试能证明的事情。最终命令、测试结果、镜像身份和清理记录见
[统一验收](../evidence/DELIVERY/2026-10-29/verification.json)。

## 已经交付的完整流程

| 部分 | 能做什么 | 关键边界 |
|---|---|---|
| R1/R2 | 购买、并发防透支、原身份重试、冲正、A/B核对、冻结排空和投影重建 | SQL约束不能单独证明余额和归属正确，仍由独立核对发现偏差 |
| R3 | 持久提现指令、hold、保守派发、认证观测、未知结果恢复和关单墓碑 | 202只表示受理；时间过去或NOT_FOUND都不能释放hold |
| R4 | 固定结算来源、完整组匹配、不可变run、全块pin与原子current发布 | 对账发现差异，不自动修账；不完整来源不能变成健康结果 |
| R5 | 当前权限鉴权、真实HTTP服务、调查台、不同actor审批、备份恢复 | 旧权限/证据/版本失效；恢复默认不写账、不派发 |
| R6 | 选定合成证据和来源、严格版本/单位、独立标准库读入器 | 只读，无数据库凭据或运维修正能力；Chronicle应用未在此仓库集成 |
| 运行配套 | 固定镜像/依赖、非root、TLS、角色隔离、资源限制、优雅停机 | 单机本地范围；真实环境需要自己的身份、网络和支付商契约 |

用同一组数字理解：客户100买80后，客户20、商户80；商户提现60、手续费2时预留62，
可用18。只有经认证的SUCCEEDED才消费62并记本地分录；负终局释放hold；UNKNOWN继续保留。
如果支付商已成功而本地崩溃，恢复读取既有外部证据，沿原身份结算，不重新付款。

## 从哪里开始运行

源码开发与测试需要Python3.12、uv和Docker：

```sh
make bootstrap
make check
make test
make build
```

首次运行可能下载锁定依赖和PostgreSQL镜像。Docker不可用时集成测试明确失败。
`make test`中的demo/lab子进程明确复用正在运行pytest的解释器环境，不依赖外层设置
`UV_NO_SYNC`、`UV_PROJECT_ENVIRONMENT`或预先填充仓库离线缓存；先按上面执行`make bootstrap`即可。
测试只创建自己的临时数据库/容器；`make lab`仍是独立学习工具，不能当生产接口。
从零理解可读[原有中文教程](learning/LEDGEROPS-TUTORIAL.zh-CN.md)。

需要真实容器部署时，先读[Linux运行手册](../ops/linux/README.md)。以下是可直接替换路径的
本地合成验收示例，目录需要是新的；`--dispose-owned-test-data`会明确删除这个测试项目的数据。
源仓库必须已提交且干净。秘密只进入专属卷和临时0600文件，不打印到终端。

```sh
python ops/linux/stack.py --project ledgerops-test-local \
  --state $WORKSPACE_ROOT/ledgerops-local-run/stack.json \
  build --source $REPO_ROOT \
  --context $WORKSPACE_ROOT/ledgerops-local-run/build \
  --https-port 18443 --operator-port 19443
python ops/linux/stack.py --project ledgerops-test-local \
  --state $WORKSPACE_ROOT/ledgerops-local-run/stack.json init
python ops/linux/stack.py --project ledgerops-test-local \
  --state $WORKSPACE_ROOT/ledgerops-local-run/stack.json seed-synthetic
python ops/linux/stack.py --project ledgerops-test-local \
  --state $WORKSPACE_ROOT/ledgerops-local-run/stack.json up --operator
python ops/linux/acceptance.py --project ledgerops-test-local \
  --state $WORKSPACE_ROOT/ledgerops-local-run/stack.json \
  --output $WORKSPACE_ROOT/ledgerops-local-run/verification.json \
  --dispose-owned-test-data
```

验收会显式开启自己的合成派发，执行完自动关停清理。普通默认启动保持RESTORE、ready503，
金融写入关闭且worker不启动；这不是程序故障。需要保留数据的普通`down`不删除卷。
不要用`docker system prune`清理项目，也不要把验收里的故障注入用于真实数据。

## 大工作区如何继续核对和维护

旧财务格式1–9仍支持，原来的10,000行/表和16MiB上限保持。新的format10通过有界游标和分块
完整导出，以磁盘SQLite独立核对；不靠截断或忽略隔离记录缩小数据。CLI须显式选择：

```sh
python -m ledgerops.cli export --format stream10 --workspace WORKSPACE_UUID \
  --output-root PRIVATE_EXPORT_ROOT --code-revision COMMITTED_SOURCE_SHA
python -m ledgerops.cli verify --format stream10 PRIVATE_EXPORT_UUID_DIRECTORY \
  --scratch-root PRIVATE_DISK_SCRATCH
```

受保护的`LEDGEROPS_EXPORT_DSN`须由运行环境装载；不要把密码写进命令行或Git。
[R4与审批接入](STREAMING-EVIDENCE.md)、[完整源格式](STREAM10.md)和
[大型维护](MAINTENANCE-CAPACITY.md)说明限制与操作方式。
维护自动保留小数据旧路径，大数据改用排他屏障内的完整磁盘核验。维护预览明确标识
READ COMMITTED捕获上下文，不能冒充只读RR来源进入R4或R6。

实测覆盖约17MiB来源、129条64KiB隔离记录和超过10,000请求键。声明的源上限为256MiB/
100万行、单行4MiB、SQLite主索引512MiB、语义闭包16MiB；额外临时文件仍需要磁盘。
超限或不完整语义检查会明确失败，不能返回健康。这些是工程拒绝上限，不能当吞吐或SLO承诺。

## 恢复、身份与人工操作

- [认证与撤销](AUTHENTICATION.md)：身份从当前凭据映射；金融事务内复核，旧业务重放也不能绕过撤权。
- [受控重处理](OPERATOR-ACTIONS.md)：不同actor审批，绑定当前证据epoch、目标版本、恢复代次、期限和完整事故集合。
- [备份恢复](BACKUP-RESTORE.md)：真实pg_dump/pg_restore，同快照业务档案、对象清单、旧凭据/审批失效。
- [HTTP接口](HTTP-SERVICE.md)：业务接口与学习lab分离，普通API与运维实例使用不同能力配置。
- [只读交换](EVIDENCE-EXCHANGE.md)：离线读入与明确的未知外部时间/COMMIT时间，无操作回调。

备份早于受理、且缺少独立命令档案时，工具报告RESTORE_GAP。它不会根据支付商记录猜造原请求键
或原202，更不会自动再付款。恢复后CONNECT、写入和派发仍关闭；重新开放需要手册中的明确检查。
同一数据库里的业务档案不能代替独立耐久命令档案；数据恢复耗时不是已经恢复营业的RTO。

## 上线声明的实际范围

已测试的运行环境是本机Docker中的Linux arm64，使用真实PostgreSQL和独立持久SQLite支付商。
没有远程部署、真实支付商适配、真实IdP、主机丢失容灾或HA验收。非root、TLS和有限故障测试
不会自动证明这些能力。不同actor分离依赖身份管理员正确映射现实人员，不能阻止受信管理员串通。

工作区导出能核对保留的原始请求，却不能证明最初保存的请求就没有错误；历史权限和provider HMAC
在无信任密钥的离线核对中仍明确标为未核实。Hash是完整性证据，不是来源签名。
当前Work设置禁止浏览器自动化，HTTP接口/静态资源/脚本已经检查，网页视觉和实际浏览器交互未验收。
代码实现、验证范围、真实上线以及个人掌握分别记录，不能互相代替。
