"""Small local activity log with no credential-bearing fields."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def log_cycle(
    path: Path, *, entries: int, replies: int, new_items: int, decision: str,
    target_id: str | None = None, dry_run: bool = True, control_state: str = "NOT_CHECKED",
    write_result: str = "NOT_ATTEMPTED", verification: str = "NOT_ATTEMPTED",
    rate_limit: str = "NOT_CHECKED", recovery: str = "NOT_APPLICABLE", message: str = "",
    total_unseen: int = 0, candidate_threads: int = 0, model_action: str = "SKIP",
    reply_word_count: int = 0, incremental_value_score: int = 0, decision_reason: str = "",
    model_rationale: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    line = (
        f"{timestamp} entries={entries} replies={replies} new_items={new_items} "
        f"decision={decision} target_id={target_id or '-'} dry_run={str(dry_run).lower()} "
        f"control_state={control_state} write_result={write_result} verification={verification} "
        f"rate_limit={rate_limit} recovery={recovery} total_unseen={total_unseen} "
        f"candidate_threads={candidate_threads} model_action={model_action} "
        f"reply_words={reply_word_count} incremental_value_score={incremental_value_score} "
        f"reason={decision_reason!r} model_rationale={model_rationale!r} message={message!r}\n"
    )
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(line)
