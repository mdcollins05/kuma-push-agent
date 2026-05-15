from typing import List, Optional

from pydantic import BaseModel, field_validator


class MonitorCreate(BaseModel):
    name: str
    url: str
    interval: int = 60
    expected_codes: List[int] = [200]
    keyword: Optional[str] = None
    verify_ssl: bool = True

    @field_validator("interval")
    @classmethod
    def interval_min(cls, v: int) -> int:
        if v < 20:
            raise ValueError("interval must be at least 20 seconds")
        return v

    @field_validator("expected_codes")
    @classmethod
    def codes_valid(cls, v: List[int]) -> List[int]:
        for c in v:
            if not (100 <= c <= 599):
                raise ValueError(f"invalid HTTP status code: {c}")
        return v


class MonitorUpdate(MonitorCreate):
    pass


class MonitorResponse(BaseModel):
    id: int
    name: str
    url: str
    interval: int
    expected_codes: List[int]
    keyword: Optional[str]
    verify_ssl: bool
    kuma_synced: bool
    last_status: Optional[str]
    last_check_time: Optional[str]
    last_response_ms: Optional[int]
    last_error: Optional[str]
    enabled: bool

    model_config = {"from_attributes": True}
