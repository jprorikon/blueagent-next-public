"""應用層的 Ports：把記憶與腦包成管線協議。"""

from __future__ import annotations

from agent.brain.ports import BrainPort, BrainRequest, ProbeMemoryPort


class AppPorts:
    """記憶與腦的轉接：只認協議（任何記憶後端、任何大腦都行），不認具體實作。"""

    def __init__(self, memory: ProbeMemoryPort, brain: BrainPort) -> None:
        self.memory = memory
        self.brain = brain

    def recall(self, scope, query, *, limit: int = 4, participants=(), stream: str = "",
               game: int | None = None):
        return self.memory.recall(scope, query, limit=limit, participants=participants,
                                  stream=stream, game=game)

    def recall_detail(self, scope, query, *, limit: int = 4, participants=(), stream: str = "",
                      game: int | None = None):
        return self.memory.recall_detail(scope, query, limit=limit,
                                         participants=participants, stream=stream, game=game)

    def record(self, scope, turn, *, participants=()) -> None:
        self.memory.record(scope, turn, participants=participants)

    def probe(self, scope, query, *, participants=()) -> int:
        return self.memory.probe(scope, query, participants=participants)

    def run_turn(self, request: BrainRequest):
        return self.brain.run_turn(request)

    def speak(self, scope, text, *, thread_id: str, epoch: int):
        return iter(())

    def abort(self, thread_id: str) -> None:
        return None

    def send(self, frame) -> None:
        return None
