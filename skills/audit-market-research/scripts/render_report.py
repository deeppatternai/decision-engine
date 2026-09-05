#!/usr/bin/env python3
"""Deterministic DOCX report generator for /audit-market-research.

Reads the house style from `assets/report_style.json` (user-editable, ships with
the skill — see SKILL.md "DOCX report table style") and a structured report JSON,
and emits a .docx with the style applied DETERMINISTICALLY — so the format is
identical on every machine, no agent-interpretation drift (改造方案
2026-05-27, Owner spec: 换机器一字不差). Style lives in the JSON (client-side,
user-editable); this script just applies it.

Usage:
    python render_report.py <report.json> <out.docx> [style.json]

report.json schema (generic; the skill maps its insight envelope into this):
    {
      "title": "...",
      "subtitle": "...",                  # optional (e.g. "Generated ... | Sources N")
      "sections": [
        {
          "heading": "1. ...",            # optional
          "paragraphs": ["...", "..."],   # optional
          "table": {"headers": ["A","B"], "rows": [["1","2"], ...]}   # optional
        }
      ]
    }
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Twips

DEFAULT_STYLE = Path(__file__).resolve().parent.parent / "assets" / "report_style.json"
_PAGE_CONTENT_TWIPS = 9360  # US-Letter (12240) minus 1in left+right margins


def _norm_hex(color_hex):
    """Normalize a user-entered hex color to bare RRGGBB for OOXML use.

    report_style.json is USER-EDITABLE, so users naturally write web-style hex.
    Both RGBColor.from_string and the raw w:fill / w:color OOXML attributes want
    bare 6-digit RRGGBB — a leading '#' raises ValueError in the former and is
    silently invalid in the latter. So strip a leading '#' and expand 3-digit
    CSS shorthand ("#FFF" -> "FFFFFF"). Any other length is returned as-is and
    left to RGBColor.from_string to accept or reject (fail loud, not silent).
    """
    if not color_hex:
        return color_hex
    h = color_hex.lstrip("#")
    if len(h) == 3:  # CSS shorthand: #FFF -> FFFFFF
        h = "".join(c * 2 for c in h)
    return h


def load_style(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _tcpr(cell):
    return cell._tc.get_or_add_tcPr()


def _cell_valign_center(cell) -> None:
    tcpr = _tcpr(cell)
    v = tcpr.find(qn("w:vAlign"))
    if v is None:
        v = OxmlElement("w:vAlign")
        tcpr.append(v)
    v.set(qn("w:val"), "center")


def _cell_margins(cell, top, bottom, left, right) -> None:
    tcpr = _tcpr(cell)
    mar = OxmlElement("w:tcMar")
    # start/end (and left/right aliases) so it renders in both LTR Word + older readers
    for tag, val in (("top", top), ("bottom", bottom),
                     ("start", left), ("end", right), ("left", left), ("right", right)):
        e = OxmlElement(f"w:{tag}")
        e.set(qn("w:w"), str(int(val)))
        e.set(qn("w:type"), "dxa")
        mar.append(e)
    tcpr.append(mar)


def _cell_shading(cell, fill_hex) -> None:
    tcpr = _tcpr(cell)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), _norm_hex(fill_hex))
    tcpr.append(shd)


def _no_table_borders(table) -> None:
    tblpr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{edge}")
        e.set(qn("w:val"), "nil")
        borders.append(e)
    tblpr.append(borders)


def _style_run(run, style, *, size_pt, bold=False, color_hex=None) -> None:
    font = run.font
    font.name = style["font"]["body_latin"]
    font.size = Pt(size_pt)
    font.bold = bold
    if color_hex:
        font.color.rgb = RGBColor.from_string(_norm_hex(color_hex))
    # CJK font via eastAsia (so 中文 uses LiSong Pro, not the Latin face)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), style["font"]["body_cjk"])


def _add_para(doc_or_cell_para, style, text, *, size_pt=None, bold=False, color_hex=None,
              is_cell=False):
    p = doc_or_cell_para if is_cell else doc_or_cell_para.add_paragraph()
    p.paragraph_format.space_after = Pt(0)  # no after-space (yoga-socks border-press fix)
    run = p.add_run(text)
    _style_run(run, style, size_pt=size_pt or style["font"]["body_pt"], bold=bold,
               color_hex=color_hex)
    return p


def _add_table(doc, spec, style) -> None:
    headers = spec.get("headers", []) or []
    rows = spec.get("rows", []) or []
    ncol = max(1, len(headers) or (len(rows[0]) if rows else 1))
    cellcfg = style["cell"]
    pal = style["palette"]
    table = doc.add_table(rows=1 + len(rows), cols=ncol)
    table.autofit = False
    colw = _PAGE_CONTENT_TWIPS // ncol

    def fill_cell(cell, text, *, fill_hex, bold=False, color_hex=None):
        cell.width = Twips(colw)
        _cell_valign_center(cell)
        _cell_margins(cell, cellcfg["margin_top_twips"], cellcfg["margin_bottom_twips"],
                      cellcfg["margin_left_twips"], cellcfg["margin_right_twips"])
        _cell_shading(cell, fill_hex)
        _add_para(cell.paragraphs[0], style, str(text), bold=bold, color_hex=color_hex,
                  is_cell=True)

    for j, head in enumerate(headers):
        fill_cell(table.rows[0].cells[j], head, fill_hex=pal["header_fill"], bold=True,
                  color_hex=pal["header_text"])
    for i, row in enumerate(rows):
        fill_hex = pal["row_bg"] if i % 2 == 0 else pal["row_alt_bg"]
        for j in range(ncol):
            val = row[j] if j < len(row) else ""
            fill_cell(table.rows[1 + i].cells[j], val, fill_hex=fill_hex)
    _no_table_borders(table)


def _apply_machine_fonts(style: dict, lang) -> dict:
    """Override the style's Latin + CJK faces with ones ACTUALLY INSTALLED on this machine for the
    run's language, reducing local tofu (方块) when the house-style font is absent (e.g. LiSong Pro
    on recent macOS / any Linux). DOCX fonts are not embedded, so another reader machine still needs
    the selected family or a compatible substitute. The house-style font stays the first preference, so a
    machine that HAS it is unchanged; the pick is cached after the first report (Owner 2026-07-24).
    Fail-safe: any resolver error leaves the style fonts as-is — never worse than before, never a
    render crash."""
    if not lang:
        return style
    try:
        import font_resolver           # sibling script
        picked = font_resolver.resolve_fonts(lang, style.get("font", {}))
        style["font"]["body_latin"] = picked["body_latin"]
        style["font"]["body_cjk"] = picked["body_cjk"]
        src = "recorded" if picked.get("from_cache") else "auto-selected on this machine"
        print(f"\U0001f524 fonts ({src}): latin={picked['body_latin']} \u00b7 cjk={picked['body_cjk']}")
    except Exception as e:  # font adaptation is best-effort, never break the render
        print(f"\U0001f524 font auto-select skipped ({type(e).__name__}: {e}); using style defaults")
    return style


def render(report_path, out_path, style_path=DEFAULT_STYLE, lang=None) -> str:
    style = load_style(style_path)
    style = _apply_machine_fonts(style, lang)
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = style["font"]["body_latin"]
    normal.font.size = Pt(style["font"]["body_pt"])

    if report.get("title"):
        _add_para(doc, style, report["title"], size_pt=style["font"]["title_pt"], bold=True,
                  color_hex=style["palette"]["header_fill"])
    if report.get("subtitle"):
        _add_para(doc, style, report["subtitle"], size_pt=10,
                  color_hex=style["palette"]["gray_secondary"])

    for sec in report.get("sections", []) or []:
        if sec.get("heading"):
            _add_para(doc, style, sec["heading"], size_pt=16, bold=True,
                      color_hex=style["palette"]["accent"])
        for para in sec.get("paragraphs", []) or []:
            _add_para(doc, style, para)
        if sec.get("table"):
            _add_table(doc, sec["table"], style)

    doc.save(out_path)
    return out_path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Render a report.json into a styled .docx.")
    ap.add_argument("report_json")
    ap.add_argument("out_docx")
    ap.add_argument("style_json", nargs="?", default=str(DEFAULT_STYLE))
    ap.add_argument("--lang", default=None,
                    help="BCP-47 language -> auto-select an on-machine font for its script (recorded "
                         "after the first report). Omit to use the style fonts verbatim.")
    args = ap.parse_args()
    print("wrote", render(args.report_json, args.out_docx, args.style_json, lang=args.lang))
