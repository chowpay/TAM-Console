from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


MAX_TEXT_CHARS = 120_000
OCR_PAGE_LIMIT = 20


def _run_text_command(command: list[str], timeout: int = 90) -> str:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def pdf_page_count(path: Path) -> int:
    output = _run_text_command(["pdfinfo", str(path)], timeout=30)
    for line in output.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def extract_pdf_text(path: Path) -> tuple[str, str]:
    text = _run_text_command(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"])
    if len(text.strip()) >= 500:
        return text[:MAX_TEXT_CHARS], "pdftotext"
    ocr_text = ocr_pdf(path)
    if ocr_text:
        return ocr_text[:MAX_TEXT_CHARS], "ocr"
    return text[:MAX_TEXT_CHARS], "pdftotext"


def ocr_pdf(path: Path) -> str:
    pages = pdf_page_count(path) or OCR_PAGE_LIMIT
    last_page = min(pages, OCR_PAGE_LIMIT)
    with tempfile.TemporaryDirectory(prefix="tam-pdf-ocr-") as temp_dir:
        prefix = str(Path(temp_dir) / "page")
        render = subprocess.run(
            ["pdftoppm", "-r", "160", "-f", "1", "-l", str(last_page), "-png", str(path), prefix],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
            check=False,
        )
        if render.returncode != 0:
            return ""
        chunks = []
        for image in sorted(Path(temp_dir).glob("page-*.png")):
            text = _run_text_command(["tesseract", str(image), "stdout", "--psm", "6"], timeout=90)
            if text:
                chunks.append(f"## OCR page {len(chunks) + 1}\n{text}")
        return "\n\n".join(chunks).strip()


def extract_document_text(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(path)
    if suffix in {".txt", ".md", ".csv", ".json", ".log"}:
        return path.read_text(errors="replace")[:MAX_TEXT_CHARS], "text"
    return "", "unsupported"


def build_extraction_prompt(packet: dict) -> str:
    return (
        "You are extracting TAM Console document intelligence from bounded evidence.\n"
        "Use only the provided customer context, artifact metadata, and extracted document text.\n"
        "The goal is to help a technical account manager understand what the customer wants to do, "
        "what capabilities are being requested, what technical requirements matter, and what follow-up is needed.\n"
        "Preserve product/protocol names from the document. If terminology appears inconsistent, call that out as an open question.\n"
        "Return strict JSON with: document_type, executive_summary, customer_goal, requested_capabilities, workflows, "
        "technical_requirements, assumptions, open_questions, action_items, risks, related_tickets, environment_references, "
        "terms, evidence.\n"
        "Each action item must include task, owner, due_date, confidence, and evidence.\n"
        "Evidence should cite page numbers, section names, or short source snippets when available.\n\n"
        + json.dumps(packet, indent=2, sort_keys=True)
    )
