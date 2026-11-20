"""Max-flow and matching kernels.

Graphs in this problem are tiny (source, sink, at most two generation nodes,
ten shares per generation, and at most 63 custodians), so Dinic is both simple
and fast enough.  Edges are returned as mutable lists so callers can inspect
saturation and find the residual strongly connected components, which is how
the witness builder decides whether a copy occurs in *some* maximum flow.
"""

from __future__ import annotations

from collections import deque
from typing import List

INF = 1 << 30

Edge = List[int]  # [head, capacity, reverse_index]


class Dinic:
    """Adjacency-list Dinic maximum flow."""

    __slots__ = ("size", "graph")

    def __init__(self, size: int) -> None:
        self.size = size
        self.graph: List[List[Edge]] = [[] for _ in range(size)]

    def add(self, tail: int, head: int, capacity: int) -> Edge:
        forward: Edge = [head, capacity, len(self.graph[head])]
        backward: Edge = [tail, 0, len(self.graph[tail])]
        self.graph[tail].append(forward)
        self.graph[head].append(backward)
        return forward

    def max_flow(self, source: int, sink: int) -> int:
        graph = self.graph
        size = self.size
        total = 0
        while True:
            level = [-1] * size
            level[source] = 0
            queue = deque([source])
            while queue:
                tail = queue.popleft()
                for head, capacity, _ in graph[tail]:
                    if capacity > 0 and level[head] < 0:
                        level[head] = level[tail] + 1
                        queue.append(head)
            if level[sink] < 0:
                return total
            cursor = [0] * size
            while True:
                sent = self._push(source, sink, level, cursor, INF)
                if not sent:
                    break
                total += sent

    def _push(self, tail: int, sink: int, level: List[int], cursor: List[int],
              available: int) -> int:
        if tail == sink:
            return available
        edges = self.graph[tail]
        while cursor[tail] < len(edges):
            edge = edges[cursor[tail]]
            head, capacity = edge[0], edge[1]
            if capacity > 0 and level[head] == level[tail] + 1:
                sent = self._push(head, sink, level, cursor, min(available, capacity))
                if sent:
                    edge[1] -= sent
                    self.graph[head][edge[2]][1] += sent
                    return sent
            cursor[tail] += 1
        return 0

    def residual_scc(self) -> List[int]:
        """Component label of every node in the positive-capacity residual graph."""
        size = self.size
        adjacency: List[List[int]] = [[] for _ in range(size)]
        reverse: List[List[int]] = [[] for _ in range(size)]
        for tail in range(size):
            for head, capacity, _ in self.graph[tail]:
                if capacity > 0:
                    adjacency[tail].append(head)
                    reverse[head].append(tail)

        order: List[int] = []
        seen = [False] * size
        for root in range(size):
            if seen[root]:
                continue
            seen[root] = True
            stack = [(root, 0)]
            while stack:
                tail, cursor = stack[-1]
                if cursor < len(adjacency[tail]):
                    head = adjacency[tail][cursor]
                    stack[-1] = (tail, cursor + 1)
                    if not seen[head]:
                        seen[head] = True
                        stack.append((head, 0))
                else:
                    order.append(tail)
                    stack.pop()

        component = [-1] * size
        label = 0
        for root in reversed(order):
            if component[root] != -1:
                continue
            component[root] = label
            stack = [root]
            while stack:
                tail = stack.pop()
                for head in reverse[tail]:
                    if component[head] == -1:
                        component[head] = label
                        stack.append(head)
            label += 1
        return component


def maximum_bipartite_matching(adjacency: Sequence[Sequence[int]],
                               right_size: int) -> int:
    """Hopcroft-Karp matching size; used by the exhaustive test oracle.

    ``adjacency[left]`` lists reachable right vertices.  Kept deliberately
    independent from :class:`Dinic` so the oracle does not share a bug with the
    engine.
    """
    left_size = len(adjacency)
    match_left = [-1] * left_size
    match_right = [-1] * right_size
    distance = [0] * left_size
    matched = 0

    def bfs() -> bool:
        queue = deque()
        for node in range(left_size):
            if match_left[node] < 0:
                distance[node] = 0
                queue.append(node)
            else:
                distance[node] = -1
        found = False
        while queue:
            node = queue.popleft()
            for other in adjacency[node]:
                paired = match_right[other]
                if paired < 0:
                    found = True
                elif distance[paired] < 0:
                    distance[paired] = distance[node] + 1
                    queue.append(paired)
        return found

    def dfs(node: int) -> bool:
        for other in adjacency[node]:
            paired = match_right[other]
            if paired < 0 or (distance[paired] == distance[node] + 1 and dfs(paired)):
                match_left[node] = other
                match_right[other] = node
                return True
        distance[node] = -1
        return False

    while bfs():
        for node in range(left_size):
            if match_left[node] < 0 and dfs(node):
                matched += 1
    return matched
