"""领域契约：模型只提出补丁，合并与校验由程序完成。"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scene: str = ""
    current_mood: str = ""
    target_mood: str = ""
    audio_query: str = ""
    lyrics_query: str = ""
    count: int = Field(default=10, ge=1, le=100)
    count_mode: Literal["approx", "exact"] = "approx"
    minutes: float | None = Field(default=None, gt=0, le=1440)
    duration_mode: Literal["none", "max", "approx"] = "none"
    exclude_artists: list[str] = Field(default_factory=list)
    exclude_tracks: list[str] = Field(default_factory=list)
    language: str | None = None
    vocal: Literal["any", "instrumental", "vocal"] = "any"
    mix_types: bool = False
    negative_preferences: list[str] = Field(default_factory=list)
    negative_lyrics: list[str] = Field(default_factory=list)


class IntentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updates: dict = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)
    clarification: str | None = None


def merge_intent(previous: dict, patch: IntentPatch) -> Intent:
    """未提及字段保持不变；clear 是明确取消，不等价于省略。"""
    values = Intent.model_validate(previous).model_dump()
    defaults = Intent().model_dump()
    for name in patch.clear:
        if name not in defaults:
            raise ValueError(f"未知取消字段：{name}")
        values[name] = defaults[name]
    values.update(patch.updates)
    intent = Intent.model_validate(values)
    if intent.minutes is None:
        intent.duration_mode = "none"
    elif intent.duration_mode == "none":
        if not patch.clarification:
            raise ValueError("有时长但缺少上限/大约语义，需要澄清")
        # 暂存已确认的时长数字，含义仍等待用户确认；此状态不能进入检索/组合。
    return intent
