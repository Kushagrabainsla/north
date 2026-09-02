"""Context documents and manual context injection."""

from __future__ import annotations

from fastapi import Form, HTTPException, UploadFile
from pydantic import BaseModel

from memory.gateway import LocalMemoryGateway
from memory.models import ContextDocument
from orchestrator.api.deps import _get_context_injector, _get_context_store, router

_VALID_DOCS = {d.value.replace(".md", ""): d for d in ContextDocument}


def _resolve_doc(doc: str) -> ContextDocument:
    key = doc.replace(".md", "")
    if key not in _VALID_DOCS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown document {doc!r}. Valid: {list(_VALID_DOCS)}",
        )
    return _VALID_DOCS[key]


class ContextDocOut(BaseModel):
    document: str
    content: str


class ContextWriteRequest(BaseModel):
    content: str


@router.get("/context/{doc}", response_model=ContextDocOut)
async def read_context(doc: str) -> ContextDocOut:
    """Read a context document."""
    document = _resolve_doc(doc)
    context_store = _get_context_store()
    # soul.md has a shipped persona fallback when no user override exists.
    # Use the same gateway path as agents so the Memory UI shows the effective
    # document rather than an empty editor for a missing override.
    content = (
        await LocalMemoryGateway(context_store).read_persona()
        if document is ContextDocument.SOUL
        else await context_store.read(document)
    )
    return ContextDocOut(document=document.value, content=content)


@router.put("/context/{doc}", status_code=204)
async def write_context(doc: str, body: ContextWriteRequest) -> None:
    """Overwrite a context document entirely."""
    document = _resolve_doc(doc)
    await _get_context_store().write(document, body.content)


@router.delete("/context/{doc}", status_code=204)
async def delete_context(doc: str) -> None:
    """Delete a user-customized context document."""
    document = _resolve_doc(doc)
    delete = getattr(_get_context_store(), "delete", None)
    if delete is None:
        raise HTTPException(status_code=405, detail="This context store does not support deletion")
    await delete(document)


@router.post("/context/add", status_code=202)
async def add_context(
    text: str | None = Form(None),
    url: str | None = Form(None),
    file: UploadFile | None = None,
) -> dict[str, str]:
    """Manual context injection: accepts text, URL, or file upload (multipart form)."""
    injector = _get_context_injector()
    if file is not None:
        content = await file.read()
        doc = await injector.inject_file(file.filename or "upload", content)
        return {"document": doc.value, "source": f"file:{file.filename}"}
    if url:
        doc = await injector.inject_url(url)
        return {"document": doc.value, "source": f"url:{url}"}
    if text:
        doc = await injector.inject_text(text)
        return {"document": doc.value, "source": "text"}
    raise HTTPException(status_code=422, detail="Provide text, url, or a file upload")


