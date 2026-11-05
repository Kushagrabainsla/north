"""Workspace and task context helpers.

The user-memory layer (documents, facts, episodes, embeddings, extraction, and
the gateway) now lives in the ``memory`` package. This package keeps only the
workspace conventions loader.
"""

from context.repo_instructions import load_repo_instructions

__all__ = ["load_repo_instructions"]
