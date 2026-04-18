#!/usr/bin/env python3
"""
Convert a Markdown file to PDF using ``markdown`` (MD → HTML) and PyMuPDF ``Story`` (HTML → PDF).

No extra system libraries (e.g. cairo) are required beyond existing ``PyMuPDF``.

Example:
  python md_to_pdf.py notes.md
  python md_to_pdf.py notes.md -o notes.pdf
  python md_to_pdf.py notes.md --css extra.css
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import fitz  # PyMuPDF
import markdown


DEFAULT_CSS = """
body {
  font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 11pt;
  line-height: 1.35;
  color: #111;
}
h1, h2, h3, h4 { color: #000; }
pre, code {
  font-family: Menlo, Monaco, Consolas, "Courier New", monospace;
  font-size: 9pt;
}
pre {
  background: #f4f4f4;
  border: 1px solid #ddd;
  padding: 8px;
  white-space: pre-wrap;
  word-wrap: break-word;
}
code { background: #f4f4f4; padding: 1px 4px; }
pre code { background: transparent; padding: 0; border: none; }
table { border-collapse: collapse; width: 100%; margin: 0.5em 0; }
th, td { border: 1px solid #ccc; padding: 4px 8px; text-align: left; }
blockquote {
  border-left: 4px solid #ccc;
  margin: 0.5em 0;
  padding-left: 1em;
  color: #333;
}
a { color: #0645ad; }
ul, ol { margin: 0.4em 0; padding-left: 1.4em; }
"""


def md_to_pdf(
    md_path: Path,
    pdf_path: Path,
    *,
    extra_css: str | None = None,
    paper: str = "a4",
    margin_pt: float = 36.0,
) -> None:
    text = md_path.read_text(encoding="utf-8")
    body = markdown.markdown(
        text,
        extensions=["extra", "tables", "fenced_code", "nl2br"],
    )
    css = DEFAULT_CSS + (extra_css or "")
    html = (
        "<!DOCTYPE html><html><head>"
        '<meta charset="utf-8"/>'
        "</head><body>"
        f"{body}"
        "</body></html>"
    )
    story = fitz.Story(html=html, user_css=css)
    mediabox = fitz.paper_rect(paper)
    content_rect = mediabox + (margin_pt, margin_pt, -margin_pt, -margin_pt)

    def rectfn(_rect_num: int, _filled) -> tuple:
        return mediabox, content_rect, None

    doc = story.write_with_links(rectfn)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(pdf_path.as_posix())
    doc.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Markdown to PDF (PyMuPDF Story).")
    parser.add_argument("input", type=Path, help="Path to .md file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output .pdf path (default: same basename as input)",
    )
    parser.add_argument(
        "--css",
        type=Path,
        default=None,
        help="Optional UTF-8 CSS file appended after built-in styles",
    )
    parser.add_argument(
        "--paper",
        default="a4",
        help="Paper size name for fitz.paper_rect (default: a4)",
    )
    parser.add_argument(
        "--margin-pt",
        type=float,
        default=36.0,
        help="Margin in points on all sides (default: 36 ≈ 0.5 inch)",
    )
    args = parser.parse_args()
    md_path = args.input
    if not md_path.is_file():
        print(f"Not a file: {md_path}", file=sys.stderr)
        sys.exit(1)
    pdf_path = args.output or md_path.with_suffix(".pdf")
    extra = args.css.read_text(encoding="utf-8") if args.css else None
    try:
        md_to_pdf(
            md_path,
            pdf_path,
            extra_css=extra,
            paper=args.paper,
            margin_pt=args.margin_pt,
        )
    except Exception as e:  # noqa: BLE001
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    print(f"Wrote {pdf_path}")


if __name__ == "__main__":
    main()
