"""模块事件结构。语音、传感器和状态机事件共用这一数据格式。

修改规则：一端改，两端同步。字段漂移会让接口统一形同虚设。
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import json


@dataclass
class SpeechEvent:
    type: str = "speech_keyword"
    keyword: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    source: str = ""
    module_ok: bool = True
    input_mode: str = ""            # "wav_replay" / "mic_live" / "sensor" / "system"
    audio_offset_ms: int = -1       # 关键词起点，毫秒；-1 = 不可用
    audio_device_ok: Optional[bool] = None   # None = 不适用（WAV 回放）；True/False = 麦克风设备状态

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
