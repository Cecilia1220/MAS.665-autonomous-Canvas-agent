"""Persistent, local-only state for observed Canvas discussion items."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


class MemoryError(RuntimeError):
    """The local memory file could not be safely read or written."""


ONE_HOUR_SECONDS = 60 * 60
PARTICIPATION_WINDOW_SECONDS = 8 * ONE_HOUR_SECONDS


@dataclass
class MemoryState:
    seen_entry_ids: set[str] = field(default_factory=set)
    seen_reply_ids: set[str] = field(default_factory=set)
    own_entry_ids: set[str] = field(default_factory=set)
    own_reply_ids: set[str] = field(default_factory=set)
    successful_write_timestamps: list[float] = field(default_factory=list)
    # Updated only after Canvas accepts a reply, so a new decision can avoid
    # immediately contributing to the same root thread again.
    most_recent_contribution_target: str | None = None
    pending_action: dict[str, object] | None = None

    def has_seen(self, item_id: str, item_type: str) -> bool:
        return item_id in self._seen_ids(item_type)

    def is_own(self, item_id: str, item_type: str) -> bool:
        return item_id in self._own_ids(item_type)

    def mark_seen(self, items: Iterable[object]) -> None:
        """Record every retrieved item after a successful decision cycle."""
        for item in items:
            self._seen_ids(item.item_type).add(item.item_id)

    def record_own(self, item_id: str, item_type: str) -> None:
        """Reserve an ID as agent-authored when a future write is verified."""
        self._own_ids(item_type).add(item_id)

    def writes_in_last_hour(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return sum(timestamp > now - ONE_HOUR_SECONDS for timestamp in self.successful_write_timestamps)

    def writes_in_last_eight_hours(self, now: float | None = None) -> int:
        """Count accepted contributions for the participation budget."""
        now = time.time() if now is None else now
        # The budget is inclusive at the boundary: now - timestamp <= 8 hours.
        return sum(
            timestamp >= now - PARTICIPATION_WINDOW_SECONDS
            for timestamp in self.successful_write_timestamps
        )

    def record_successful_write(self, timestamp: float | None = None) -> None:
        timestamp = time.time() if timestamp is None else timestamp
        # Retain enough history for the eight-hour participation guard while
        # keeping the local state bounded. The hourly hard limit is counted
        # separately and must not discard still-relevant eight-hour history.
        self.successful_write_timestamps = [
            value
            for value in self.successful_write_timestamps
            if value >= timestamp - PARTICIPATION_WINDOW_SECONDS
        ]
        self.successful_write_timestamps.append(timestamp)

    def _seen_ids(self, item_type: str) -> set[str]:
        return self.seen_entry_ids if item_type == "entry" else self.seen_reply_ids

    def _own_ids(self, item_type: str) -> set[str]:
        return self.own_entry_ids if item_type == "entry" else self.own_reply_ids


class MemoryStore:
    """Read and atomically replace the JSON state file to reduce corruption risk."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> MemoryState:
        if not self.path.exists():
            return MemoryState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MemoryError(f"Could not read local memory file: {self.path}") from error
        if not isinstance(payload, dict):
            raise MemoryError("Local memory file has an invalid format.")
        try:
            return MemoryState(
                seen_entry_ids=_id_set(payload.get("seen_entry_ids", [])),
                seen_reply_ids=_id_set(payload.get("seen_reply_ids", [])),
                own_entry_ids=_id_set(payload.get("own_entry_ids", [])),
                own_reply_ids=_id_set(payload.get("own_reply_ids", [])),
                successful_write_timestamps=_timestamp_list(
                    payload.get("successful_write_timestamps", [])
                ),
                most_recent_contribution_target=_optional_id(
                    payload.get("most_recent_contribution_target")
                ),
                pending_action=_pending_action(payload.get("pending_action")),
            )
        except ValueError as error:
            raise MemoryError("Local memory file has invalid ID data.") from error

    def save(self, state: MemoryState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "seen_entry_ids": sorted(state.seen_entry_ids),
            "seen_reply_ids": sorted(state.seen_reply_ids),
            "own_entry_ids": sorted(state.own_entry_ids),
            "own_reply_ids": sorted(state.own_reply_ids),
            "successful_write_timestamps": state.successful_write_timestamps,
            "most_recent_contribution_target": state.most_recent_contribution_target,
            "pending_action": state.pending_action,
        }
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as temporary_file:
                temporary_path = temporary_file.name
                json.dump(payload, temporary_file, indent=2, sort_keys=True)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)
            raise MemoryError(f"Could not safely write local memory file: {self.path}") from error


def _id_set(value: object) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("IDs must be a list of strings")
    return set(value)


def _timestamp_list(value: object) -> list[float]:
    if not isinstance(value, list) or not all(isinstance(item, (int, float)) for item in value):
        raise ValueError("timestamps must be a list of numbers")
    return [float(item) for item in value]


def _optional_id(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("most recent contribution target must be a string or null")


def _pending_action(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("pending action must be an object or null")
    required = {"target_entry_id", "reply_text", "created_at"}
    if not required.issubset(value) or not all(isinstance(value[key], str) for key in required):
        raise ValueError("pending action is missing required fields")
    attempts = value.get("recovery_attempts", 0)
    if not isinstance(attempts, int) or attempts < 0:
        raise ValueError("pending action recovery attempts must be a non-negative integer")
    for key in ("post_attempted", "post_counted"):
        if key in value and not isinstance(value[key], bool):
            raise ValueError(f"pending action {key} must be a boolean")
    return value
