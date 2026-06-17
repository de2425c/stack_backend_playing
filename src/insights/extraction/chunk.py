"""Step 3: Split sections into concept-unit chunks using LLM."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from anthropic import Anthropic


CHUNKING_PROMPT = """You are processing a section from Matthew Janda's "Applications of No-Limit Hold'em" poker textbook.

Your task is to identify NATURAL CONCEPT BOUNDARIES in this text. Each chunk should:
1. Cover ONE distinct strategic concept or topic
2. Be self-contained and make sense when read independently
3. Be 500-2000 tokens (roughly 2000-8000 characters)
4. Preserve examples and explanations with their concepts

Return a JSON array where each element has:
- "title": Short descriptive title (3-8 words)
- "start_marker": First 50 characters of where this chunk starts (exact text)
- "end_marker": Last 50 characters of where this chunk ends (exact text)
- "summary": 1-2 sentence summary of the concept covered

IMPORTANT:
- The markers must be EXACT substrings from the text
- Chunks should NOT overlap
- Cover ALL the text - don't skip anything
- If a section is small (<1500 chars), return it as a single chunk

TEXT TO CHUNK:
{text}

Return ONLY valid JSON array, no other text."""


def estimate_tokens(text: str) -> int:
    """Rough token estimate (4 chars per token for English)."""
    return len(text) // 4


def chunk_section_simple(section: dict, max_chars: int = 6000, overlap: int = 200) -> list[dict]:
    """
    Simple character-based chunking for fallback or small sections.

    Args:
        section: Section dict with text and metadata.
        max_chars: Maximum characters per chunk.
        overlap: Character overlap between chunks.

    Returns:
        List of chunk dicts.
    """
    text = section["text"]
    chunks = []
    chunk_num = 0

    # If small enough, keep as single chunk
    if len(text) <= max_chars:
        return [{
            "chunk_id": f"janda-p{section['part']}-001",
            "title": section["name"],
            "part": section["part"],
            "name": section["name"],
            "page_range": section["page_range"],
            "text": text,
            "summary": "",
            "char_count": len(text),
            "token_estimate": estimate_tokens(text),
        }]

    # Split by single newlines (PDF text often has single newlines between paragraphs)
    # Look for lines that end sentences (period, question, exclamation) as paragraph boundaries
    lines = text.split('\n')
    paragraphs = []
    current_para = ""

    for line in lines:
        line = line.strip()
        if not line:
            if current_para:
                paragraphs.append(current_para)
                current_para = ""
            continue

        if current_para:
            # Check if previous line ended a sentence and this looks like a new paragraph
            # (starts with capital letter or is short like a header)
            if (current_para[-1] in '.?!' and
                (line[0].isupper() or len(line) < 50)):
                paragraphs.append(current_para)
                current_para = line
            else:
                current_para += " " + line
        else:
            current_para = line

    if current_para:
        paragraphs.append(current_para)

    # Now chunk by paragraphs
    current_chunk = ""
    for para in paragraphs:
        if len(current_chunk) + len(para) + 2 > max_chars and current_chunk:
            chunk_num += 1
            chunks.append({
                "chunk_id": f"janda-p{section['part']}-{chunk_num:03d}",
                "title": f"{section['name']} (Part {chunk_num})",
                "part": section["part"],
                "name": section["name"],
                "page_range": section["page_range"],
                "text": current_chunk.strip(),
                "summary": "",
                "char_count": len(current_chunk),
                "token_estimate": estimate_tokens(current_chunk),
            })
            # Start new chunk with some overlap from end of previous
            if overlap and len(current_chunk) > overlap:
                # Find a good break point for overlap
                overlap_text = current_chunk[-overlap:]
                # Try to start at a sentence boundary
                sentence_end = overlap_text.find('. ')
                if sentence_end != -1:
                    overlap_text = overlap_text[sentence_end + 2:]
                current_chunk = overlap_text + "\n\n" + para
            else:
                current_chunk = para
        else:
            current_chunk += "\n\n" + para if current_chunk else para

    # Don't forget the last chunk
    if current_chunk.strip():
        chunk_num += 1
        chunks.append({
            "chunk_id": f"janda-p{section['part']}-{chunk_num:03d}",
            "title": f"{section['name']} (Part {chunk_num})",
            "part": section["part"],
            "name": section["name"],
            "page_range": section["page_range"],
            "text": current_chunk.strip(),
            "summary": "",
            "char_count": len(current_chunk),
            "token_estimate": estimate_tokens(current_chunk),
        })

    # If still no chunks (very long single paragraph), fall back to character-based
    if not chunks:
        for i in range(0, len(text), max_chars - overlap):
            chunk_num += 1
            chunk_text = text[i:i + max_chars]
            chunks.append({
                "chunk_id": f"janda-p{section['part']}-{chunk_num:03d}",
                "title": f"{section['name']} (Part {chunk_num})",
                "part": section["part"],
                "name": section["name"],
                "page_range": section["page_range"],
                "text": chunk_text.strip(),
                "summary": "",
                "char_count": len(chunk_text),
                "token_estimate": estimate_tokens(chunk_text),
            })

    return chunks


def chunk_section_llm(
    section: dict,
    client: Anthropic,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 3
) -> list[dict]:
    """
    Use Claude to identify concept boundaries for chunking.

    Args:
        section: Section dict with text and metadata.
        client: Anthropic client.
        model: Model to use.
        max_retries: Maximum retry attempts.

    Returns:
        List of chunk dicts with concept-aligned boundaries.
    """
    text = section["text"]

    # For small sections, use simple chunking
    if len(text) < 4000:
        print(f"    Section too small for LLM chunking, using simple split")
        return chunk_section_simple(section)

    # For very large sections, process in parts
    max_input_chars = 25000
    if len(text) > max_input_chars:
        print(f"    Section too large ({len(text):,} chars), splitting first")
        # Split roughly in half at a sentence boundary
        mid = len(text) // 2
        # Try to find a good split point - sentence ending with period followed by newline
        split_pos = text.rfind(".\n", mid - 2000, mid + 2000)
        if split_pos == -1:
            split_pos = text.rfind("\n", mid - 1000, mid + 1000)
        if split_pos == -1:
            split_pos = mid
        else:
            split_pos += 1  # Include the newline in first part

        # Process each half
        section1 = {**section, "text": text[:split_pos]}
        section2 = {**section, "text": text[split_pos:]}

        chunks1 = chunk_section_llm(section1, client, model, max_retries)
        chunks2 = chunk_section_llm(section2, client, model, max_retries)

        # Renumber chunk IDs
        all_chunks = chunks1 + chunks2
        for i, chunk in enumerate(all_chunks):
            chunk["chunk_id"] = f"janda-p{section['part']}-{i+1:03d}"

        return all_chunks

    prompt = CHUNKING_PROMPT.format(text=text[:max_input_chars])

    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4000,
                timeout=120.0,  # 2 minute timeout for longer chunking responses
                messages=[{"role": "user", "content": prompt}]
            )

            content = response.content[0].text

            # Parse JSON from response
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            boundaries = json.loads(content)

            # Convert boundaries to chunks
            chunks = []
            for i, boundary in enumerate(boundaries):
                # Find the text between markers
                start_marker = boundary.get("start_marker", "")
                end_marker = boundary.get("end_marker", "")

                # Find positions
                start_pos = text.find(start_marker[:30]) if start_marker else 0
                end_pos = text.find(end_marker[-30:]) if end_marker else len(text)

                if start_pos == -1:
                    start_pos = 0
                if end_pos == -1 or end_pos <= start_pos:
                    end_pos = len(text)
                else:
                    end_pos += len(end_marker[-30:]) if end_marker else 0

                chunk_text = text[start_pos:end_pos].strip()

                if chunk_text:  # Only add non-empty chunks
                    chunks.append({
                        "chunk_id": f"janda-p{section['part']}-{i+1:03d}",
                        "title": boundary.get("title", f"{section['name']} Part {i+1}"),
                        "part": section["part"],
                        "name": section["name"],
                        "page_range": section["page_range"],
                        "text": chunk_text,
                        "summary": boundary.get("summary", ""),
                        "char_count": len(chunk_text),
                        "token_estimate": estimate_tokens(chunk_text),
                    })

            if chunks:
                return chunks

            print(f"    No chunks extracted, falling back to simple split")
            return chunk_section_simple(section)

        except json.JSONDecodeError as e:
            print(f"    JSON parse error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return chunk_section_simple(section)

        except Exception as e:
            if "rate" in str(e).lower() or "429" in str(e):
                wait_time = 30 * (2 ** attempt)
                print(f"    Rate limited, waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            print(f"    Error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return chunk_section_simple(section)

    return chunk_section_simple(section)


def chunk_all_sections(
    sections: list[dict],
    use_llm: bool = True,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-6"
) -> list[dict]:
    """
    Chunk all sections into concept units.

    Args:
        sections: List of section dicts.
        use_llm: Whether to use LLM for intelligent chunking.
        api_key: Anthropic API key (uses env var if not provided).
        model: Model to use for LLM chunking.

    Returns:
        List of all chunks.
    """
    all_chunks = []

    if use_llm:
        client = Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
    else:
        client = None

    for section in sections:
        print(f"  Chunking: Part {section['part']} - {section['name']} "
              f"({section['char_count']:,} chars)")

        if use_llm and client:
            chunks = chunk_section_llm(section, client, model)
        else:
            chunks = chunk_section_simple(section)

        print(f"    -> {len(chunks)} chunks")
        all_chunks.extend(chunks)

        # Rate limiting pause
        if use_llm:
            time.sleep(1)

    return all_chunks


def save_chunks(chunks: list[dict], output_path: str | Path) -> None:
    """Save chunks to JSON file."""
    with open(output_path, "w") as f:
        json.dump(chunks, f, indent=2)


def load_chunks(path: str | Path) -> list[dict]:
    """Load chunks from JSON file."""
    with open(path) as f:
        return json.load(f)
