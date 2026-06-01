"""Step 1: Extract text from PDF with page markers."""

from __future__ import annotations

from pathlib import Path

import fitz  # pymupdf


def extract_pdf(pdf_path: str | Path) -> tuple[str, list]:
    """
    Extract text from PDF with page markers and TOC.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Tuple of (raw_text with page markers, toc list).
    """
    doc = fitz.open(str(pdf_path))
    toc = doc.get_toc()

    raw_text = ""
    for page_num in range(len(doc)):
        page = doc[page_num]
        raw_text += f"\n---PAGE {page_num + 1}---\n"
        raw_text += page.get_text()

    doc.close()

    return raw_text, toc


def extract_page_range(raw_text: str, start_page: int, end_page: int) -> str:
    """
    Extract text for a specific page range from raw text with markers.

    Args:
        raw_text: Text with ---PAGE N--- markers.
        start_page: Starting page number (1-indexed).
        end_page: Ending page number (inclusive).

    Returns:
        Text from the specified page range.
    """
    lines = raw_text.split("\n")
    result_lines = []
    in_range = False
    current_page = 0

    for line in lines:
        if line.startswith("---PAGE ") and line.endswith("---"):
            try:
                current_page = int(line[8:-3])
                in_range = start_page <= current_page <= end_page
            except ValueError:
                pass
            continue

        if in_range:
            result_lines.append(line)

    return "\n".join(result_lines)


def save_raw_text(raw_text: str, output_path: str | Path) -> None:
    """Save raw extracted text to file."""
    Path(output_path).write_text(raw_text)


if __name__ == "__main__":
    from .config import DEFAULT_PDF_PATH

    raw_text, toc = extract_pdf(DEFAULT_PDF_PATH)
    print(f"Extracted {len(raw_text):,} characters")
    print(f"TOC has {len(toc)} entries")

    # Print first few TOC entries
    for entry in toc[:10]:
        print(f"  Level {entry[0]}: {entry[1]} (page {entry[2]})")
