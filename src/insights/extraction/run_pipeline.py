#!/usr/bin/env python3
"""Main entry point for Janda textbook extraction pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .config import JANDA_CHAPTERS, DEFAULT_PDF_PATH
from .extract_pdf import extract_pdf, save_raw_text
from .segment import segment_by_chapters, save_sections, print_section_summary
from .chunk import chunk_all_sections, save_chunks
from .tag import tag_all_chunks, save_corpus, print_metadata_summary


def generate_report(
    raw_text: str,
    sections: list[dict],
    chunks: list[dict],
    output_path: str | Path
) -> None:
    """Generate extraction report."""
    report = f"""# Janda Textbook Extraction Report

Generated: {datetime.now().isoformat()}

## Summary

- **Source:** Applications of No-Limit Hold'em by Matthew Janda
- **Raw text:** {len(raw_text):,} characters
- **Sections:** {len(sections)}
- **Chunks:** {len(chunks)}

## Section Breakdown

| Part | Name | Pages | Characters | Chunks |
|------|------|-------|------------|--------|
"""

    # Count chunks per section
    chunks_per_part = {}
    for chunk in chunks:
        part = chunk["part"]
        chunks_per_part[part] = chunks_per_part.get(part, 0) + 1

    for section in sections:
        part = section["part"]
        pages = f"{section['page_range'][0]}-{section['page_range'][1]}"
        chunk_count = chunks_per_part.get(part, 0)
        report += f"| {part} | {section['name']} | {pages} | {section['char_count']:,} | {chunk_count} |\n"

    # Token statistics
    token_counts = [c.get("token_estimate", 0) for c in chunks]
    if token_counts:
        avg_tokens = sum(token_counts) / len(token_counts)
        min_tokens = min(token_counts)
        max_tokens = max(token_counts)
    else:
        avg_tokens = min_tokens = max_tokens = 0

    report += f"""
## Chunk Statistics

- **Total chunks:** {len(chunks)}
- **Average tokens per chunk:** {avg_tokens:.0f}
- **Min tokens:** {min_tokens}
- **Max tokens:** {max_tokens}

## Metadata Coverage

"""

    # Calculate metadata coverage
    total = len(chunks)
    if total > 0:
        streets = sum(1 for c in chunks if c.get("metadata", {}).get("streets"))
        textures = sum(1 for c in chunks if c.get("metadata", {}).get("board_textures"))
        concepts = sum(1 for c in chunks if c.get("metadata", {}).get("concepts"))

        report += f"- Streets tagged: {streets}/{total} ({100*streets/total:.1f}%)\n"
        report += f"- Board textures tagged: {textures}/{total} ({100*textures/total:.1f}%)\n"
        report += f"- Concepts tagged: {concepts}/{total} ({100*concepts/total:.1f}%)\n"

    # Top concepts
    concept_counts: dict[str, int] = {}
    for chunk in chunks:
        for concept in chunk.get("metadata", {}).get("concepts", []):
            concept_counts[concept] = concept_counts.get(concept, 0) + 1

    if concept_counts:
        report += "\n### Top Concepts\n\n"
        for concept, count in sorted(concept_counts.items(), key=lambda x: -x[1])[:20]:
            report += f"- {concept}: {count} chunks\n"

    # Sample chunks
    report += "\n## Sample Chunks\n\n"
    for i, chunk in enumerate(chunks[:3]):
        report += f"### {chunk['chunk_id']}: {chunk['title']}\n\n"
        report += f"**Part:** {chunk['part']} - {chunk['name']}\n\n"
        report += f"**Summary:** {chunk.get('summary', 'N/A')}\n\n"
        if chunk.get("metadata"):
            report += f"**Concepts:** {', '.join(chunk['metadata'].get('concepts', []))}\n\n"
        report += f"**Text preview:** {chunk['text'][:500]}...\n\n"
        report += "---\n\n"

    Path(output_path).write_text(report)


def run_extraction_pipeline(
    pdf_path: str | None = None,
    output_dir: str | None = None,
    use_llm_chunking: bool = True,
    skip_tagging: bool = False,
    skip_existing: bool = True,
    api_key: str | None = None
) -> list[dict]:
    """
    Run the full extraction pipeline.

    Args:
        pdf_path: Path to Janda PDF (uses default if not provided).
        output_dir: Output directory (uses default if not provided).
        use_llm_chunking: Whether to use LLM for intelligent chunking.
        skip_tagging: Skip the LLM tagging step.
        skip_existing: Skip steps if intermediate files exist.
        api_key: Anthropic API key.

    Returns:
        List of final corpus chunks.
    """
    pdf_path = pdf_path or DEFAULT_PDF_PATH
    output_dir = Path(output_dir or Path(__file__).parent / "output")
    output_dir.mkdir(parents=True, exist_ok=True)

    # File paths
    raw_text_path = output_dir / "janda_raw.txt"
    sections_path = output_dir / "janda_sections.json"
    chunks_path = output_dir / "janda_chunks.json"
    corpus_path = output_dir / "janda_corpus.json"
    report_path = output_dir / "extraction_report.md"

    print("=" * 60)
    print("JANDA TEXTBOOK EXTRACTION PIPELINE")
    print("=" * 60)
    print(f"PDF: {pdf_path}")
    print(f"Output: {output_dir}")
    print()

    # Step 1: Extract PDF
    print("Step 1: Extracting PDF...")
    if skip_existing and raw_text_path.exists():
        print("  Loading existing raw text...")
        raw_text = raw_text_path.read_text()
    else:
        raw_text, toc = extract_pdf(pdf_path)
        save_raw_text(raw_text, raw_text_path)
        print(f"  Extracted {len(raw_text):,} characters")
        print(f"  TOC has {len(toc)} entries")

    # Step 2: Segment by chapters
    print("\nStep 2: Segmenting by chapters...")
    if skip_existing and sections_path.exists():
        print("  Loading existing sections...")
        with open(sections_path) as f:
            sections = json.load(f)
    else:
        sections = segment_by_chapters(raw_text, JANDA_CHAPTERS)
        save_sections(sections, sections_path)
    print_section_summary(sections)

    # Step 3: Chunk sections
    print("\nStep 3: Chunking sections...")
    if skip_existing and chunks_path.exists():
        print("  Loading existing chunks...")
        with open(chunks_path) as f:
            chunks = json.load(f)
    else:
        chunks = chunk_all_sections(
            sections,
            use_llm=use_llm_chunking,
            api_key=api_key
        )
        save_chunks(chunks, chunks_path)
    print(f"  Total: {len(chunks)} chunks")

    # Step 4: Tag chunks
    print("\nStep 4: Tagging chunks with metadata...")
    if skip_tagging:
        print("  Skipping tagging (--no-tag flag)")
        # Add empty metadata to each chunk
        for chunk in chunks:
            chunk["metadata"] = {
                "streets": [],
                "board_textures": [],
                "pot_types": [],
                "positions": [],
                "concepts": [],
                "stack_depths": [],
                "has_range_data": False,
                "has_ev_calculations": False,
                "has_examples": False,
                "difficulty": "intermediate",
            }
        save_corpus(chunks, corpus_path)
    elif skip_existing and corpus_path.exists():
        print("  Loading existing corpus...")
        with open(corpus_path) as f:
            chunks = json.load(f)
    else:
        chunks = tag_all_chunks(chunks, api_key=api_key, checkpoint_path=str(corpus_path))
        save_corpus(chunks, corpus_path)

    if not skip_tagging:
        print_metadata_summary(chunks)

    # Step 5: Generate report
    print("\nStep 5: Generating report...")
    generate_report(raw_text, sections, chunks, report_path)
    print(f"  Report saved to {report_path}")

    print("\n" + "=" * 60)
    print("EXTRACTION COMPLETE")
    print("=" * 60)
    print(f"Final corpus: {corpus_path}")
    print(f"Chunks: {len(chunks)}")

    return chunks


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Extract and chunk Janda textbook for RAG"
    )
    parser.add_argument(
        "--pdf",
        default=DEFAULT_PDF_PATH,
        help="Path to Janda PDF"
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).parent / "output"),
        help="Output directory"
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Use simple chunking instead of LLM"
    )
    parser.add_argument(
        "--no-tag",
        action="store_true",
        help="Skip the LLM tagging step"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess even if intermediate files exist"
    )

    args = parser.parse_args()

    # Check for API key if using LLM
    needs_api = (not args.no_llm) or (not args.no_tag)
    if needs_api and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable required for LLM operations")
        print("Use --no-llm --no-tag for fully offline processing")
        sys.exit(1)

    # Check PDF exists
    if not Path(args.pdf).exists():
        print(f"Error: PDF not found: {args.pdf}")
        sys.exit(1)

    run_extraction_pipeline(
        pdf_path=args.pdf,
        output_dir=args.output,
        use_llm_chunking=not args.no_llm,
        skip_tagging=args.no_tag,
        skip_existing=not args.force
    )


if __name__ == "__main__":
    main()
