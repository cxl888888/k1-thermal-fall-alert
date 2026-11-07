from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

ID = Annotated[str, Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_.:-]+$')]
Origin = Literal['live', 'controlled_simulation', 'historical_replay', 'synthetic']
Route = Literal['normal', 'needs_assistance', 'emergency_help', 'fall_or_injury', 'uncertain', 'safety_response']


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class PairBody(StrictModel):
    code: Annotated[str, Field(min_length=24, max_length=128)]


class ContactPhoneBody(StrictModel):
    phone: Annotated[str, Field(pattern=r'^\+?[0-9]{3,20}$')]


class VoiceEntry(StrictModel):
    kind: Literal['utterance', 'question', 'answer', 'no_answer', 'playback_failed', 'thermal_candidate']
    text: Annotated[str, Field(max_length=4000)] = ''
    risk: Literal['thermal_candidate', 'self_reported_fall', 'emergency_help'] | None = None
    follow_up: StrictBool | None = None


class ThermalFrame(StrictModel):
    offset_ms: Annotated[StrictInt, Field(ge=-12000, le=0)]
    pixels_decic: Annotated[list[Annotated[StrictInt, Field(ge=-1000, le=10000)]],
                           Field(min_length=768, max_length=768)]


class DeviceResolution(StrictModel):
    operation_id: Annotated[StrictInt, Field(gt=0)]
    outcome: Literal['resolved', 'stale']


class EventBody(StrictModel):
    protocol_version: Literal['family_event_0.1'] = 'family_event_0.1'
    device_id: ID
    boot_id: ID
    source_event_id: ID
    sequence: Annotated[StrictInt, Field(gt=0,le=2**63-1)]
    state_version: Annotated[StrictInt, Field(gt=0,le=2**63-1)]
    origin: Origin
    kind: Literal['incident', 'status']
    incident_id: ID | None = None
    occurred_at: datetime | None = None
    time_quality: Literal['device_unverified', 'device_synchronized', 'historical', 'synthetic']
    title: Annotated[str, Field(min_length=1, max_length=200)] = '设备消息'
    route: Route = 'uncertain'
    reason: Annotated[str, Field(max_length=2000)] = ''
    transcript: Annotated[str, Field(max_length=4000)] | None = None
    source: Annotated[str, Field(min_length=1, max_length=80)]
    red_latched: StrictBool = False
    archived: StrictBool = False
    interaction_state: Annotated[str, Field(max_length=80)] = 'unknown'
    sensor_status: dict[Literal['microphone', 'speaker', 'thermal', 'radar'],
                        Literal['available', 'unavailable', 'error', 'unknown', 'stale']] = Field(default_factory=dict)
    voice_entry: VoiceEntry | None = None
    thermal_frames: Annotated[list[ThermalFrame], Field(min_length=1, max_length=2)] | None = None
    resolution: DeviceResolution | None = None

    @field_validator('occurred_at')
    @classmethod
    def timezone_required(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError('设备时间必须包含时区；未知时留空')
        return value

    @model_validator(mode='after')
    def consistent(self):
        if self.kind == 'incident' and not self.incident_id:
            raise ValueError('incident事件必须提供稳定incident_id')
        if self.kind == 'status' and self.incident_id is not None:
            raise ValueError('状态快照不创建或修改处置事件')
        if self.red_latched and self.archived:
            raise ValueError('红色锁存事件不能同时标为已归档')
        if self.kind != 'incident' and (self.voice_entry or self.thermal_frames):
            raise ValueError('现场记录须关联事件')
        if self.resolution and (self.kind != 'incident' or self.origin != 'live'
                or self.red_latched != (self.resolution.outcome == 'stale')
                or self.archived != (self.resolution.outcome == 'resolved')):
            raise ValueError('板端解除结果与告警状态不一致')
        if self.thermal_frames:
            offsets = [frame.offset_ms for frame in self.thermal_frames]
            if self.origin != 'live' or self.source != 'thermal' or offsets[-1] != 0 or sorted(set(offsets)) != offsets:
                raise ValueError('热像需来自真实触发帧，按相对时间排序')
        return self


class OperationBody(StrictModel):
    request_id: ID
    expected_version: Annotated[StrictInt, Field(gt=0)]
    note: Annotated[str, Field(max_length=1000)] = ''
    human_checked: StrictBool = False
