"""Vector store for poker concepts and textbook using Pinecone."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pinecone import Pinecone, ServerlessSpec


# Namespace constants
CONCEPTS_NAMESPACE = "concepts"
TEXTBOOK_NAMESPACE = "textbook"
TERMS_NAMESPACE = "terms"
JANDA_NAMESPACE = "janda-textbook"


class PokerVectorStore:
    """Vector store with namespaces: concepts, textbook, terms, and janda-textbook."""

    def __init__(self, index_name: str = "poker-rag"):
        """
        Initialize Pinecone vector store.

        Args:
            index_name: Name of the Pinecone index.
        """
        api_key = os.getenv("PINECONE_API_KEY")
        if not api_key:
            raise ValueError("PINECONE_API_KEY not set")

        self.pc = Pinecone(api_key=api_key)
        self.index_name = index_name
        self.model = "multilingual-e5-large"  # Pinecone's built-in embedding model

        # Create index if doesn't exist
        existing_indexes = [idx.name for idx in self.pc.list_indexes()]
        if index_name not in existing_indexes:
            print(f"Creating index '{index_name}'...")
            self.pc.create_index(
                name=index_name,
                dimension=1024,  # multilingual-e5-large dimension
                metric="cosine",
                spec=ServerlessSpec(cloud="aws", region="us-east-1")
            )

        self.index = self.pc.Index(index_name)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using Pinecone's inference API with retry."""
        import time

        max_retries = 5
        for attempt in range(max_retries):
            try:
                embeddings = self.pc.inference.embed(
                    model=self.model,
                    inputs=texts,
                    parameters={"input_type": "passage"}
                )
                return [e.values for e in embeddings.data]
            except Exception as e:
                if "429" in str(e) or "rate" in str(e).lower():
                    wait = 30 * (2 ** attempt)
                    print(f"    Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise
        raise Exception("Max retries exceeded")

    def _embed_query(self, query: str) -> list[float]:
        """Embed a query using Pinecone's inference API."""
        embeddings = self.pc.inference.embed(
            model=self.model,
            inputs=[query],
            parameters={"input_type": "query"}
        )
        return embeddings.data[0].values

    def index_concepts(self, concepts_path: str):
        """
        Index concepts from JSON file.

        Args:
            concepts_path: Path to concepts JSON file.
        """
        with open(concepts_path) as f:
            concepts = json.load(f)

        # Build documents
        docs = []
        for c in concepts:
            doc = f"{c['name']}. {c['key_insight']}"
            if c.get("explanation"):
                doc += f" {c['explanation']}"
            docs.append({
                "id": c["id"],
                "text": doc,
                "metadata": {
                    "type": "concept",
                    "name": c["name"],
                    "insight": c["key_insight"][:500],  # Pinecone metadata limit
                    "chapter": c.get("source_chapter", "")[:100],
                }
            })

        # Embed in batches
        batch_size = 96
        for i in range(0, len(docs), batch_size):
            batch = docs[i:i + batch_size]
            texts = [d["text"] for d in batch]
            embeddings = self._embed(texts)

            vectors = [
                {
                    "id": f"concept_{d['id']}",
                    "values": emb,
                    "metadata": d["metadata"]
                }
                for d, emb in zip(batch, embeddings)
            ]

            self.index.upsert(vectors=vectors, namespace="concepts")
            print(f"  Indexed concepts {i+1}-{min(i+batch_size, len(docs))}")

        print(f"Indexed {len(docs)} concepts")

    def index_textbook(self, pdf_path: str, chapters: list[dict]):
        """
        Index textbook chunks from PDF.

        Args:
            pdf_path: Path to PDF file.
            chapters: List of {"name": str, "start": int, "end": int}.
        """
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)

        chunks = []
        chunk_id = 0

        for chapter in chapters:
            chapter_name = chapter["name"]
            start_page = chapter["start"] - 1
            end_page = min(chapter["end"], len(reader.pages))

            # Extract chapter text
            chapter_text = ""
            for i in range(start_page, end_page):
                chapter_text += reader.pages[i].extract_text() + "\n"

            # Split into chunks
            chunk_size = 1500
            overlap = 150

            for i in range(0, len(chapter_text), chunk_size - overlap):
                chunk = chapter_text[i:i + chunk_size].strip()
                if len(chunk) < 100:
                    continue

                chunk_id += 1
                chunks.append({
                    "id": f"chunk_{chunk_id}",
                    "text": chunk,
                    "metadata": {
                        "type": "textbook",
                        "chapter": chapter_name,
                        "start_page": chapter["start"],
                    }
                })

        # Embed in batches
        batch_size = 96
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            texts = [c["text"] for c in batch]
            embeddings = self._embed(texts)

            vectors = [
                {
                    "id": c["id"],
                    "values": emb,
                    "metadata": {**c["metadata"], "text": c["text"][:1000]}  # Store truncated text
                }
                for c, emb in zip(batch, embeddings)
            ]

            self.index.upsert(vectors=vectors, namespace="textbook")
            print(f"  Indexed chunks {i+1}-{min(i+batch_size, len(chunks))}")

        print(f"Indexed {len(chunks)} textbook chunks")

    def search_concepts(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Search for relevant concepts.

        Returns:
            List of concept dicts with id, name, insight, score.
        """
        query_embedding = self._embed_query(query)

        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace="concepts",
            include_metadata=True
        )

        return [
            {
                "id": m.id.replace("concept_", ""),
                "name": m.metadata.get("name", ""),
                "insight": m.metadata.get("insight", ""),
                "chapter": m.metadata.get("chapter", ""),
                "score": m.score
            }
            for m in results.matches
        ]

    def search_textbook(self, query: str, top_k: int = 3) -> list[dict]:
        """
        Search for relevant textbook passages.

        Returns:
            List of dicts with text, chapter, score.
        """
        query_embedding = self._embed_query(query)

        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace="textbook",
            include_metadata=True
        )

        return [
            {
                "text": m.metadata.get("text", ""),
                "chapter": m.metadata.get("chapter", ""),
                "score": m.score
            }
            for m in results.matches
        ]

    def index_terms(self, terms_path: str):
        """
        Index poker terms from local_content.json into Pinecone.

        Args:
            terms_path: Path to local_content.json file.
        """
        with open(terms_path) as f:
            terms = json.load(f)

        documents = []
        for term_id, term_data in terms.items():
            # Combine name + blurb + body for searchable text
            searchable_text = f"{term_data['name']}: {term_data['blurb']}"
            if term_data.get('body'):
                searchable_text += f" {term_data['body']}"

            documents.append({
                "id": f"term_{term_id}",
                "text": searchable_text,
                "metadata": {
                    "type": "term",
                    "term_id": term_id,
                    "name": term_data["name"],
                    "blurb": term_data["blurb"],
                    "body": term_data.get("body", "") or "",
                    "category": term_data.get("category", "")
                }
            })

        # Embed in batches
        batch_size = 96
        for i in range(0, len(documents), batch_size):
            batch = documents[i:i + batch_size]
            texts = [d["text"] for d in batch]
            embeddings = self._embed(texts)

            vectors = [
                {
                    "id": d["id"],
                    "values": emb,
                    "metadata": d["metadata"]
                }
                for d, emb in zip(batch, embeddings)
            ]

            self.index.upsert(vectors=vectors, namespace="terms")
            print(f"  Indexed terms {i+1}-{min(i+batch_size, len(documents))}")

        print(f"Indexed {len(documents)} terms")

    def search_terms(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Search for relevant poker terms.

        Returns:
            List of term dicts with term_id, name, blurb, body, category, score.
        """
        query_embedding = self._embed_query(query)

        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace="terms",
            include_metadata=True
        )

        return [
            {
                "term_id": m.metadata.get("term_id", ""),
                "name": m.metadata.get("name", ""),
                "blurb": m.metadata.get("blurb", ""),
                "body": m.metadata.get("body", ""),
                "category": m.metadata.get("category", ""),
                "score": m.score
            }
            for m in results.matches
        ]

    def search(self, query: str, n_concepts: int = 3, n_textbook: int = 2) -> dict:
        """
        Search both concepts and textbook.

        Returns:
            {"concepts": [...], "textbook": [...]}
        """
        return {
            "concepts": self.search_concepts(query, n_concepts),
            "textbook": self.search_textbook(query, n_textbook)
        }

    def index_janda(self, corpus_path: str) -> None:
        """
        Index Janda corpus with full metadata for filtered retrieval.

        Each chunk stored with:
        - Embedding of: title + summary + text[:1500]
        - Metadata: all fields from corpus (streets, concepts, pot_types, etc.)

        Args:
            corpus_path: Path to janda_corpus.json file.
        """
        with open(corpus_path) as f:
            corpus = json.load(f)

        print(f"Indexing {len(corpus)} Janda chunks...")

        # Build documents for embedding
        documents = []
        for chunk in corpus:
            # Create rich embedding text: title + summary + text snippet
            text_snippet = chunk.get("text", "")[:1500]
            embed_text = f"{chunk['title']}. {chunk.get('summary', '')}. {text_snippet}"

            # Extract metadata for filtering
            meta = chunk.get("metadata", {})

            # Pinecone metadata (all fields for filtering + display)
            pinecone_meta: dict[str, Any] = {
                # Core identification
                "chunk_id": chunk["chunk_id"],
                "title": chunk["title"],
                "part": chunk.get("part", 0),
                "name": chunk.get("name", ""),

                # Filterable arrays (Pinecone uses $in operator)
                "streets": meta.get("streets", []),
                "board_textures": meta.get("board_textures", []),
                "pot_types": meta.get("pot_types", []),
                "positions": meta.get("positions", []),
                "concepts": meta.get("concepts", []),
                "stack_depths": meta.get("stack_depths", []),

                # Boolean flags
                "has_range_data": meta.get("has_range_data", False),
                "has_ev_calculations": meta.get("has_ev_calculations", False),
                "has_examples": meta.get("has_examples", False),
                "difficulty": meta.get("difficulty", "intermediate"),

                # Content for display (truncated for Pinecone limits)
                "summary": chunk.get("summary", "")[:500],
                "text": chunk.get("text", "")[:2000],
            }

            documents.append({
                "id": chunk["chunk_id"],
                "text": embed_text,
                "metadata": pinecone_meta
            })

        # Embed in batches
        batch_size = 50  # Smaller batches for rate limiting
        for i in range(0, len(documents), batch_size):
            batch = documents[i:i + batch_size]
            texts = [d["text"] for d in batch]
            embeddings = self._embed(texts)

            vectors = [
                {
                    "id": d["id"],
                    "values": emb,
                    "metadata": d["metadata"]
                }
                for d, emb in zip(batch, embeddings)
            ]

            self.index.upsert(vectors=vectors, namespace=JANDA_NAMESPACE)
            print(f"  Indexed Janda chunks {i+1}-{min(i+batch_size, len(documents))}")

        print(f"Indexed {len(documents)} Janda chunks to namespace '{JANDA_NAMESPACE}'")

    def search_janda(
        self,
        query: str,
        filters: dict[str, list[str]] | None = None,
        top_k: int = 3,
        strict_filters: bool = False
    ) -> list[dict]:
        """
        Search Janda corpus with optional metadata filters.

        Args:
            query: Semantic search query.
            filters: Optional metadata filters, e.g.:
                {"streets": ["flop"], "pot_types": ["single_raised"]}
                Uses Pinecone's $in operator for array fields.
            top_k: Number of results to return.
            strict_filters: If True, apply all filters. If False (default),
                only apply 'streets' filter to avoid over-filtering on
                sparse metadata fields like board_textures.

        Returns:
            List of dicts with chunk_id, title, text, summary, metadata, score.
        """
        query_embedding = self._embed_query(query)

        # Build Pinecone filter from our filter dict
        pinecone_filter = None
        if filters:
            if strict_filters:
                # Apply all filters
                pinecone_filter = self._build_pinecone_filter(filters)
            else:
                # Only apply street filter (most reliable metadata)
                # Other context is better handled by semantic search
                street_filter = {k: v for k, v in filters.items() if k == "streets"}
                if street_filter:
                    pinecone_filter = self._build_pinecone_filter(street_filter)

        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace=JANDA_NAMESPACE,
            include_metadata=True,
            filter=pinecone_filter
        )

        return [
            {
                "chunk_id": m.metadata.get("chunk_id", m.id),
                "title": m.metadata.get("title", ""),
                "text": m.metadata.get("text", ""),
                "summary": m.metadata.get("summary", ""),
                "part": m.metadata.get("part", 0),
                "name": m.metadata.get("name", ""),
                "metadata": {
                    "streets": m.metadata.get("streets", []),
                    "board_textures": m.metadata.get("board_textures", []),
                    "pot_types": m.metadata.get("pot_types", []),
                    "positions": m.metadata.get("positions", []),
                    "concepts": m.metadata.get("concepts", []),
                    "difficulty": m.metadata.get("difficulty", ""),
                    "has_examples": m.metadata.get("has_examples", False),
                },
                "score": m.score
            }
            for m in results.matches
        ]

    def _build_pinecone_filter(
        self,
        filters: dict[str, list[str]]
    ) -> dict[str, Any]:
        """
        Build Pinecone filter from our simplified filter dict.

        Pinecone filter syntax for arrays uses $in operator:
        {"streets": {"$in": ["flop", "turn"]}}

        For AND conditions across fields:
        {"$and": [{"streets": {"$in": ["flop"]}}, {"pot_types": {"$in": ["3-bet"]}}]}
        """
        conditions = []

        for field, values in filters.items():
            if not values:
                continue
            # Use $in for array membership
            conditions.append({field: {"$in": values}})

        if not conditions:
            return {}
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}


# Chapter definitions for indexing
GRINDERS_MANUAL_CHAPTERS = [
    {"name": "Opening the Pot", "start": 18, "end": 50},
    {"name": "ISO Raises", "start": 50, "end": 80},
    {"name": "C-Betting", "start": 80, "end": 120},
    {"name": "Value Betting", "start": 120, "end": 160},
    {"name": "Calling Opens", "start": 160, "end": 220},
    {"name": "Facing Bets - End of Action", "start": 220, "end": 260},
    {"name": "Facing Bets - Open Action", "start": 260, "end": 300},
    {"name": "Combos and Blockers", "start": 300, "end": 340},
    {"name": "3-Betting", "start": 340, "end": 390},
    {"name": "Facing 3-Bets", "start": 390, "end": 440},
    {"name": "Bluffing Turn and River", "start": 440, "end": 490},
]
