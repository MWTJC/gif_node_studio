from __future__ import annotations

from typing import Any

from ..media.backend import MediaBackend
from ..nodes.registry import node_class_by_kind


def execute_node(kind: str, inputs: list[Any], params: dict[str, Any], backend: MediaBackend) -> Any:
    """Compatibility adapter for callers that still persist or transmit a node kind."""
    return node_class_by_kind(kind).execute(inputs, params, backend)
