"""测试用 PDF 样本（reportlab 生成；中文用内置的 STSong-Light CID 字体，不依赖字体文件）。"""

from __future__ import annotations

import io

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
FONT = "STSong-Light"
W, H = A4
BODY = "本手册说明公司的基本制度与员工的权利义务，所有员工应当认真阅读并遵守相关规定。"


def _header_footer(c: canvas.Canvas, page: int) -> None:
    c.setFont(FONT, 9)
    c.drawString(60, H - 30, "星海科技 内部资料 请勿外传")
    c.drawCentredString(W / 2, 25, f"第 {page} 页")


def _column(c: canvas.Canvas, x: float, y: float, lines: list[str], size: float = 10.5) -> float:
    c.setFont(FONT, size)
    for line in lines:
        c.drawString(x, y, line)
        y -= size * 1.5
    return y


def _table(c: canvas.Canvas, x: float, top: float, rows: list[list[str]]) -> float:
    col_w, row_h = 150.0, 20.0
    c.setFont(FONT, 10)
    for r, row in enumerate(rows):
        y = top - r * row_h
        for k, cell in enumerate(row):
            c.rect(x + k * col_w, y - row_h, col_w, row_h)
            c.drawString(x + k * col_w + 5, y - 14, cell)
    return top - len(rows) * row_h


def _png(w: int = 240, h: int = 160) -> ImageReader:
    img = Image.new("RGB", (w, h), (40, 120, 200))
    for i in range(0, w, 20):
        for j in range(h):
            img.putpixel((i, j), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def employee_handbook() -> bytes:
    """3 页：页眉页脚、三级标题、双栏正文、跨页续表、图片 + 图注。"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    # ── 第 1 页：标题（通栏）+ 一级章标题 + 双栏正文
    _header_footer(c, 1)
    c.setFont(FONT, 22)
    c.drawCentredString(W / 2, H - 80, "员工手册")
    c.setFont(FONT, 16)
    c.drawString(60, H - 120, "第一章 总则")
    left = ["左栏甲：员工应遵守考勤制度。", *[BODY[:20], BODY[20:40]] * 4]
    right = ["右栏乙：请假需提前在系统申请。", *[BODY[:20], BODY[20:40]] * 4]
    _column(c, 60, H - 150, left)
    _column(c, W / 2 + 20, H - 150, right)
    c.showPage()

    # ── 第 2 页：二级标题 + 正文 + 表格（延续到下一页）
    _header_footer(c, 2)
    c.setFont(FONT, 13)
    c.drawString(60, H - 70, "1.1 适用范围")
    y = _column(c, 60, H - 95, [BODY, "本制度适用于全体正式员工与实习生。"])
    _table(
        c,
        60,
        y - 20,
        [["假期类型", "天数", "审批人"], ["年假", "10", "部门经理"], ["病假", "按实际", "人事"]],
    )
    c.showPage()

    # ── 第 3 页：续表（重复表头）+ 图片 + 图注 + 正文
    _header_footer(c, 3)
    bottom = _table(
        c,
        60,
        H - 60,
        [["假期类型", "天数", "审批人"], ["婚假", "3", "部门经理"], ["产假", "158", "人事"]],
    )
    c.drawImage(_png(), 60, bottom - 200, width=240, height=160)
    c.setFont(FONT, 10)
    c.drawString(60, bottom - 220, "图 1 组织架构")
    _column(c, 60, bottom - 250, ["第二章 考勤", "员工每日应在九点前到岗打卡。"])
    c.showPage()
    c.save()
    return buf.getvalue()


def scanned() -> bytes:
    """一页只有一张铺满的图片，没有文本层（模拟扫描件）。"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawImage(_png(600, 800), 0, 0, width=W, height=H)
    c.showPage()
    c.save()
    return buf.getvalue()


def partly_scanned() -> bytes:
    """第 1 页有文本，后两页只有图片（部分扫描件：能入库，但应进待审核）。"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _column(c, 60, H - 80, ["报销制度", BODY, "差旅报销需要上传发票。"])
    c.showPage()
    for _ in range(2):
        c.drawImage(_png(600, 800), 0, 0, width=W, height=H)
        c.showPage()
    c.save()
    return buf.getvalue()
