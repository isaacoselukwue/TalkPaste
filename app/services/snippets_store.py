"""Per-user snippets (trigger phrase → expansion) persistence.

Snippets are longer expansions keyed by a short spoken trigger, e.g.::

    {
      "insert signature": "Kind regards,\\nIsaac",
      "current address": "1 Example Street, London"
    }

Mechanically identical to the dictionary (case-insensitive whole-phrase
replacement) so it reuses :class:`~app.services.dictionary_store.MappingStore`.
The distinction is intent and ordering: snippets are expanded *before*
dictionary corrections in the formatter.
"""

from __future__ import annotations

from app.services.dictionary_store import MappingStore


class SnippetStore(MappingStore):
    """Expandable text snippets keyed by a spoken trigger phrase."""

    label = "snippets"
