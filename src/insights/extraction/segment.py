"""Step 2: Segment raw text into chapter/section units."""

from __future__ import annotations

import json
from pathlib import Path

from .extract_pdf import extract_page_range


def segment_by_chapters(raw_text: str, chapters: list[dict]) -> list[dict]:
    """
    Split raw text into chapter/section units.

    Args:
        raw_text: Text with ---PAGE N--- markers.
        chapters: List of chapter definitions with name, part, start, end.

    Returns:
        List of section dicts with part, name, page_range, text.
    """
    sections = []

    for chapter in chapters:
        text = extract_page_range(raw_text, chapter["start"], chapter["end"])

        # Clean up text - remove excessive whitespace
        text = "\n".join(line for line in text.split("\n") if line.strip())

        sections.append({
            "part": chapter["part"],
            "name": chapter["name"],
            "page_range": [chapter["start"], chapter["end"]],
            "text": text,
            "char_count": len(text),
        })

    return sections


def save_sections(sections: list[dict], output_path: str | Path) -> None:
    """Save sections to JSON file."""
    with open(output_path, "w") as f:
        json.dump(sections, f, indent=2)


def load_sections(path: str | Path) -> list[dict]:
    """Load sections from JSON file."""
    with open(path) as f:
        return json.load(f)


def print_section_summary(sections: list[dict]) -> None:
    """Print summary of extracted sections."""
    total_chars = sum(s["char_count"] for s in sections)
    print(f"\n{'='*60}")
    print(f"Extracted {len(sections)} sections")
    print(f"Total characters: {total_chars:,}")
    print(f"{'='*60}\n")

    for section in sections:
        pages = f"{section['page_range'][0]}-{section['page_range'][1]}"
        print(f"Part {section['part']:2d}: {section['name']:<25} "
              f"Pages {pages:<10} ({section['char_count']:,} chars)")


if __name__ == "__main__":
    from .config import JANDA_CHAPTERS
    from .extract_pdf import extract_pdf

    # Test with sample
    raw_text, _ = extract_pdf("/Users/davideyal/Downloads/AplicationsofNLH (1).pdf")
    sections = segment_by_chapters(raw_text, JANDA_CHAPTERS)
    print_section_summary(sections)
