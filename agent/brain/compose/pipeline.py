"""管線組裝：名稱 → stage 實例；與 bootstrap 解耦。"""

from __future__ import annotations

from agent.brain.ports import BrainPort, ProbeMemoryPort
from agent.brain.compose.stages import (ArtifactStage, BrainStage, EgressStage,
                                        MemoryUsageStage, MemoryWriteStage, NormalizeStage,
                                        StateElicitStage,
                                        PersonaStage, RecallStage, SubAgentStage,
                                        VisionStage, VoiceEgressStage)
from agent.brain.pipelines.base import Pipeline, PipelineRegistry
from agent.brain.runtime.stance import StanceService
from agent.brain.runtime.tools import ToolRegistry
from agent.brain.runtime.voice import VoiceService


def build_pipeline(entities, principals, social, memory: ProbeMemoryPort,
                   brain: BrainPort, voice: VoiceService,
                   tools: ToolRegistry,
                   stances: StanceService, *,
                   speak_lang: str = "", soul=None, userfile=None,
                   activity_log=None, affect=None,
                   prompt_stats: dict | None = None,
                   persona_cores: dict | None = None, llm_call=None,
                   orchestrator=None, prethinker=None, skills=None,
                    memory_usage=None, timeline=None,
                   log=None, log_path=None, motion=None, search=None,
                   activity_store=None, deliveries=None,
                   elsewhere=None) -> tuple[PipelineRegistry, object]:
    from agent.brain.compose.ports import AppPorts

    ports = AppPorts(memory, brain)
    stages = (
        NormalizeStage(),
        # `search`＝混合檢索（字面＋向量＋窗口摘要）；沒有就退回記憶埠的字面召回
        RecallStage(search),
        SubAgentStage(orchestrator, prethinker),
        PersonaStage(entities, principals, social, stances, speak_lang=speak_lang,
                     soul=soul, userfile=userfile, activity_log=activity_log,
                     affect=affect, prompt_stats=prompt_stats,
                     persona_cores=persona_cores, skills=skills, timeline=timeline,
                     tools=tools, activity_store=activity_store, deliveries=deliveries,
                     elsewhere=elsewhere),
        StateElicitStage(cores=persona_cores, affect=affect, call=llm_call,
                         log=log, log_path=log_path),
        BrainStage(tools, voice, motion=motion),
        MemoryUsageStage(memory_usage),
        VoiceEgressStage(voice, motion=motion),
        MemoryWriteStage(userfile=userfile),
        ArtifactStage(),
        EgressStage(),
    )
    # 看圖：第一格先把圖讀成文字，之後與打字那條一模一樣
    seeing = (VisionStage(),) + stages
    pipelines = PipelineRegistry()
    for pipeline_id in ("chat.text", "voice.realtime", "trpg.turn"):
        pipelines.register(Pipeline(id=pipeline_id, stages=stages))
    pipelines.register(Pipeline(id="media.see", stages=seeing))
    return pipelines, ports
