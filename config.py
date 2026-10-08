"""Static configuration and safe environment loading for the Canvas agent."""

from __future__ import annotations

import os
from dataclasses import dataclass


CANVAS_BASE_URL = "https://canvas.mit.edu"
COURSE_ID = 40577
DISCUSSION_TOPIC_ID = 448963
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


class ConfigurationError(RuntimeError):
    """Raised when required local configuration is unavailable."""


@dataclass(frozen=True)
class Settings:
    canvas_base_url: str
    course_id: int
    discussion_topic_id: int
    canvas_token: str
    canvas_write_enabled: bool
    simulate_lost_ack: bool
    openai_api_key: str | None
    openai_model: str


def load_settings() -> Settings:
    """Load settings without printing or persisting the Canvas API token."""
    token = os.getenv("CANVAS_TOKEN")
    if not token:
        raise ConfigurationError(
            "CANVAS_TOKEN is not set. Set it in your environment before running the agent."
        )

    return Settings(
        canvas_base_url=CANVAS_BASE_URL,
        course_id=COURSE_ID,
        discussion_topic_id=DISCUSSION_TOPIC_ID,
        canvas_token=token,
        canvas_write_enabled=os.getenv("CANVAS_WRITE_ENABLED", "false").lower() == "true",
        simulate_lost_ack=os.getenv("SIMULATE_LOST_ACK", "false").lower() == "true",
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_model=os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
    )
