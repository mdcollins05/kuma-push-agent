from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class NotificationResponse(BaseModel):
    id: int = Field(..., description="Uptime Kuma notification channel ID")
    name: str = Field(..., description="Notification channel name")


class TagResponse(BaseModel):
    id: Optional[int] = Field(None, description="Uptime Kuma tag ID — null when creation is queued and not yet processed")
    name: str = Field(..., description="Tag name")
    color: str = Field(..., description="Tag color as a hex string", examples=["#3396FF"])


class GroupResponse(BaseModel):
    kuma_id: int = Field(..., description="Uptime Kuma group monitor ID")
    name: str = Field(..., description="Group name")


class TagCreate(BaseModel):
    name: str = Field(..., description="Tag name", examples=["production"])
    color: str = Field(..., description="Tag color as a hex string", examples=["#3396FF"])

    model_config = {
        "json_schema_extra": {
            "examples": [{"name": "production", "color": "#3396FF"}]
        }
    }


class HttpConfig(BaseModel):
    """HTTP check configuration."""
    type: Literal["http"] = Field("http", description="Check type discriminator")
    url: str = Field(..., description="URL to health-check", examples=["https://api.example.com/health"])
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = Field(
        "GET", description="HTTP request method"
    )
    headers: Dict[str, str] = Field(
        default_factory=dict, description="Custom request headers", examples=[{"Authorization": "Bearer xxx"}]
    )
    body: Optional[str] = Field(
        None, description="Request body, sent as raw text — typically JSON when method is POST/PUT/PATCH"
    )
    expected_codes: List[int] = Field(
        default_factory=lambda: [200], description="HTTP status codes considered healthy", examples=[[200]]
    )
    keyword: Optional[str] = Field(
        None, description="Optional string that must appear in the response body"
    )
    max_response_ms: Optional[int] = Field(
        None, description="Mark DOWN if response time exceeds this many milliseconds"
    )
    verify_ssl: bool = Field(True, description="Reject invalid or self-signed TLS certificates")

    @field_validator("url")
    @classmethod
    def url_non_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("url must not be blank")
        return v

    @field_validator("expected_codes")
    @classmethod
    def codes_valid(cls, v: List[int]) -> List[int]:
        if not v:
            raise ValueError("expected_codes must contain at least one status code")
        for c in v:
            if not (100 <= c <= 599):
                raise ValueError(f"invalid HTTP status code: {c}")
        return v

    @field_validator("max_response_ms")
    @classmethod
    def max_response_positive(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("max_response_ms must be greater than 0")
        return v


# Discriminated union of check configurations.
# Future: Annotated[Union[HttpConfig, TcpConfig, DnsConfig, PingConfig], Field(discriminator="type")]
CheckConfig = HttpConfig


class MonitorCreate(BaseModel):
    name: str = Field(..., description="Human-readable monitor name, must be unique", examples=["My API"])
    interval: int = Field(60, description="Check interval in seconds (minimum 20)", examples=[60])
    config: CheckConfig = Field(..., description="Check-type-specific configuration")
    tag_ids: List[int] = Field(default_factory=list, description="Uptime Kuma tag IDs to apply to the monitor")
    notification_ids: List[int] = Field(
        default_factory=list, description="Uptime Kuma notification channel IDs to alert on status change"
    )
    kuma_group_id: Optional[int] = Field(
        None, description="Uptime Kuma group monitor ID to nest this monitor under, or null for top level"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "My API",
                    "interval": 60,
                    "config": {
                        "type": "http",
                        "url": "https://api.example.com/health",
                        "method": "GET",
                        "expected_codes": [200],
                    },
                    "tag_ids": [],
                    "notification_ids": [],
                    "kuma_group_id": None,
                },
                {
                    "name": "Auth POST probe",
                    "interval": 60,
                    "config": {
                        "type": "http",
                        "url": "https://api.example.com/auth",
                        "method": "POST",
                        "headers": {"Content-Type": "application/json"},
                        "body": "{\"probe\": true}",
                        "expected_codes": [200, 204],
                    },
                },
            ]
        }
    }

    @field_validator("interval")
    @classmethod
    def interval_min(cls, v: int) -> int:
        if v < 20:
            raise ValueError("interval must be at least 20 seconds")
        return v


class MonitorUpdate(MonitorCreate):
    pass


class SetupRequest(BaseModel):
    username: str = Field(..., description="UI login username", examples=["admin"])
    password: str = Field(..., description="UI login password", examples=["changeme"])
    timezone: str = Field("UTC", description="Display timezone for timestamps (IANA name)", examples=["America/New_York"])

    model_config = {
        "json_schema_extra": {
            "examples": [{"username": "admin", "password": "changeme", "timezone": "UTC"}]
        }
    }


class SetupResponse(BaseModel):
    api_key: str = Field(..., description="Generated API key — store this, it is only returned once")


class KumaSettingsRequest(BaseModel):
    kuma_url: str = Field(..., description="Base URL of the Uptime Kuma instance", examples=["http://kuma:3001"])
    kuma_username: str = Field(..., description="Uptime Kuma login username", examples=["admin"])
    kuma_password: Optional[str] = Field(None, description="Uptime Kuma login password — omit to keep existing value")

    model_config = {
        "json_schema_extra": {
            "examples": [{"kuma_url": "http://kuma:3001", "kuma_username": "admin", "kuma_password": "secret"}]
        }
    }


class KumaSettingsResponse(BaseModel):
    configured: bool = Field(..., description="Whether Kuma credentials are saved and active")
    kuma_url: Optional[str] = Field(None, description="Configured Kuma URL")
    kuma_username: Optional[str] = Field(None, description="Configured Kuma username")


class MonitorStatus(BaseModel):
    id: int = Field(..., description="Unique monitor ID")
    enabled: bool = Field(..., description="Whether the check scheduler is active")
    last_status: Optional[str] = Field(None, description="'up' or 'down', null if never checked")
    last_check_time: Optional[str] = Field(None, description="Timestamp of the last check in the configured timezone")
    last_response_ms: Optional[int] = Field(None, description="Response time in milliseconds")
    last_error: Optional[str] = Field(None, description="Error message from the most recent failed check")
    kuma_synced: bool = Field(..., description="Whether monitor exists in Uptime Kuma")
    kuma_monitor_id: Optional[int] = Field(None, description="Uptime Kuma monitor ID, null if not synced")
    pending_jobs: int = Field(..., description="Number of pending Uptime Kuma sync jobs")
    failed_jobs: int = Field(..., description="Number of failed Uptime Kuma sync jobs")
    pending_create_tags: bool = Field(..., description="Whether a tag-creation job is pending")

    model_config = {"from_attributes": True}


class MonitorResponse(BaseModel):
    id: int = Field(..., description="Unique monitor ID")
    name: str = Field(..., description="Human-readable monitor name")
    interval: int = Field(..., description="Check interval in seconds")
    config: CheckConfig = Field(..., description="Check-type-specific configuration")
    kuma_synced: bool = Field(..., description="Whether monitor has been successfully created in Uptime Kuma")
    last_status: Optional[str] = Field(None, description="'up' or 'down', null if never checked")
    last_check_time: Optional[str] = Field(None, description="ISO 8601 timestamp of the last check")
    last_response_ms: Optional[int] = Field(None, description="Response time in milliseconds")
    last_error: Optional[str] = Field(None, description="Error message from the most recent failed check")
    tag_ids: List[int] = Field(..., description="Uptime Kuma tag IDs applied to this monitor")
    notification_ids: List[int] = Field(..., description="Uptime Kuma notification channel IDs configured for this monitor")
    kuma_group_id: Optional[int] = Field(None, description="Uptime Kuma group monitor ID this monitor is nested under, or null for top level")
    enabled: bool = Field(..., description="Whether the check scheduler is active for this monitor")

    model_config = {"from_attributes": True}
