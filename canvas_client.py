"""Minimal Canvas discussion client; writes are gated by the agent's safety pipeline."""

from __future__ import annotations

from typing import Any
import time

import requests


class CanvasClientError(RuntimeError):
    """Base error for safe, user-facing Canvas client failures."""


class CanvasAuthenticationError(CanvasClientError):
    """Canvas rejected the supplied credentials."""


class CanvasHTTPError(CanvasClientError):
    """Canvas returned an unexpected HTTP response."""


class CanvasResponseError(CanvasClientError):
    """Canvas returned data in an unexpected format."""


class CanvasClient:
    """Access to one Canvas course discussion topic."""

    def __init__(self, base_url: str, token: str, timeout_seconds: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def get_discussion_topic(self, course_id: int, topic_id: int) -> dict[str, Any]:
        """Retrieve discussion metadata. Performs a GET request only."""
        path = f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}"
        return self._get_object(path, operation="topic metadata")

    def get_discussion_view(self, course_id: int, topic_id: int) -> dict[str, Any]:
        """Retrieve the full discussion tree, including entries and replies."""
        path = f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/view"
        payload = self._get_object(path, operation="full discussion view")
        entries = payload.get("view")
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            raise CanvasResponseError(
                "Full discussion view returned a malformed response (missing valid 'view' entries)."
            )
        return payload

    def post_discussion_reply(
        self, course_id: int, topic_id: int, entry_id: str, message: str
    ) -> dict[str, Any]:
        """Post one reply once; ambiguous POST failures are reconciled from pending state."""
        path = (
            f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}"
            f"/entries/{entry_id}/replies"
        )
        payload = self._request_json(
            "POST", path, "post discussion reply", data={"message": message}, retry_transient=False
        )
        if not isinstance(payload, dict):
            raise CanvasResponseError("Post discussion reply returned a malformed JSON object.")
        return payload

    def _get_object(self, path: str, operation: str) -> dict[str, Any]:
        payload = self._get_json(path, operation)
        if not isinstance(payload, dict):
            raise CanvasResponseError(f"{operation.capitalize()} returned a malformed JSON object.")
        return payload

    def _get_json(self, path: str, operation: str) -> Any:
        return self._request_json("GET", path, operation)

    def _request_json(
        self,
        method: str,
        path: str,
        operation: str,
        data: dict[str, str] | None = None,
        retry_transient: bool = True,
    ) -> Any:
        retryable_statuses = {408, 429, 500, 502, 503, 504}
        response: requests.Response | None = None
        attempts = 3 if retry_transient else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method, f"{self.base_url}{path}", data=data, timeout=self.timeout_seconds
                )
            except requests.RequestException as error:
                if attempt == attempts - 1:
                    raise CanvasHTTPError(
                        f"{operation.capitalize()} failed before Canvas returned an HTTP status. "
                        "Check your network and try again."
                    ) from error
                time.sleep(2**attempt)
                continue

            if response.status_code not in retryable_statuses or attempt == attempts - 1:
                break
            time.sleep(2**attempt)

        if response is None:
            raise CanvasHTTPError(f"{operation.capitalize()} did not receive an HTTP response.")
        if response.status_code in (401, 403):
            raise CanvasAuthenticationError(
                f"{operation.capitalize()} failed with HTTP {response.status_code}: "
                "Canvas rejected the token or this account lacks access."
            )
        if not response.ok:
            raise CanvasHTTPError(
                f"{operation.capitalize()} failed with HTTP {response.status_code}."
            )

        try:
            return response.json()
        except ValueError as error:
            raise CanvasResponseError(
                f"{operation.capitalize()} returned a non-JSON response (HTTP {response.status_code})."
            ) from error
