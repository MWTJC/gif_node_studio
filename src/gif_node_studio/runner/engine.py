from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class NodeStatus(str, Enum):
    DIRTY = "dirty"
    RUNNING = "running"
    CLEAN = "clean"
    ERROR = "error"


@dataclass
class RuntimeNode:
    executor: Callable[[list[Any], dict[str, Any]], Any]
    params: dict[str, Any] = field(default_factory=dict)
    status: NodeStatus = NodeStatus.DIRTY
    output: Any = None
    error: str | None = None


class ExecutionGraph:
    def __init__(self) -> None:
        self._nodes: dict[str, RuntimeNode] = {}
        self._upstream: dict[str, list[str]] = {}
        self._downstream: dict[str, list[str]] = {}

    def add_node(self, node_id: str, executor: Callable[[list[Any], dict[str, Any]], Any], params: dict[str, Any] | None = None) -> None:
        if node_id in self._nodes:
            raise ValueError(f"duplicate node: {node_id}")
        self._nodes[node_id] = RuntimeNode(executor, dict(params or {}))
        self._upstream[node_id], self._downstream[node_id] = [], []

    def connect(self, source: str, target: str) -> None:
        if source == target or self._reachable(target, source):
            raise ValueError("connection would create a cycle")
        if source not in self._upstream[target]:
            self._upstream[target].append(source)
            self._downstream[source].append(target)
            self.mark_dirty(target)

    def _reachable(self, start: str, goal: str) -> bool:
        queue = deque([start])
        seen: set[str] = set()
        while queue:
            node = queue.popleft()
            if node == goal:
                return True
            if node not in seen:
                seen.add(node)
                queue.extend(self._downstream.get(node, ()))
        return False

    def mark_dirty(self, node_id: str) -> None:
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            self._nodes[current].status = NodeStatus.DIRTY
            queue.extend(self._downstream[current])

    def set_params(self, node_id: str, params: dict[str, Any]) -> None:
        if self._nodes[node_id].params != params:
            self._nodes[node_id].params = dict(params)
            self.mark_dirty(node_id)

    def status(self, node_id: str) -> NodeStatus:
        return self._nodes[node_id].status

    def run_to(self, node_id: str) -> Any:
        for current in self._ancestor_order(node_id):
            node = self._nodes[current]
            if node.status is NodeStatus.CLEAN:
                continue
            node.status = NodeStatus.RUNNING
            try:
                node.output = node.executor([self._nodes[n].output for n in self._upstream[current]], dict(node.params))
            except Exception as exc:
                node.status, node.error = NodeStatus.ERROR, str(exc)
                raise
            node.status, node.error = NodeStatus.CLEAN, None
        return self._nodes[node_id].output

    def _ancestor_order(self, target: str) -> list[str]:
        order: list[str] = []
        seen: set[str] = set()
        def visit(node: str) -> None:
            if node in seen:
                return
            seen.add(node)
            for parent in self._upstream[node]:
                visit(parent)
            order.append(node)
        visit(target)
        return order
