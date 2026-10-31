"""The incident report as a readable, printable page.

The report is composed once, as Markdown (report.py). This renders that same Markdown as a self-contained HTML page so the
Report button shows a document rather than raw text: tables, headings, a print/PDF button, and downloads of the Markdown and
JSON. Everything in the Markdown comes from evidence (file names, witness quotes), so every character is HTML-escaped before
any markup is added; nothing in the report can inject markup or script.
"""
from __future__ import annotations

import html
import re

_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")


def _inline(text: str) -> str:
    out = html.escape(text, quote=True)
    codes: list[str] = []

    def stash(m: re.Match) -> str:  # code spans are shielded from bold/italic
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    out = _CODE.sub(stash, out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], out)


def _cells(line: str) -> list[str]:
    parts = line.strip().strip("|").split("|")
    return [c.strip() for c in parts]


def _is_rule(line: str) -> bool:
    cells = _cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def markdown_to_html(md: str) -> str:
    """The subset of Markdown the report uses: headings, paragraphs, tables, bullet lists, quotes, rules, inline emphasis."""
    lines, out, i = md.splitlines(), [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif m := re.match(r"(#{1,3}) (.+)", line):
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
        elif line.strip() == "---":
            out.append("<hr>")
            i += 1
        elif line.startswith("|") and i + 1 < len(lines) and _is_rule(lines[i + 1]):
            head, rows = _cells(line), []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            out.append("<div class=\"scroll\"><table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head)
                       + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
                       + "</tbody></table></div>")
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{_inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
        elif line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i].lstrip("> ").rstrip())
                i += 1
            out.append("<blockquote>" + "".join(f"<p>{_inline(q)}</p>" for q in quote if q) + "</blockquote>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"(#{1,3} |\||- |>|---$)", lines[i]):
                para.append(lines[i].strip())
                i += 1
            if not para:  # a line the rules above did not claim: keep it as text rather than loop
                para, i = [lines[i].strip()], i + 1
            out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Incident report {title}</title>
<style>
:root {{ --bg:#ffffff; --fg:#0f172a; --muted:#64748b; --line:#e2e8f0; --head:#f1f5f9; --accent:#2563eb; --quote:#f8fafc; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0b1220; --fg:#e2e8f0; --muted:#94a3b8; --line:#273449; --head:#162033; --accent:#60a5fa; --quote:#101a2c; }} }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:60rem; margin:0 auto; padding:1rem 1rem 4rem; }}
.bar {{ position:sticky; top:0; display:flex; flex-wrap:wrap; gap:.5rem; align-items:center; padding:.6rem 1rem; background:var(--bg); border-bottom:1px solid var(--line); }}
.bar a, .bar button {{ font:inherit; font-size:.85rem; color:var(--fg); background:none; border:1px solid var(--line); border-radius:999px; padding:.25rem .8rem; text-decoration:none; cursor:pointer; }}
.bar a:hover, .bar button:hover {{ border-color:var(--accent); color:var(--accent); }}
.bar span {{ margin-right:auto; color:var(--muted); font-size:.85rem; }}
h1 {{ font-size:1.6rem; margin:1.2rem 0 .4rem; }} h2 {{ font-size:1.2rem; margin:2rem 0 .5rem; padding-bottom:.25rem; border-bottom:1px solid var(--line); }} h3 {{ font-size:1rem; margin:1.2rem 0 .3rem; }}
p, li {{ max-width:70ch; }} code {{ font:.85em ui-monospace,Menlo,monospace; background:var(--head); padding:.05rem .3rem; border-radius:4px; }}
.scroll {{ overflow-x:auto; margin:.6rem 0; }} table {{ border-collapse:collapse; width:100%; font-size:.88rem; }}
th, td {{ border:1px solid var(--line); padding:.35rem .55rem; text-align:left; vertical-align:top; }} th {{ background:var(--head); }}
blockquote {{ margin:.8rem 0; padding:.2rem 1rem; border-left:4px solid var(--accent); background:var(--quote); }} blockquote p {{ margin:.4rem 0; }}
hr {{ border:0; border-top:1px solid var(--line); margin:2rem 0 1rem; }} em {{ color:inherit; }}
@media print {{ .bar {{ display:none; }} main {{ max-width:none; padding:0; }} :root {{ --bg:#fff; --fg:#000; --muted:#444; --line:#bbb; --head:#eee; --quote:#fff; }} h2 {{ break-after:avoid; }} tr {{ break-inside:avoid; }} }}
</style></head><body>
<div class="bar"><span>Run {run}</span>
<button onclick="window.print()" title="Print, or save as PDF from the print dialog">Print / save as PDF</button>
<a href="{md_link}" download>Download Markdown</a><a href="{json_link}" download="report-{run}.json">Download JSON</a></div>
<main>
{body}
</main></body></html>
"""


def render_page(markdown: str, run_id: str, md_link: str, json_link: str) -> str:
    title = html.escape(run_id)
    return PAGE.format(title=title, run=title, md_link=html.escape(md_link, quote=True),
                       json_link=html.escape(json_link, quote=True), body=markdown_to_html(markdown))
