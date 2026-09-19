# DOCX Report Generation

Read this file only after market insights have been delivered and the user asks for a DOCX report.
Report generation is client-side presentation work; the server does not own the style or renderer.

## User Confirmation

After delivering the insight report, offer a DOCX report in `scope.language`. Run the renderer only
after an affirmative response. Do not treat consent to market research as consent to create files.

## Resources

| Resource | Purpose |
|---|---|
| `scripts/render_report.py` | Deterministically maps structured report JSON into a styled DOCX |
| `assets/report_style.json` | The user-editable canonical style source |
| `scripts/font_resolver.py` | Selects an installed language-appropriate font and caches the choice |
| `scripts/requirements.txt` | Declares the `python-docx` dependency |

Keep `assets/report_style.json` separate from the renderer. It allows users to change preferred
fonts, palette, semantic colors, and cell metrics without modifying Python. Do not duplicate its
default values in SKILL.md or another reference.

## Input Shape

Map the final insight envelope into a report JSON file:

```json
{
  "title": "Research report title",
  "subtitle": "Optional date or source summary",
  "sections": [
    {
      "heading": "Section heading",
      "paragraphs": ["Paragraph one", "Paragraph two"],
      "table": {
        "headers": ["Column A", "Column B"],
        "rows": [["Value A", "Value B"]]
      }
    }
  ]
}
```

Use the report's user-facing content, not raw internal panel payloads. Preserve citations, trust
tiers, uncertainty, and quality warnings.

## Run the Renderer

Install the dependency once when it is not already available:

```text
python -m pip install -r scripts/requirements.txt
```

Generate the report:

```text
python scripts/render_report.py <report.json> <out.docx> --lang <bcp47>
```

The renderer accepts an optional style path after `<out.docx>`. Without one, it uses
`assets/report_style.json`.

When `--lang` is present, `render_report.py` invokes `font_resolver.py`. The resolver checks fonts
installed on the current Windows, macOS, or Linux machine, selects a suitable family for Latin or
the relevant CJK region, records the selection in a per-user cache, and reuses it while valid. If
font probing or cache access fails, it falls back to the style default rather than failing the
report.

Fonts are not embedded in the DOCX. Another reader machine still needs the selected family or a
compatible substitute. Font metrics, wrapping, pagination, and document bytes can therefore differ
across machines even when report text and non-font styling are identical.

## Table Rules

The renderer and canonical style asset enforce these presentation invariants:

- vertically center every cell;
- use no paragraph after-spacing inside cells and keep line spacing bounded;
- use symmetric cell margins;
- use DXA widths rather than percentage widths;
- use clear shading and no hard cell borders;
- apply the semantic high, medium, and warning colors from the style asset.

Do not hand-edit OOXML to reproduce these rules. Use the shipped renderer so repeated reports follow
the same structure.

## Verification

After generation, verify that the DOCX exists, opens successfully, contains the expected sections,
and has no clipped or unreadable table content. Report any font fallback or portability limitation
to the user when it affects the result.
