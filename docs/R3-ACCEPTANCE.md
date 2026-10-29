# R3 本地验收

四个切片已完成：独立持久支付商、准入与hold/意图、认证观测与金融终结、有界后台进程及真实恢复。
最新完整回归 **1114 passed，188.67s，零失败/跳过**；Ruff、wheel/sdist、单独环境实际安装验证通过。
定位提交使用各片evidence文件的 `git log -1 --format=%H -- <path>`，不生成源码SHA清单。

| 原要求 | 已有证据 |
|---|---|
| 独立key/墓碑、PUT与close串行、provider提交前后杀进程 | R3-PR01 provider SQLite/HTTP真实进程测试与694项完整回归 |
| 80→80/62/18；202/hold/意图原子；固定截止与同身份重试 | PR02准入、claim、网络边界、截止前后、真实COMMIT丢失；900项回归 |
| hold在途投影漂移可修复，不改hold历史 | PR02实际维护；PR04修复后经真实provider结算，审计保留 |
| 成功18/0/18、clearing38；负面权威80/0/80且无journal | PR03终结、独立B/hold检查；PR04实际worker/provider路径 |
| 重复、乱序、ID换bytes、矛盾、无效签名、旧worker响应 | PR03真实PG与独立协议fixtures；一份终结和资金效果；1020项回归 |
| provider已付款但ledger worker死亡 | PR04在独立provider after_commit gate处SIGKILL；PG仍80/62/18，重启LOOKUP或滞后后同keyPUT，只一个外部效果/journal/消费 |
| 发送未到达、pending重启、永久暂不可用 | PR04真实截断server、双方重启与有限失败；有墓碑才释放，没有权威就保留HELD |
| 冻结排空和并发余额竞争 | PR04观测真实pg_blocking_pids；hold/冲正两种顺序、消费/购买、claim/observe与freeze，恢复后一次终结 |
| 防止旧备份自动派发 | PR04进程外restore/dispatch开关默认拒绝，DB为NORMAL也零出站；完整旧备份恢复仍属于R5 |
| 缺陷检测 | 8类命名语义变异：未知释放、换key、忽略旧矛盾、仅按eventID去重、漏reserved、绕屏障、两种金融事务分割。必须命中目标业务断言，非卡死/语法错误 |
| 运维进程边界 | 实际非高权限worker身份、schema能力和PG超时；SIGTERM停止新claim并清理，配置/不变量失败明确exit2，日志无秘密 |

证据：[PR01](../evidence/R3-PR01/2026-10-29/verification.json)、
[PR02](../evidence/R3-PR02/2026-10-29/verification.json)、
[PR03](../evidence/R3-PR03/2026-10-29/verification.json)、
[PR04](../evidence/R3-PR04/2026-10-29/verification.json)。

R3没有证明任意真实支付商的关单/负面终局保证、任意故障时序或真实电源/磁盘损坏。
恢复实验保留了本地准入与身份历史；旧备份丢失指令、外部已支付的恢复及不可恢复缺口在R5。
离线导出本身不能验证HMAC或实际银行付款；过程测试另外直接读独立SQLite事实。
namespace锁、导出行数/字节上限及记录的调度范围是真实边界，不外推吞吐、HA或上线资格。
实现、测试、用户个人掌握和生产环境激活分开记录；本轮继续R4–R6，没有推送或真实资金动作。
