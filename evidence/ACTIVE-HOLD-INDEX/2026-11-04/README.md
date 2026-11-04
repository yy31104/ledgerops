# 活动预留索引验收

当前切片用Git提交绑定，最终全量与Linux镜像证据另记。真实PostgreSQL64项通过，覆盖查询计划、新旧导出、备份、旧库关闭状态下的显式索引升级和原有并发提现。

默认优化器在50,000条终态/8条活动的物理样本中选中部分覆盖索引，共享块667→2，结果始终24；样本绕过业务图触发器且回滚，不冒充经过财务核验的业务历史。原始计划见`query-plan.json`。

新旧备份记录真实迁移清单；只有已知且摘要相符的011物理索引从财务能力清单中分离，未知迁移继续拒绝。旧恢复不自动授权或升级；`--index-only`不会GRANT CONNECT、改mode或restore generation。普通事务索引需要维护停写，不声称在线无锁。

日志和JUnit经`ops.evidence_redact`复制脱敏；原始临时文件不改。详细证据和边界见`verification.json`及`docs/ACTIVE-HOLD-INDEX.md`。
