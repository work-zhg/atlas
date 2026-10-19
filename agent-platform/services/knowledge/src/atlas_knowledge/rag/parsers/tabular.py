"""XLSX / CSV：每个工作表一张表（表头为第一行）。

结构感知分段时，表格按「表头 + 每行一个单元」切，每行子块的向量前缀带表头；
普通分段时渲染成「"列名":"值";…」一行一条（与原解析器一致）。
"""

from __future__ import annotations

import csv
import io

from ..docir import Block, DocIR
from ..extractor.builtin import decode_text

__all__ = ["parse_csv", "parse_xlsx"]


def _clean_rows(rows: list[list[object]]) -> list[list[str]]:
    out = []
    for r in rows:
        cells = ["" if c is None else str(c).strip() for c in r]
        if any(cells):
            out.append(cells)
    return out


def parse_xlsx(data: bytes, filename: str = "") -> DocIR:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    blocks: list[Block] = []
    try:
        multi = len(wb.worksheets) > 1
        for ws in wb.worksheets:
            rows = _clean_rows([list(r) for r in ws.iter_rows(values_only=True)])
            if len(rows) < 2:
                continue
            if multi:
                blocks.append(Block(type="heading", text=ws.title, level=1))
            blocks.append(
                Block(
                    type="table",
                    text=ws.title,
                    table={"rows": rows, "header_rows": 1},
                    attrs={"sheet": ws.title},
                )
            )
    finally:
        wb.close()
    return DocIR(blocks=blocks, meta={"parser": "xlsx"}, report={"tables": len(blocks)})


def parse_csv(data: bytes, filename: str = "") -> DocIR:
    rows = _clean_rows([list(r) for r in csv.reader(io.StringIO(decode_text(data)))])
    if len(rows) < 2:
        return DocIR(meta={"parser": "csv"})
    return DocIR(
        blocks=[Block(type="table", table={"rows": rows, "header_rows": 1})],
        meta={"parser": "csv"},
        report={"tables": 1},
    )
