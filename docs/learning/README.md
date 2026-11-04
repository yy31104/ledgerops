# LedgerOps reading tutorial

The [original Chinese tutorial](LEDGEROPS-TUTORIAL.zh-CN.md) explains purchases,
transactions and concurrency through the 100/80 example. It describes the R1/R2
source baseline recorded in the tutorial; payout and service work came later.
Code blocks distinguish teaching simplifications from source excerpts.

Generated PDFs are local output and are not tracked in Git. To render a copy,
set DOCUMENT_PYTHON to a Python environment with ReportLab installed, then run:

```sh
"$DOCUMENT_PYTHON" docs/learning/render_pdf.py --output output/pdf/ledgerops-tutorial.pdf
```

This does not add ReportLab to the application dependencies. The renderer defaults
to macOS STHeiti and Menlo fonts; use its font arguments on other platforms.
The tutorial source, renderer and recorded code baseline remain unchanged.
