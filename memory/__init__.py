"""Unified memory layer: documents, facts, episodes, embeddings, and the gateway.

The gateway is the single read path over these stores, so per-agent and
per-tool permissions are enforced in one place. Facts and episodic stores are
imported directly from their modules (``memory.facts``, ``memory.episodic``) to
avoid an import cycle with the DI hub.
"""

from memory.base import ContextStore, MemoryGateway
from memory.documents import FileContextStore
from memory.exceptions import ContextError, ContextReadError, ContextWriteError
from memory.extraction import ExtractionPipeline
from memory.gateway import LocalMemoryGateway
from memory.injection import ContextInjector
from memory.models import ContextDocument, MemoryContext, MemoryPrincipal

__all__ = [
    "ContextDocument",
    "ContextError",
    "ContextInjector",
    "ContextReadError",
    "ContextStore",
    "ContextWriteError",
    "ExtractionPipeline",
    "FileContextStore",
    "LocalMemoryGateway",
    "MemoryContext",
    "MemoryGateway",
    "MemoryPrincipal",
]
