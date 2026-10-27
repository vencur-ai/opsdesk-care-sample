"""Read-only policy document tools."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import Field

from opsdesk.apps.base import AppTool, ToolArgs, ToolError, to_json_data
from opsdesk.core.state import WorldState


class ListDocumentsArgs(ToolArgs):
    """Filters for policy documents."""

    as_of: date | None = None
    include_expired: bool = False


class ReadDocumentArgs(ToolArgs):
    """Identifier for a policy document."""

    document_id: str = Field(min_length=1)


class ListDocumentsTool(AppTool):
    """List effective document metadata without exposing their contents."""

    app = "documents"
    name = "documents.list"
    description = "List policy-document titles, sources, and effective dates."
    args_model = ListDocumentsArgs

    def execute(self, state: WorldState, args: ListDocumentsArgs) -> list[dict[str, Any]]:
        as_of = args.as_of or state.current_date
        result = []
        for document in sorted(state.documents, key=lambda item: item.document_id):
            if not args.include_expired:
                if document.effective_from > as_of:
                    continue
                if document.effective_until and document.effective_until < as_of:
                    continue
            result.append(
                {
                    "document_id": document.document_id,
                    "title": document.title,
                    "source": document.source,
                    "effective_from": document.effective_from.isoformat(),
                    "effective_until": (
                        document.effective_until.isoformat()
                        if document.effective_until
                        else None
                    ),
                }
            )
        return result


class ReadDocumentTool(AppTool):
    """Read one policy document by stable identifier."""

    app = "documents"
    name = "documents.read"
    description = "Read the full text of one policy document by its exact document ID."
    args_model = ReadDocumentArgs

    def execute(self, state: WorldState, args: ReadDocumentArgs) -> dict[str, Any]:
        document = next(
            (item for item in state.documents if item.document_id == args.document_id),
            None,
        )
        if document is None:
            raise ToolError("Document not found.")
        return to_json_data(document)