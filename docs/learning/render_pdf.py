"""Render the linear Chinese tutorial with the bundled document runtime.

This is an authoring utility, outside the ledgerops package. It deliberately
accepts only the Markdown constructs used by the accompanying book: headings,
paragraphs, simple lists/tables, blockquotes, and fenced code blocks.
"""

from __future__ import annotations

import argparse
import html
import re
import unicodedata
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    HRFlowable,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    XPreformatted,
)
from reportlab.platypus.tableofcontents import TableOfContents

ROOT = Path(__file__).resolve().parents[2]
INK = colors.HexColor("#203b34")
TEXT = colors.HexColor("#283a36")
MUTED = colors.HexColor("#61736c")
ACCENT = colors.HexColor("#2b6855")
LINE = colors.HexColor("#d8e2dc")
PAPER = colors.HexColor("#f2f6f2")
CODE_PAPER = colors.HexColor("#f1f4f2")
PAGE_W, PAGE_H = A4
MARGIN = 52
WIDTH = PAGE_W - 2 * MARGIN


def normalize(text: str) -> str:
    return text.translate({code: "-" for code in range(0x2010, 0x2016)})


def fonts(regular: Path, bold: Path, mono: Path) -> None:
    for name, path in (("Book", regular), ("BookBold", bold), ("Code", mono)):
        pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=0))
    pdfmetrics.registerFontFamily(
        "Book", normal="Book", bold="BookBold", italic="Book", boldItalic="BookBold"
    )
    pdfmetrics.registerFontFamily(
        "Code", normal="Code", bold="Code", italic="Code", boldItalic="Code"
    )


def font_runs(text: str, *, code: bool = False) -> str:
    """Keep Latin code monospaced while embedding CJK comments legibly."""
    output = []
    font = None
    chunk = []
    for char in normalize(text):
        chosen = "Code" if code and ord(char) < 128 else "Book"
        if chosen != font and chunk:
            output.append(f'<font name="{font}">{html.escape("".join(chunk))}</font>')
            chunk = []
        font = chosen
        chunk.append(char)
    if chunk:
        output.append(f'<font name="{font}">{html.escape("".join(chunk))}</font>')
    return "".join(output)


_INLINE = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)]+)\)")


def inline(text: str) -> str:
    output, last = [], 0
    for match in _INLINE.finditer(normalize(text)):
        output.append(html.escape(normalize(text)[last : match.start()]))
        code, bold, label, target = match.groups()
        if code is not None:
            output.append(f'<font size="10.4" color="#315e50">{font_runs(code, code=True)}</font>')
        elif bold is not None:
            output.append(f"<b>{html.escape(bold)}</b>")
        elif target.startswith(("http://", "https://")):
            output.append(
                f'<link href="{html.escape(target, quote=True)}" color="#2b6855">'
                f"{html.escape(label)}</link>"
            )
        else:
            # Source references remain readable when the PDF is moved elsewhere.
            output.append(html.escape(label))
            if target != label:
                output.append(f" <font color='#61736c'>({html.escape(target)})</font>")
        last = match.end()
    output.append(html.escape(normalize(text)[last:]))
    return "".join(output)


def styles() -> dict[str, ParagraphStyle]:
    base = dict(fontName="Book", textColor=TEXT, wordWrap="CJK", splitLongWords=True)
    return {
        "body": ParagraphStyle(
            "BookBody",
            **base,
            fontSize=11.4,
            leading=19.4,
            spaceAfter=9,
            allowWidows=0,
            allowOrphans=0,
        ),
        "lead": ParagraphStyle(
            "BookLead",
            **base,
            fontSize=12,
            leading=21,
            spaceAfter=13,
        ),
        "h2": ParagraphStyle(
            "BookH2",
            fontName="BookBold",
            textColor=INK,
            fontSize=20,
            leading=29,
            spaceBefore=18,
            spaceAfter=14,
            wordWrap="CJK",
            keepWithNext=1,
        ),
        "h3": ParagraphStyle(
            "BookH3",
            fontName="BookBold",
            textColor=INK,
            fontSize=13.5,
            leading=21,
            spaceBefore=14,
            spaceAfter=8,
            wordWrap="CJK",
            keepWithNext=1,
        ),
        "h4": ParagraphStyle(
            "BookH4",
            fontName="BookBold",
            textColor=INK,
            fontSize=11.8,
            leading=19,
            spaceBefore=11,
            spaceAfter=6,
            wordWrap="CJK",
            keepWithNext=1,
        ),
        "quote": ParagraphStyle(
            "BookQuote",
            **base,
            fontSize=11.1,
            leading=19,
            leftIndent=12,
            rightIndent=12,
            borderColor=LINE,
            borderWidth=0.5,
            borderPadding=10,
            backColor=PAPER,
            spaceBefore=6,
            spaceAfter=13,
        ),
        "table": ParagraphStyle(
            "BookTable",
            **base,
            fontSize=10.4,
            leading=16,
            spaceAfter=0,
        ),
        "table_head": ParagraphStyle(
            "BookTableHead",
            fontName="BookBold",
            textColor=colors.white,
            fontSize=10.2,
            leading=16,
            wordWrap="CJK",
        ),
        "code": ParagraphStyle(
            "BookCode",
            fontName="Code",
            textColor=INK,
            fontSize=9.7,
            leading=15,
            alignment=TA_LEFT,
            leftIndent=0,
            rightIndent=0,
            borderPadding=0,
            spaceBefore=0,
            spaceAfter=0,
        ),
        "code_caption": ParagraphStyle(
            "BookCodeCaption",
            fontName="Book",
            textColor=MUTED,
            fontSize=8.4,
            leading=13,
            spaceBefore=5,
            spaceAfter=4,
            keepWithNext=1,
        ),
        "list": ParagraphStyle(
            "BookList",
            **base,
            fontSize=11.2,
            leading=19,
            leftIndent=14,
            firstLineIndent=-10,
            spaceAfter=6,
            allowWidows=0,
            allowOrphans=0,
        ),
        "toc": ParagraphStyle(
            "BookToc",
            fontName="Book",
            textColor=TEXT,
            fontSize=11.2,
            leading=20,
            leftIndent=0,
            rightIndent=20,
            spaceBefore=6,
            wordWrap="CJK",
        ),
    }


def code_lines(text: str, maximum: float = WIDTH - 25) -> str:
    lines = []
    for original in normalize(text).splitlines():
        current, used = "", 0.0
        indent = len(original) - len(original.lstrip())
        for char in original.expandtabs(4):
            font = "Code" if ord(char) < 128 else "Book"
            width = pdfmetrics.stringWidth(char, font, 9.7)
            if used + width > maximum and current:
                lines.append(font_runs(current, code=True))
                current = " " * min(indent + 2, 8)
                used = pdfmetrics.stringWidth(current, "Code", 9.7)
            current += char
            used += width
        lines.append(font_runs(current, code=True))
    return "\n".join(lines or [" "])


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def text_width_hint(value: str) -> float:
    return sum(1.0 if unicodedata.east_asian_width(c) in "WF" else 0.55 for c in value)


def markdown_blocks(text: str, style: dict[str, ParagraphStyle]) -> list:
    lines = text.splitlines()
    result, index = [], 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.startswith("<!--"):
            while index < len(lines) and "-->" not in lines[index]:
                index += 1
            index += 1
            continue
        if line.startswith("```"):
            language = line[3:].strip()
            index += 1
            code = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            caption = {"python": "PYTHON", "sql": "SQL", "text": "推演 / 结构"}.get(
                language, language.upper() or "代码"
            )
            code_box = Table(
                [[XPreformatted(code_lines("\n".join(code)), style["code"])]],
                colWidths=[WIDTH],
                hAlign="LEFT",
            )
            code_box.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), CODE_PAPER),
                        ("LEFTPADDING", (0, 0), (-1, -1), 10),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                        ("TOPPADDING", (0, 0), (-1, -1), 10),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                    ]
                )
            )
            result.extend([Paragraph(caption, style["code_caption"]), code_box, Spacer(1, 10)])
            index += 1
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            level, title = len(heading[1]), heading[2]
            if level == 1:  # The first H1 is rendered on the cover.
                index += 1
                continue
            result.append(CondPageBreak(135 if level == 2 else 78))
            paragraph = Paragraph(inline(title), style[f"h{level}"])
            if level <= 3:
                paragraph._outline_level = level - 2
                if level == 2:
                    paragraph._toc_level = 0
            result.append(paragraph)
            index += 1
            continue
        if (
            "|" in line
            and index + 1 < len(lines)
            and re.fullmatch(r"\s*\|?[\s:|\-]+\|?\s*", lines[index + 1])
            and "-" in lines[index + 1]
        ):
            rows = [table_cells(lines[index])]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(table_cells(lines[index]))
                index += 1
            columns = len(rows[0])
            if any(len(row) != columns for row in rows):
                raise ValueError(f"Uneven Markdown table near {rows[0]!r}")
            weights = [
                max(7, min(23, max(text_width_hint(row[col]) for row in rows)))
                for col in range(columns)
            ]
            widths = [WIDTH * weight / sum(weights) for weight in weights]
            data = [
                [
                    Paragraph(inline(cell), style["table_head" if n == 0 else "table"])
                    for cell in row
                ]
                for n, row in enumerate(rows)
            ]
            table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), INK),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PAPER]),
                        ("LINEBELOW", (0, 0), (-1, 0), 0.7, INK),
                        ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 7),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ]
                )
            )
            table_group = [Spacer(1, 5), table, Spacer(1, 13)]
            # Keep the worked numeric examples together; long reference tables
            # still split and repeat their header on subsequent pages.
            keep_example = len(rows) <= 6 or rows[0][0] == "步骤"
            result.extend([KeepTogether(table_group)] if keep_example else table_group)
            continue
        if line.startswith(">"):
            quote = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote.append(lines[index].lstrip()[1:].strip())
                index += 1
            result.append(Paragraph(inline(" ".join(quote)), style["quote"]))
            continue
        if re.match(r"^[-*]\s|^\d+[.)]\s", line):
            marker = "- "
            match = re.match(r"^(\d+)[.)]\s+(.*)", line)
            body = line[2:].strip()
            if match:
                marker, body = match[1] + ". ", match[2]
            result.append(Paragraph(inline(marker + body), style["list"]))
            index += 1
            continue
        if line in {"---", "***", "___"}:
            result.extend([Spacer(1, 5), HRFlowable(width="100%", color=LINE), Spacer(1, 12)])
            index += 1
            continue
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip():
            following = lines[index].strip()
            if re.match(r"^#{1,4}\s|^```|^>|^[-*]\s|^\d+[.)]\s", following):
                break
            if "|" in following and index + 1 < len(lines) and "---" in lines[index + 1]:
                break
            paragraph.append(following)
            index += 1
        result.append(Paragraph(inline("".join(paragraph)), style["body"]))
    return result


class BookTemplate(BaseDocTemplate):
    def beforeDocument(self):
        self._heading_index = 0
        self._chapter_seen = False

    def afterFlowable(self, flowable):
        if not hasattr(flowable, "_outline_level"):
            return
        level = flowable._outline_level
        if level == 1 and not self._chapter_seen:
            return
        self._heading_index += 1
        key = f"section-{self._heading_index}"
        title = flowable.getPlainText()
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(title, key, level, closed=False)
        if level == 0:
            self._chapter_seen = True
        if hasattr(flowable, "_toc_level"):
            self.notify("TOCEntry", (flowable._toc_level, title, self.page, key))


def furniture(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(MUTED)
    canvas.setFont("Book", 8.4)
    canvas.drawString(MARGIN, PAGE_H - 31, "LedgerOps / 顺读教程")
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 31, "从一笔购买，读到并发与恢复")
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.4)
    canvas.line(MARGIN, PAGE_H - 40, PAGE_W - MARGIN, PAGE_H - 40)
    canvas.line(MARGIN, 40, PAGE_W - MARGIN, 40)
    canvas.drawString(MARGIN, 25, "R1 / R2 · 合成资金 · 本地学习文档")
    canvas.drawRightString(PAGE_W - MARGIN, 25, str(doc.page))
    canvas.restoreState()


def cover_furniture(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(PAPER)
    canvas.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    canvas.setFillColor(ACCENT)
    canvas.rect(MARGIN, PAGE_H - 92, 36, 4, stroke=0, fill=1)
    canvas.setFont("Book", 9)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 40, "LEDGEROPS  /  PROJECT READING GUIDE")
    canvas.restoreState()


def render(source: Path, output: Path, revision: str = "a08f80c") -> None:
    text = source.read_text(encoding="utf-8")
    match = re.search(r"^#\s+(.+)$", text, flags=re.M)
    title = match[1] if match else "LedgerOps：从零读懂一个账本后端"
    style = styles()
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = BookTemplate(
        str(output),
        pagesize=A4,
        rightMargin=MARGIN,
        leftMargin=MARGIN,
        topMargin=54,
        bottomMargin=54,
        title=title,
        author="LedgerOps 学习教程",
        pageCompression=1,
    )
    frame = Frame(
        MARGIN,
        54,
        WIDTH,
        PAGE_H - 108,
        id="body",
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    doc.addPageTemplates(
        [
            PageTemplate(id="Cover", frames=[frame], onPage=cover_furniture),
            PageTemplate(id="Body", frames=[frame], onPage=furniture),
        ]
    )
    cover_title = ParagraphStyle(
        "CoverTitle",
        fontName="BookBold",
        fontSize=29,
        leading=43,
        textColor=INK,
        wordWrap="CJK",
        spaceAfter=24,
    )
    cover_subtitle = ParagraphStyle(
        "CoverSubtitle",
        fontName="Book",
        fontSize=14,
        leading=25,
        textColor=MUTED,
        wordWrap="CJK",
        spaceAfter=16,
    )
    cover_meta = ParagraphStyle(
        "CoverMeta",
        fontName="Book",
        fontSize=10.5,
        leading=19,
        textColor=MUTED,
        wordWrap="CJK",
    )
    toc = TableOfContents()
    toc.levelStyles = [style["toc"]]
    toc.dotsMinLevel = 0
    cover_text = inline(title)
    if title.startswith("LedgerOps："):
        cover_text = "<font size='22'>LedgerOps</font><br/>" + inline(title.split("：", 1)[1])
    story = [
        Spacer(1, 120),
        Paragraph(cover_text, cover_title),
        Paragraph("一份不用找按钮、从头顺着读的中文教程", cover_subtitle),
        Spacer(1, 12),
        HRFlowable(width=80, thickness=1.3, color=ACCENT, hAlign="LEFT"),
        Spacer(1, 24),
        Paragraph(
            "从客户余额100开始，把购买、幂等、并发、冲正、核对和重建串成一个故事。每一步先解释问题，再读代码，推演结果全部写出。",
            cover_subtitle,
        ),
        Spacer(1, 38),
        Paragraph(
            "适合：能读基础Python，正在补SQL、事务与并发。<br/>"
            f"代码参照：{html.escape(revision)} · R1 / R2<br/>"
            "正文可独立阅读，无需启动网页或数据库。",
            cover_meta,
        ),
        NextPageTemplate("Body"),
        PageBreak(),
        Paragraph("阅读路线", style["h2"]),
        Paragraph(
            "从开头顺读即可。目录和PDF书签可用于第二遍查找；第一次阅读不要求跳转源码、安装工具或操作网页。",
            style["lead"],
        ),
        toc,
        PageBreak(),
    ]
    story.extend(markdown_blocks(text, style))
    doc.multiBuild(story)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=ROOT / "docs/learning/LEDGEROPS-TUTORIAL.zh-CN.md"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "output/pdf/LedgerOps-从零读懂账本.pdf"
    )
    parser.add_argument(
        "--regular-font", type=Path, default=Path("/System/Library/Fonts/STHeiti Light.ttc")
    )
    parser.add_argument(
        "--bold-font", type=Path, default=Path("/System/Library/Fonts/STHeiti Medium.ttc")
    )
    parser.add_argument("--mono-font", type=Path, default=Path("/System/Library/Fonts/Menlo.ttc"))
    parser.add_argument("--revision", default="a08f80c")
    args = parser.parse_args()
    fonts(args.regular_font, args.bold_font, args.mono_font)
    render(args.source, args.output, args.revision)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
