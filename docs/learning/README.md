# 从头顺读的LedgerOps教程

主要阅读文件：[PDF](../../output/pdf/LedgerOps-从零读懂账本.pdf)。
可搜索或编辑的正文：[Markdown](LEDGEROPS-TUTORIAL.zh-CN.md)。

正文依据a08f80c，按一条100/80的故事连续解释R1/R2。面向能读基础Python、
尚需串起SQL/事务/并发的读者；全部推演结果写在正文，不以操作网页为前提。
客户不用字母C作别名，避免与贷方C混淆；冲正ID用V1/V2，避免与阶段R1/R2混淆。
代码块分别注明教学简化或源码节选，不是一份待直接运行的脚本。

本次仅新增教程/排版工具并加阅读入口；生产代码、迁移、学习页面和项目依赖未改。
教程引用既有609项验收事实，没有声称为了文案重新跑过完整财务回归。

## 重新生成

使用带reportlab的文档Python环境运行，不向ledgerops运行时增加依赖。将该环境的解释器路径赋给`DOCUMENT_PYTHON`后运行：

```sh
"$DOCUMENT_PYTHON" docs/learning/render_pdf.py
```

默认读取同目录Markdown，输出到仓库output/pdf。默认使用macOS的STHeiti
Light/Medium与Menlo并嵌入PDF；其他环境可用脚本的字体参数指定可用字体。
源码中的代码版本由封面参数指定，默认a08f80c，代表教程解释的代码基线。

交付前检查：技术与初学者叙事复核；中文字体与搜索文本；目录/书签；全页PNG
排版检查；正文、代码、表格的页边界；短表完整、长表重复表头。临时预览在
被忽略的var/tutorial-pdf中，正式PDF和正文随本地文档提交保存。
