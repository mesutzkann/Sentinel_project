"""Hybrid retrieval: ingestion, chunking, embeddings, the chunk store, and four retrievers.

Import from the modules rather than from here. The package deliberately re-exports nothing:
``rag.store`` pulls in SQLAlchemy and ``rag.embeddings`` pulls in an HTTP client, and a chunker
test should not need either running to import the thing it is testing.
"""
