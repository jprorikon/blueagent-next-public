"""對話管線的階段：normalize → recall → persona → state → brain → voice → memory.write → egress。

一類階段一個模組；這裡把名字匯總出來（`from agent.brain.compose.stages import X` 照用）。
"""

from __future__ import annotations

from agent.brain.compose.stages.intake import NormalizeStage, VisionStage  # noqa: F401
from agent.brain.compose.stages.recall import RecallStage, SubAgentStage  # noqa: F401
from agent.brain.compose.stages.persona import SPEAK_LANGS, PersonaStage  # noqa: F401
from agent.brain.compose.stages.state_elicit import StateElicitStage  # noqa: F401
from agent.brain.compose.stages.speech_text import _SENTENCE_END, _MIN_SENTENCE, _squash, _remaining_after_spoken, _take_sentences  # noqa: F401
from agent.brain.compose.stages.brain import _current_game_block, BrainStage  # noqa: F401
from agent.brain.compose.stages.memory_write import MemoryUsageStage, _overlaps, MemoryWriteStage, MAX_RECENT_PER_TURN, _FACT_VERBS, _fact_prefix  # noqa: F401
from agent.brain.compose.stages.egress import VoiceEgressStage, ArtifactStage, EgressStage  # noqa: F401

__all__ = ["ArtifactStage", "BrainStage", "EgressStage", "MAX_RECENT_PER_TURN", "MemoryUsageStage", "MemoryWriteStage", "NormalizeStage", "PersonaStage", "RecallStage", "SPEAK_LANGS", "StateElicitStage", "SubAgentStage", "VisionStage", "VoiceEgressStage"]
