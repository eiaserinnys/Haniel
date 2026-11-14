"""와이어 글 모델의 바탕. `protocol`과 `protocol_server`가 함께 쓴다(2b-2에서 protocol 모듈을 나누며 옮겼다)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictMessage(BaseModel):
    """Wire message that rejects fields belonging to another protocol variant."""

    model_config = ConfigDict(extra="forbid")
