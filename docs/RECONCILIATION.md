# 结算文件与离线核对

这一层回答“本地记账与支付商自己的结算记录是否一致”，不执行退款、释放预留或改账。
购买属于内部转账，没有外部结算行；提现本金60、手续费2应对应支付商一笔gross62效果。

## 先取得支付商证据

模拟支付商使用自己的SQLite文件。结算分配只指定已有支付编号和业务时间，金额/商户/收款方
从它自己的持久请求和effects读取；不能用账本导出来生成“外部证据”。

批次JSON四个字段：`batch_id`、`effective_at`（UTC秒格式）、`fee_rule_version`（quoted_fee_v1）、
`rows`。每个row仅含`row_id`和明确的`provider_keys`数组。UUID与key须取实际已存在的对象。
一个批次只能有一个workspace，每行只能属于同一商户/币种；同一支付编号最多分配一次。
同一组允许拆行，每行都须明确成员。以下命令在仓库运行，秘密通过环境注入：

```sh
python -m provider_sim.settlement \
  --database /absolute/path/provider.sqlite3 \
  --namespace "$PROVIDER_NAMESPACE" --key-id "$PROVIDER_KEY_ID" \
  assign --batch-file /absolute/path/batch.json

python -m provider_sim.settlement \
  --database /absolute/path/provider.sqlite3 \
  --namespace "$PROVIDER_NAMESPACE" --key-id "$PROVIDER_KEY_ID" \
  statement --workspace "$WORKSPACE_ID" --date 2026-10-29 --version 1
```

`LEDGEROPS_PROVIDER_HMAC_KEY`须与该已有支付商文件的签名配置一致。路径错误明确失败，
不会默默创建一个空数据库并签发“没有支付”的结算单。输出JSON可保存为statement文件。
这条本机管理员CLI不是公网权限接口；独立provider_sim不进入正式ledgerops wheel。

同workspace/日期/时区的版本从1递增；重复请求旧版本返回原字节。后来补到的结算只出新版本。
两套固定水位分别描述执行事件和结算分配；新增结算不会增加执行事件水位。

## 固定本地导出后离线比较

先按 [导出文档](EXPORT-AND-VERIFY.md) 保存同一个REPEATABLE READ快照。
签名验证文件为key ID→十六进制密钥的JSON映射，仅当前所有者可读；不要提交到Git。
支持最多8个可信key ID，namespace必须显式指定。

```sh
python -m ledgerops.recon_cli match \
  --ledger-export /absolute/path/exports/EXPORT_UUID \
  --statement /absolute/path/statement.json \
  --namespace "$PROVIDER_NAMESPACE" \
  --keys-file /absolute/path/private/provider-keys.json
```

命令不需要数据库连接。源码checkout会记录实际HEAD和dirty状态；从已安装wheel运行时，
必须另传 `--code-revision` 的40位实现提交，开发状态另加 `--dirty`。
来源导出的代码版本和本次匹配器版本分开记录，不能拿旧导出的版本冒充新算法版本。

stdout为JSON报告；exit0表示本报告范围内reconciled，exit1表示输入可用但有差异/未决，
exit2表示输入或配置不可用。运行脚本必须检查退出码，不能把“文件里有JSON”当通过。
缺失/软链接/FIFO/超限输入明确失败；不截断后返回健康结果。

## 怎样读报告

- `usable`说明输入能否安全解释；`reconciled`是本次明确范围内的结果，不是全系统结算完成保证。
- `issues`按稳定规则排列，先看`category`和对象引用，再看expected/actual。数额相同但商户不同
  仍是WRONG_OWNER；未知状态不能因经过了一段时间就变成成功或失败。
- `groups`保留完整组键、成员与本金/手续费/gross；缺少成员信息是AMBIGUOUS_GROUP，不猜配。
- `matched_provider_keys`与组级匹配都必须考虑相关矛盾；不能只看合计相等。
- `unsettled_provider_keys`列出完整支付商effects索引中尚未分配结算的编号；有这些编号时
  `settlement_complete=false`且`reconciled=false`。单笔资金字段可以匹配，但不声称已经结算。
  因为没有执行时间，不能把未分配编号硬归到某一天，也不能编造未来结算时间。
- `internal_verification`附独立结构/A/B核对。它与外部statement匹配有不同能力，不能互相替代。
- `inputs`固定原始字节摘要/长度、各自版本与水位、业务窗口和匹配器版本，供重现和后续发布。

全部差异类别：MISSING_INTERNAL、UNEXPECTED_INTERNAL、DUPLICATE_EFFECT、WRONG_OWNER、
WRONG_AMOUNT、CURRENCY_MISMATCH、MISSING_PROVIDER、UNEXPECTED_PROVIDER、TIMING_ONLY、
FEE_MISMATCH、AMBIGUOUS_GROUP、INPUT_CONTRADICTION、CORRUPT_INPUT。
这些是调查分类，不是自动认定哪一方经济事实必然为真。

## 时间与范围

完整effects索引比较的是固定workspace/namespace输入里的资金与身份，日期窗口比较的是当天结算组。
报告分开写inventory_scope与settlement_scope，历史差异不冒充今日新差异。
业务日用Europe/Warsaw的本地日历算UTC半开窗口，夏令时切换日可能23或25小时。

R3事件没有实际执行时间，结算模块没有补猜。`effective_at`是支付商管理员分配的结算生效时间，
`recorded_at`是分配留档时间；HTTP收到时间不代替两者。本地journal.recorded_at是本地记账时间。
TIMING_ONLY必须有双方完全一致的经济字段及明确不同窗口；支付商已付而本地只有HELD仍报告
MISSING_INTERNAL，不能用“可能是异步”把它隐藏。

HMAC证明签名配置一致，不证明支付商永不撒谎、数据库从未丢记录或原始请求捕获完全正确。
两份独立存储导出不是分布式原子快照。当前边界为每账本表10000行、账本包16MiB；statement
最多10000效果/成员、1000行、签名包16MiB。超限报错，吞吐与容量以实际测量为准。

## 发布、查询与恢复中断

`match`仍是无需数据库的单次离线报告。`publish`使用迁移008的专用
`ledger_reconciler`登录角色和独立对象目录；角色没有余额、分录、hold写权限。
通过环境变量`LEDGEROPS_RECON_DSN`注入凭据，不把DSN放在命令行参数或Git中。

```sh
python -m ledgerops.recon_cli publish \
  --ledger-export /absolute/path/exports/EXPORT_UUID \
  --statement /absolute/path/statement.json \
  --namespace "$PROVIDER_NAMESPACE" \
  --keys-file /absolute/path/private/provider-keys.json \
  --objects /absolute/path/evidence-objects \
  --run-id "$RUN_UUID" --workers 2 --partitions 16 --timeout-seconds 300
```

先生成并保存本次run UUID。它标识一次固定输入和计划，和金融业务operation ID分开。
相同run仅允许相同字节/版本/计划重试；不同输入必须新建run。`--workers`只允许1、2、4，
分区计划不随进程数变化。工作进程不持有数据库连接或发布权限，只接收已经认证的事实和计划；
父进程复核每份结果并发布。它们是同一操作系统用户下的可信代码，不是安全沙箱。

`publish` exit0只说明本次完整发布协议完成，不表示报告没有差异。返回`published=false`
表示本次run完整，但同scope的其他run先更新了current；不会偷偷重试覆盖它。完整对象固定保留，
current按workspace/namespace/日期划分，workspace的evidence epoch在每次成功更新时递增。

查询时改用专用`ledger_reader`连接（同名环境变量），按固定run读取，或显式选择当前scope：

```sh
python -m ledgerops.recon_cli read \
  --objects /absolute/path/evidence-objects --workspace "$WORKSPACE_ID" --run-id "$RUN_UUID"

python -m ledgerops.recon_cli read \
  --objects /absolute/path/evidence-objects --workspace "$WORKSPACE_ID" \
  --current --namespace "$PROVIDER_NAMESPACE" --date 2026-10-29
```

读者先持久固定run引用，再复核全部引用文件。`is_current_at_pin`、`evidence_epoch_at_pin`
仅描述取得引用的那个时点；读取期间后来发布新报告不会改写已固定的内容。
`published_epoch`是该run当初发布时的历史值，不是“永远有效”的证明。
读取exit0/1分别表示报告范围内通过/存在差异；坏文件、未完成run、错误权限等返回exit2，
不自动换一份报告掩盖问题。正常结束或读取失败会释放reader pin；stderr打印reader ID，
进程或连接异常中断留下的pin可在确认读者停止后显式释放：

```sh
python -m ledgerops.recon_cli release-pin --workspace "$WORKSPACE_ID" --reader-id "$READER_UUID"
```

任何工作进程退出、超时或结果不完整都会终止本次工作进程组，尝试将run记为FAILED。
数据库断线可能发生在COMMIT之后，此时`outcome_unknown=true`不等于回滚；按返回run ID
重新读取状态，再用原始输入/计划恢复，不能凭错误响应换身份覆盖当前证据。
SIGINT/SIGTERM会清理本次拥有的工作进程；强杀父进程不能保证执行清理，残留STAGING不作完整证据。

文件先写入私有临时文件并fsync，再在registry锁下原子改名、同步目录、登记引用。
这不是数据库与文件系统的分布式事务；注册前崩溃可能留下未引用文件。没有自动GC，
也不能按文件年龄删除对象。备份须同时保留注册表、引用对象和密钥引用；清理未知文件前
停止所有发布者，确认它既无持久引用也无pin。损坏对象应从经过核验的备份恢复，不能覆盖为新含义。

迁移008后的金融导出是v7，保留v6的金融行结构；发布对象通过这里的固定run读取。
原有v1–v6导出仍可核对，不能只把旧文件的版本号改为7。
