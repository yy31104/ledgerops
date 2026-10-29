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

单次离线CLI输出不是R4完整发布协议。后续持久run、完整组分区、原子current指针与证据epoch
由发布模块统一处理；不要把手工重定向的半个文件当成已发布的当前证据。
