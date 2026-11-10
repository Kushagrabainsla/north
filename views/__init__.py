"""Views north makes for itself: pages on the dashboard, drawn only from its UI kit.

North writes a view as A2UI v0.9 JSON using the parts in `views.catalog`. It is checked
here (`check_view`), saved as a draft you can open, and added to the Views page once you
approve the card that asks. Its data comes from tools that change nothing; its buttons run
flows, whose steps ask as usual.
"""

from __future__ import annotations

from views.catalog import CATALOG_ID, catalog_document, describe_catalog
from views.document import check_view, messages, summary
from views.store import ViewStore

__all__ = ["CATALOG_ID", "ViewStore", "catalog_document", "check_view", "describe_catalog", "messages", "summary"]
