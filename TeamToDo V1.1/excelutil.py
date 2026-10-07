#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TeamToDo Excel 工具（纯标准库实现）
==================================
不使用 openpyxl / xlrd 等第三方库，仅依赖 zipfile + xml，
保证在 Python 3.8.10 32 位 + PyInstaller 打包环境下可用（Windows 7 兼容）。

提供能力
--------
* write_xlsx(sheet_name, rows)  -> bytes   生成 .xlsx 文件二进制
* read_xlsx(data)               -> list[list[str]]  解析 .xlsx 为二维表格
* col_letter(n)                 -> str     列号转 A/B/C...AA
* xlsx_serial_to_date(v)        -> str     Excel 日期序列值转 YYYY-MM-DD
"""

import datetime
import re
import zipfile

try:
    import xml.etree.cElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET

__all__ = ["write_xlsx", "read_xlsx", "col_letter", "xlsx_serial_to_date",
           "ExcelError"]

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class ExcelError(Exception):
    pass


def col_letter(n):
    """1 -> A, 27 -> AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx_serial_to_date(v):
    """Excel 日期序列值（1900 基准）转 YYYY-MM-DD；失败返回空串"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    # Excel 的 1900 闰年 bug：序列 60 对应不存在的 1900-02-29
    if f >= 60:
        f -= 1
    try:
        d = datetime.date(1899, 12, 30) + datetime.timedelta(days=int(f))
    except (OverflowError, ValueError):
        return ""
    return d.strftime("%Y-%m-%d")


def _xml_escape(text):
    if text is None:
        return ""
    s = str(text)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace('"', "&quot;")
    # 去掉控制字符，避免 Excel 判定文件损坏
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s


def _is_number(v):
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    return False


def _cell_xml(ref, value):
    """生成单个单元格 XML，字符串用 inlineStr，数字用 v"""
    if value is None:
        value = ""
    if _is_number(value):
        return '<c r="%s"><v>%s</v></c>' % (ref, value)
    txt = _xml_escape(value)
    if txt == "":
        return ""
    return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, txt)


def write_xlsx(rows, sheet_name="Sheet1"):
    """rows: 二维列表（第一行通常为表头） -> xlsx 二进制内容"""
    if not rows:
        rows = [[""]]
    safe_name = re.sub(r"[\\/*?:\[\]]", "_", str(sheet_name or "Sheet1"))[:31] or "Sheet1"

    body_rows = []
    for r_idx, row in enumerate(rows, start=1):
        cells = []
        for c_idx, val in enumerate(row, start=1):
            cx = _cell_xml("%s%d" % (col_letter(c_idx), r_idx), val)
            if cx:
                cells.append(cx)
        body_rows.append('<row r="%d">%s</row>' % (r_idx, "".join(cells)))

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
        % (NS_MAIN, "".join(body_rows))
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="%s" xmlns:r="%s"><sheets>'
        '<sheet name="%s" sheetId="1" r:id="rId1"/>'
        '</sheets></workbook>' % (NS_MAIN, NS_REL_DOC, _xml_escape(safe_name))
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="%s">'
        '<Relationship Id="rId1" Type="%s/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="%s/styles" Target="styles.xml"/>'
        '</Relationships>' % (NS_REL_PKG, NS_REL_DOC, NS_REL_DOC)
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="%s">'
        '<Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>' % (NS_REL_PKG, NS_REL_DOC)
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="%s">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '</styleSheet>' % NS_MAIN
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )

    try:
        from io import BytesIO
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", content_types)
            z.writestr("_rels/.rels", root_rels)
            z.writestr("xl/workbook.xml", workbook_xml)
            z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
            z.writestr("xl/styles.xml", styles_xml)
            z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        return buf.getvalue()
    except Exception as e:
        raise ExcelError("生成 Excel 失败: %s" % e)


def _col_index(ref):
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(data):
    """解析 xlsx 二进制 -> 二维列表（字符串）；只取第一个工作表"""
    try:
        from io import BytesIO
        zf = zipfile.ZipFile(BytesIO(data))
    except Exception as e:
        raise ExcelError("无法读取 Excel 文件（请确认是 .xlsx 格式）: %s" % e)

    try:
        names = zf.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("{%s}si" % NS_MAIN):
                shared.append("".join(
                    t.text or "" for t in si.iter("{%s}t" % NS_MAIN)))

        sheet_name = None
        for cand in ("xl/worksheets/sheet1.xml",):
            if cand in names:
                sheet_name = cand
                break
        if sheet_name is None:
            for n in names:
                if n.startswith("xl/worksheets/") and n.endswith(".xml"):
                    sheet_name = n
                    break
        if sheet_name is None:
            raise ExcelError("Excel 文件中没有找到工作表")

        root = ET.fromstring(zf.read(sheet_name))
        rows = []
        sheet_data = root.find("{%s}sheetData" % NS_MAIN)
        if sheet_data is None:
            return rows
        max_col = 0
        raw_rows = []
        for row in sheet_data.findall("{%s}row" % NS_MAIN):
            cells = {}
            for c in row.findall("{%s}c" % NS_MAIN):
                ref = c.get("r") or ""
                idx = _col_index(ref)
                t = c.get("t")
                val = ""
                if t == "inlineStr":
                    is_node = c.find("{%s}is" % NS_MAIN)
                    if is_node is not None:
                        val = "".join(x.text or "" for x in is_node.iter("{%s}t" % NS_MAIN))
                else:
                    v = c.find("{%s}v" % NS_MAIN)
                    if v is None or v.text is None:
                        val = ""
                    elif t == "s":
                        try:
                            val = shared[int(v.text)]
                        except (ValueError, IndexError):
                            val = ""
                    else:
                        val = v.text
                if idx >= 0:
                    cells[idx] = (val or "").strip()
                    if idx + 1 > max_col:
                        max_col = idx + 1
            raw_rows.append(cells)
        for cells in raw_rows:
            rows.append([cells.get(i, "") for i in range(max_col)])
        # 去掉末尾全空行
        while rows and not any(x for x in rows[-1]):
            rows.pop()
        return rows
    except ExcelError:
        raise
    except Exception as e:
        raise ExcelError("解析 Excel 失败: %s" % e)
    finally:
        try:
            zf.close()
        except Exception:
            pass
