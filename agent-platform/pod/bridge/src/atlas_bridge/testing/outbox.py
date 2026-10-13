"""RecordingOutbox：只记录、不发送的 Outbox，用于 session 层的测试。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from atlas_host import Model

from ..session.ports import MessageKind

__all__ = ["Recorded", "RecordingOutbox"]


@dataclass(frozen=True, slots=True)
class Recorded:
    seq: int
    method: str
    kind: MessageKind
    params: Model
    #: 请求才有
    request_id: int | None = None

    @property
    def wire(self) -> dict[str, Any]:
        return self.params.to_wire()


class RecordingOutbox:
    def __init__(self) -> None:
        self.messages: list[Recorded] = []
        self._seq = 0

    def put_notification(
        self, method: str, build: Callable[[int], Model], *, kind: MessageKind
    ) -> int:
        self._seq += 1
        self.messages.append(Recorded(self._seq, method, kind, build(self._seq)))
        return self._seq

    def put_request(self, method: str, build: Callable[[int], Model], *, request_id: int) -> int:
        self._seq += 1
        self.messages.append(Recorded(self._seq, method, "control", build(self._seq), request_id))
        return self._seq

    async def wait_writable(self) -> None:
        return None

    def resend_plan(self, last_seq: int) -> tuple[int, list[tuple[int, int]]]:
        return max(last_seq, 0) + 1, []

    def of(self, method: str) -> list[dict[str, Any]]:
        return [m.wire for m in self.messages if m.method == method]

    def turn_states(self, turn_id: str | None = None) -> list[str]:
        return [
            p["state"] for p in self.of("turn.state") if turn_id is None or p["turnId"] == turn_id
        ]
