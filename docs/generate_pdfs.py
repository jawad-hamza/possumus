from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
import re
import textwrap

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer


ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs"
HOW_TO_PATH = DOCS_DIR / "HOW_TO_USE.md"
FULL_DOC_PATH = DOCS_DIR / "FULL_DOCUMENTATION.md"
TUTORIAL_PATH = DOCS_DIR / "TUTORIAL.md"
HOW_TO_PDF = DOCS_DIR / "How_to_Use_POS_IMS.pdf"
FULL_DOC_PDF = DOCS_DIR / "POS_IMS_Full_Documentation.pdf"
TUTORIAL_PDF = DOCS_DIR / "POS_IMS_Tutorial.pdf"
CODE_TXT = DOCS_DIR / "POS_IMS_Source_Code.txt"
CODE_PDF = DOCS_DIR / "POS_IMS_Source_Code.pdf"
GENERATED_AT = datetime.now().strftime("%Y-%m-%d %H:%M")
IMAGE_PATTERN = re.compile(r"^!\[(?P<alt>.*?)\]\((?P<path>.*?)\)$")

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "company_logos",
}
EXCLUDED_FILE_NAMES = {
    HOW_TO_PDF.name,
    FULL_DOC_PDF.name,
    TUTORIAL_PDF.name,
    CODE_PDF.name,
    CODE_TXT.name,
}
INCLUDED_EXTENSIONS = {
    ".py",
    ".html",
    ".md",
    ".txt",
    ".css",
    ".js",
    ".json",
    ".ps1",
}


def build_styles():
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "DocTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=22,
            leading=28,
            textColor=colors.HexColor("#14213d"),
            spaceAfter=8,
        ),
        "h1": ParagraphStyle(
            "H1",
            parent=styles["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=17,
            leading=22,
            textColor=colors.HexColor("#14213d"),
            spaceBefore=10,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "H2",
            parent=styles["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=18,
            textColor=colors.HexColor("#1f355f"),
            spaceBefore=8,
            spaceAfter=4,
        ),
        "h3": ParagraphStyle(
            "H3",
            parent=styles["Heading3"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=15,
            textColor=colors.HexColor("#334155"),
            spaceBefore=6,
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9.7,
            leading=14,
            textColor=colors.HexColor("#202938"),
            spaceAfter=5,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=9.7,
            leading=14,
            leftIndent=14,
            firstLineIndent=-8,
            bulletIndent=0,
            textColor=colors.HexColor("#202938"),
            spaceAfter=3,
        ),
        "code": ParagraphStyle(
            "Code",
            fontName="Courier",
            fontSize=7.5,
            leading=9.2,
            textColor=colors.HexColor("#10213d"),
            backColor=colors.HexColor("#f8fafc"),
            borderPadding=6,
            borderColor=colors.HexColor("#dbe3ee"),
            borderWidth=0.5,
            borderRadius=4,
            spaceAfter=6,
        ),
        "meta": ParagraphStyle(
            "Meta",
            parent=styles["BodyText"],
            fontName="Helvetica-Oblique",
            fontSize=8.5,
            leading=12,
            textColor=colors.HexColor("#5f6f86"),
            spaceAfter=10,
        ),
        "caption": ParagraphStyle(
            "Caption",
            parent=styles["BodyText"],
            fontName="Helvetica-Oblique",
            fontSize=8.3,
            leading=11,
            alignment=1,
            textColor=colors.HexColor("#5f6f86"),
            spaceAfter=8,
        ),
    }


def page_decorator(title: str):
    def draw(canvas_obj, doc):
        canvas_obj.saveState()
        canvas_obj.setStrokeColor(colors.HexColor("#dce3ee"))
        canvas_obj.setLineWidth(0.5)
        canvas_obj.line(doc.leftMargin, A4[1] - 20 * mm, A4[0] - doc.rightMargin, A4[1] - 20 * mm)
        canvas_obj.line(doc.leftMargin, 14 * mm, A4[0] - doc.rightMargin, 14 * mm)
        canvas_obj.setFont("Helvetica-Bold", 10)
        canvas_obj.setFillColor(colors.HexColor("#14213d"))
        canvas_obj.drawString(doc.leftMargin, A4[1] - 16 * mm, title)
        canvas_obj.setFont("Helvetica", 8)
        canvas_obj.setFillColor(colors.HexColor("#5f6f86"))
        canvas_obj.drawRightString(A4[0] - doc.rightMargin, A4[1] - 16 * mm, f"Generated {GENERATED_AT}")
        canvas_obj.drawRightString(A4[0] - doc.rightMargin, 9 * mm, f"Page {canvas_obj.getPageNumber()}")
        canvas_obj.restoreState()
    return draw


def build_image(path: Path):
    image = RLImage(str(path))
    max_width = 170 * mm
    max_height = 96 * mm
    scale = min(max_width / image.imageWidth, max_height / image.imageHeight, 1)
    image.drawWidth = image.imageWidth * scale
    image.drawHeight = image.imageHeight * scale
    image.hAlign = "CENTER"
    return image


def markdown_to_story(markdown_text: str, document_title: str, base_dir: Path):
    styles = build_styles()
    story = [Paragraph(escape(document_title), styles["title"]), Paragraph(f"Generated {GENERATED_AT}", styles["meta"])]
    lines = markdown_text.splitlines()
    paragraph_buffer: list[str] = []
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph():
        nonlocal paragraph_buffer
        if paragraph_buffer:
            text = " ".join(line.strip() for line in paragraph_buffer).strip()
            if text:
                story.append(Paragraph(escape(text), styles["body"]))
            paragraph_buffer = []

    def flush_code():
        nonlocal code_lines
        if code_lines:
            story.append(Preformatted("\n".join(code_lines), styles["code"]))
            code_lines = []

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_paragraph()
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not stripped:
            flush_paragraph()
            story.append(Spacer(1, 4))
            continue

        image_match = IMAGE_PATTERN.match(stripped)
        if image_match:
            flush_paragraph()
            alt_text = image_match.group("alt").strip() or "Snapshot"
            image_path = (base_dir / image_match.group("path").strip()).resolve()
            if image_path.exists():
                story.append(build_image(image_path))
                story.append(Spacer(1, 4))
                story.append(Paragraph(escape(alt_text), styles["caption"]))
            else:
                story.append(Paragraph(f"[Missing image: {escape(image_match.group('path'))}]", styles["meta"]))
            continue

        if stripped.startswith("# "):
            flush_paragraph()
            story.append(Paragraph(escape(stripped[2:].strip()), styles["h1"]))
            continue
        if stripped.startswith("## "):
            flush_paragraph()
            story.append(Paragraph(escape(stripped[3:].strip()), styles["h2"]))
            continue
        if stripped.startswith("### "):
            flush_paragraph()
            story.append(Paragraph(escape(stripped[4:].strip()), styles["h3"]))
            continue
        if stripped.startswith("- "):
            flush_paragraph()
            story.append(Paragraph(f"&bull; {escape(stripped[2:].strip())}", styles["bullet"]))
            continue
        if re.match(r"^\d+\.\s+", stripped):
            flush_paragraph()
            story.append(Paragraph(escape(stripped), styles["bullet"]))
            continue

        paragraph_buffer.append(stripped)

    flush_paragraph()
    flush_code()
    return story


def render_markdown_pdf(markdown_path: Path, pdf_path: Path, title: str):
    text = markdown_path.read_text(encoding="utf-8")
    story = markdown_to_story(text, title, markdown_path.parent)
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=26 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="OpenAI Codex",
    )
    decorator = page_decorator(title)
    doc.build(story, onFirstPage=decorator, onLaterPages=decorator)


def iter_source_files():
    files = []
    for path in ROOT.rglob("*"):
        if path.is_dir():
            continue
        rel_parts = path.relative_to(ROOT).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        if path.name in EXCLUDED_FILE_NAMES:
            continue
        if path.suffix.lower() not in INCLUDED_EXTENSIONS:
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(ROOT).as_posix())


def build_source_export() -> str:
    lines = [
        "POS IMS SOURCE CODE EXPORT",
        f"Generated: {GENERATED_AT}",
        "Included roots: manage.py, possumus, pos, accounts, api, docs text sources, and related text assets.",
        "Excluded: .git, .venv, company_logos uploads, generated PDFs, generated source export text.",
        "",
    ]

    for path in iter_source_files():
        relative = path.relative_to(ROOT).as_posix()
        lines.append("=" * 110)
        lines.append(f"FILE: {relative}")
        lines.append("=" * 110)
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            body = f"[Unable to read file: {exc}]"
        lines.append(body.rstrip("\n"))
        lines.append("")
    return "\n".join(lines) + "\n"


def render_code_pdf(text_content: str, pdf_path: Path, title: str):
    pdf = canvas.Canvas(str(pdf_path), pagesize=landscape(A4))
    width, height = landscape(A4)
    left = 12 * mm
    right = 12 * mm
    top = 12 * mm
    bottom = 12 * mm
    line_height = 8.2
    wrap_width = 150
    page_number = 1

    def start_page():
        pdf.setStrokeColor(colors.HexColor("#dce3ee"))
        pdf.setLineWidth(0.5)
        pdf.line(left, height - top - 6, width - right, height - top - 6)
        pdf.line(left, bottom - 4, width - right, bottom - 4)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.setFillColor(colors.HexColor("#14213d"))
        pdf.drawString(left, height - top + 4, title)
        pdf.setFont("Helvetica", 8)
        pdf.setFillColor(colors.HexColor("#5f6f86"))
        pdf.drawRightString(width - right, height - top + 4, f"Generated {GENERATED_AT}")
        pdf.drawRightString(width - right, bottom - 1, f"Page {page_number}")
        pdf.setFont("Courier", 6.5)
        pdf.setFillColor(colors.black)
        return height - top - 18

    y = start_page()

    for raw_line in text_content.splitlines():
        expanded = raw_line.replace("\t", "    ")
        wrapped = textwrap.wrap(
            expanded,
            width=wrap_width,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        for line in wrapped:
            if y <= bottom + 8:
                pdf.showPage()
                page_number += 1
                y = start_page()
            pdf.drawString(left, y, line)
            y -= line_height

    pdf.save()


def main():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    render_markdown_pdf(HOW_TO_PATH, HOW_TO_PDF, "How to Use POS IMS")
    render_markdown_pdf(FULL_DOC_PATH, FULL_DOC_PDF, "POS IMS Full Documentation")
    render_markdown_pdf(TUTORIAL_PATH, TUTORIAL_PDF, "POS IMS Step-by-Step Tutorial")
    source_export = build_source_export()
    CODE_TXT.write_text(source_export, encoding="utf-8")
    render_code_pdf(source_export, CODE_PDF, "POS IMS Source Code")
    print(f"Created: {HOW_TO_PDF}")
    print(f"Created: {FULL_DOC_PDF}")
    print(f"Created: {TUTORIAL_PDF}")
    print(f"Created: {CODE_TXT}")
    print(f"Created: {CODE_PDF}")


if __name__ == "__main__":
    main()
