"""Run one read-only Canvas discussion summary cycle."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import time

from activity_log import log_cycle
from canvas_client import (
    CanvasAuthenticationError,
    CanvasClient,
    CanvasClientError,
    CanvasHTTPError,
    CanvasResponseError,
)
from config import ConfigurationError, load_settings
from decision import (
    Action,
    Decision,
    DecisionEngine,
    DecisionError,
    MockDecisionEngine,
    OpenAIDecisionEngine,
)
from memory import MemoryError, MemoryStore


class _HTMLToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def html_to_text(value: object) -> str:
    """Turn Canvas HTML into a concise, terminal-friendly string."""
    if not isinstance(value, str):
        return ""
    parser = _HTMLToText()
    parser.feed(unescape(value))
    return parser.text()


def reply_count(entries: list[dict]) -> int:
    """Count nested replies returned by the full discussion view."""
    def count_replies(entry: dict) -> int:
        replies = entry.get("replies", [])
        if not isinstance(replies, list):
            return 0
        return len(replies) + sum(count_replies(reply) for reply in replies if isinstance(reply, dict))

    return sum(count_replies(entry) for entry in entries)


@dataclass(frozen=True)
class DiscussionItem:
    item_id: str
    item_type: str  # "entry" or "reply"
    parent_entry_id: str
    text: str
    created_at: str


def discussion_items(entries: list[dict]) -> list[DiscussionItem]:
    """Flatten the threaded Canvas view while preserving entry/reply identity."""
    items: list[DiscussionItem] = []

    def visit(node: dict, item_type: str, parent_entry_id: str) -> None:
        node_id = node.get("id")
        if not isinstance(node_id, (int, str)):
            return
        item_id = str(node_id)
        root_entry_id = item_id if item_type == "entry" else parent_entry_id
        items.append(
            DiscussionItem(
                item_id,
                item_type,
                root_entry_id,
                html_to_text(node.get("message")),
                node.get("created_at") if isinstance(node.get("created_at"), str) else "",
            )
        )
        replies = node.get("replies", [])
        if isinstance(replies, list):
            for reply in replies:
                if isinstance(reply, dict):
                    visit(reply, "reply", root_entry_id)

    for entry in entries:
        if isinstance(entry, dict):
            visit(entry, "entry", "")
    return items


def candidate_thread_contexts(
    all_items: list[DiscussionItem], new_items: list[DiscussionItem], state: object
) -> dict[str, list[str]]:
    """Select at most five newest changed threads for one bounded LLM call."""
    newest_items = sorted(
        (item for item in new_items if not state.is_own(item.item_id, item.item_type)),
        key=lambda item: item.created_at,
        reverse=True,
    )
    thread_ids: list[str] = []
    for item in newest_items:
        if item.parent_entry_id not in thread_ids:
            thread_ids.append(item.parent_entry_id)
        if len(thread_ids) == 5:
            break

    candidates: dict[str, list[str]] = {}
    for target_entry_id in thread_ids:
        thread = [item for item in all_items if item.parent_entry_id == target_entry_id and item.text]
        root = next((item for item in thread if item.item_type == "entry"), None)
        recent = sorted(
            (item for item in thread if item.item_type != "entry"),
            key=lambda item: item.created_at,
            reverse=True,
        )[:5]
        context_items = ([root] if root else []) + recent
        candidates[target_entry_id] = [
            f"{'Root post' if item.item_type == 'entry' else 'Reply'}: {item.text[:800]}"
            for item in context_items
        ]
    return candidates


def parse_course_team_control_state(description: object) -> str:
    """Parse only the leading COURSE-TEAM CONTROL line; unknown input fails closed."""
    if not isinstance(description, str):
        return "MISSING"
    text = unescape(description).lstrip()
    match = re.match(r"(?:<[^>]*>\s*)*COURSE-TEAM CONTROL:\s*(RUNNING|PAUSED)\b", text, re.I)
    return match.group(1).upper() if match else "NOT_RUNNING"


def find_reply(entries: list[dict], target_entry_id: str, pending: dict[str, object]) -> str | None:
    """Find a pending reply anywhere under its intended root thread using GET data only."""
    expected_id = pending.get("submitted_reply_id")
    expected_text = pending.get("reply_text")
    expected_author = pending.get("author_id")
    if not isinstance(expected_text, str):
        return None
    expected_text = normalize_text(expected_text)

    def visit(node: dict, root_id: str) -> str | None:
        node_id = node.get("id")
        item_id = str(node_id) if isinstance(node_id, (int, str)) else None
        id_matches = isinstance(expected_id, str) and item_id == expected_id
        author = node.get("user_id", node.get("author_id"))
        author_matches = expected_author is None or str(author) == str(expected_author)
        text_matches = normalize_text(node.get("message")) == expected_text and author_matches
        if root_id == target_entry_id and item_id and (id_matches or text_matches):
            return item_id
        for key in ("replies", "recent_replies", "entries"):
            children = node.get(key, [])
            if isinstance(children, list):
                for reply in children:
                    if isinstance(reply, dict):
                        found = visit(reply, root_id)
                        if found:
                            return found
        return None

    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id")) == target_entry_id:
            for key in ("replies", "recent_replies", "entries"):
                children = entry.get(key, [])
                if isinstance(children, list):
                    for reply in children:
                        if isinstance(reply, dict):
                            found = visit(reply, target_entry_id)
                            if found:
                                return found
    return None


def normalize_text(value: object) -> str:
    """Normalize Canvas HTML/text and harmless whitespace for idempotency matching."""
    return " ".join(html_to_text(value).split())


def make_decision_engine(settings: object) -> DecisionEngine:
    if settings.openai_api_key:
        return OpenAIDecisionEngine(settings.openai_api_key, settings.openai_model)
    return MockDecisionEngine()


def main() -> int:
    try:
        settings = load_settings()
        client = CanvasClient(settings.canvas_base_url, settings.canvas_token)
        topic = client.get_discussion_topic(
            settings.course_id, settings.discussion_topic_id
        )
        discussion_view = client.get_discussion_view(
            settings.course_id, settings.discussion_topic_id
        )
    except ConfigurationError as error:
        print(f"Configuration error: {error}")
        return 2
    except CanvasAuthenticationError as error:
        print(f"Authentication error: {error}")
        return 3
    except CanvasHTTPError as error:
        print(f"HTTP error: {error}")
        return 4
    except CanvasResponseError as error:
        print(f"Response error: {error}")
        return 5
    except CanvasClientError as error:
        print(f"Canvas error: {error}")
        return 1

    title = topic.get("title")
    if not isinstance(title, str) or not title.strip():
        print("Response error: Canvas topic is missing a valid title.")
        return 5

    entries = discussion_view["view"]
    item_list = discussion_items(entries)
    entry_total, reply_total = len(entries), reply_count(entries)
    memory_store = MemoryStore(Path(__file__).parent / "data" / "memory.json")
    log_path = Path(__file__).parent / "logs" / "activity.log"
    control, write_result, verification = "NOT_CHECKED", "NOT_ATTEMPTED", "NOT_ATTEMPTED"
    rate_limit = "NOT_CHECKED"
    recovery = "NOT_APPLICABLE"
    log_message = ""
    candidate_count = 0
    try:
        state = memory_store.load()
        pending_unresolved = False
        decision_evaluated = False
        processed_thread_ids: set[str] = set()
        # Reconcile a POST that may have completed before a crash or lost acknowledgement.
        if state.pending_action:
            # Migrate older pending state: a returned Canvas reply ID proves a POST was accepted.
            if "post_attempted" not in state.pending_action:
                state.pending_action["post_attempted"] = bool(
                    state.pending_action.get("submitted_reply_id")
                )
            if state.pending_action.get("post_attempted") and not state.pending_action.get("post_counted"):
                try:
                    state.record_successful_write(float(state.pending_action["created_at"]))
                except (KeyError, TypeError, ValueError):
                    state.record_successful_write()
                state.pending_action["post_counted"] = True
                memory_store.save(state)
            pending_target = state.pending_action.get("target_entry_id")
            # Older accepted pending actions predate this state field. Preserve
            # their target when they are recovered, without treating an
            # unacknowledged pending action as a completed contribution.
            if (
                state.pending_action.get("post_counted")
                and isinstance(pending_target, str)
                and not state.most_recent_contribution_target
            ):
                state.most_recent_contribution_target = pending_target
            if isinstance(pending_target, str):
                recovered_id = find_reply(entries, pending_target, state.pending_action)
                if recovered_id:
                    state.record_own(recovered_id, "reply")
                    state.pending_action = None
                    memory_store.save(state)
                    recovery = "RECOVERED_WITHOUT_DUPLICATE"
                    processed_thread_ids.add(pending_target)
                else:
                    pending_unresolved = True
        new_items = [item for item in item_list if not state.has_seen(item.item_id, item.item_type)]
        if pending_unresolved:
            recovery = "PENDING_UNRESOLVED"
            decision = Decision(Action.SKIP, "Pending action is unresolved; no new action will be created.")
        else:
            try:
                candidates = candidate_thread_contexts(item_list, new_items, state)
                candidate_count = len(candidates)
                decision = make_decision_engine(settings).decide(candidates)
                decision_evaluated = True
                if (
                    decision.action is Action.REPLY
                    and decision.incremental_value_score >= 4
                    and decision.target_entry_id == state.most_recent_contribution_target
                ):
                    # Do not fall through to another candidate: immediate
                    # thread repetition is an intentional skip for this cycle.
                    decision = Decision(
                        Action.SKIP,
                        "Thread diversity guard: the selected thread was also the agent's most recent contribution target.",
                        target_entry_id=decision.target_entry_id,
                        incremental_value_score=decision.incremental_value_score,
                        model_rationale=decision.model_rationale,
                    )
                    decision_evaluated = False
                elif (
                    decision.action is Action.REPLY
                    and state.writes_in_last_eight_hours() >= 3
                ):
                    # Evaluated after the score and diversity gates, but before
                    # any control check or possible Canvas write.
                    decision = Decision(
                        Action.SKIP,
                        "Participation budget reached: 3 contributions in the last 8 hours.",
                        target_entry_id=decision.target_entry_id,
                        incremental_value_score=decision.incremental_value_score,
                        model_rationale=decision.model_rationale,
                    )
                    decision_evaluated = False
            except DecisionError as error:
                decision = Decision(Action.ERROR, f"LLM error: {error}")
                log_message = decision.reason
        # Only completed/skipped threads are consumed; dry-run and blocked replies stay eligible.
        if decision.action is Action.SKIP and decision_evaluated:
            processed_thread_ids.update(candidates)
        state.mark_seen(item for item in new_items if item.parent_entry_id in processed_thread_ids)
        memory_store.save(state)
    except MemoryError as error:
        print(f"Memory error: {error}")
        return 6

    print(f"Discussion: {title.strip()}")
    print(f"Description: {html_to_text(topic.get('message')) or '(no description)'}")
    print(f"Entries retrieved: {entry_total}")
    print(f"Replies retrieved: {reply_total}")
    print(f"New items: {len(new_items)}")
    if pending_unresolved:
        pending = state.pending_action
        assert pending is not None
        print("Pending action is unresolved; new LLM actions are blocked.")
        recovery = "RECOVERY_UNRESOLVED"
        # No write was attempted during GET-only reconciliation; retain the
        # distinct recovery outcome in its dedicated log field.
        write_result = "NOT_ATTEMPTED"
        log_message = "Pending action could not be reconciled with Canvas; no POST was made."
        print("Recovery unresolved: GET-only reconciliation found no matching reply; no POST was made.")
    if not settings.canvas_write_enabled and decision.action is not Action.REPLY and not pending_unresolved:
        try:
            latest_topic = client.get_discussion_topic(settings.course_id, settings.discussion_topic_id)
            control = parse_course_team_control_state(latest_topic.get("message"))
            print(f"Control state: {control}")
        except CanvasClientError as error:
            control = "UNAVAILABLE"
            print(f"Control state: UNAVAILABLE ({error})")
    if decision.action is Action.REPLY:
        try:
            # Always re-fetch immediately before a potential write; never trust cycle-start metadata.
            latest_topic = client.get_discussion_topic(settings.course_id, settings.discussion_topic_id)
            control = parse_course_team_control_state(latest_topic.get("message"))
            print(f"Control state: {control}")
        except CanvasClientError as error:
            control, write_result = "UNAVAILABLE", "BLOCKED_CONTROL_READ_FAILURE"
            print(f"Write blocked: unable to re-fetch COURSE-TEAM CONTROL ({error})")

        if control == "PAUSED":
            log_message = "Write blocked: COURSE-TEAM CONTROL is PAUSED."
            print(log_message)
            write_result = "BLOCKED_CONTROL_PAUSED"
        elif control != "RUNNING":
            print("Write blocked: COURSE-TEAM CONTROL is missing or malformed.")
            write_result = "BLOCKED_CONTROL_INVALID"
        elif not settings.canvas_write_enabled:
            print("DRY RUN")
            print("Decision: REPLY")
            print(f"Target entry: {decision.target_entry_id}")
            print("Proposed reply:")
            print(decision.reply_text)
            write_result = "DRY_RUN"
        elif state.writes_in_last_hour() >= 3:
            print("Write blocked: WRITE_BLOCKED_RATE_LIMIT.")
            rate_limit, write_result = "WRITE_BLOCKED_RATE_LIMIT", "WRITE_BLOCKED_RATE_LIMIT"
            memory_store.save(state)
        else:
            try:
                rate_limit = "ALLOWED"
                # Persist exact intent before sending the only allowed contribution this cycle.
                state.pending_action = {
                    "target_entry_id": decision.target_entry_id or "",
                    "reply_text": decision.reply_text,
                    "created_at": str(time.time()),
                    "recovery_attempts": 0,
                    # Set before the request: an ambiguous network failure must never be re-posted.
                    "post_attempted": True,
                    "post_counted": False,
                }
                memory_store.save(state)
                posted = client.post_discussion_reply(
                    settings.course_id,
                    settings.discussion_topic_id,
                    decision.target_entry_id or "",
                    decision.reply_text,
                )
                write_result = "POST_ACCEPTED"
                posted_id = posted.get("id")
                if isinstance(posted_id, (int, str)):
                    state.pending_action["submitted_reply_id"] = str(posted_id)
                posted_author = posted.get("user_id", posted.get("author_id"))
                if isinstance(posted_author, (int, str)):
                    state.pending_action["author_id"] = str(posted_author)
                # Rate limiting counts accepted POSTs immediately, independent of verification.
                state.record_successful_write()
                state.most_recent_contribution_target = decision.target_entry_id
                state.pending_action["post_counted"] = True
                memory_store.save(state)
                if settings.simulate_lost_ack:
                    verification = "SIMULATED_LOST_ACK"
                    log_message = "Simulated lost acknowledgement after Canvas accepted the reply."
                    print("Write result: POST_ACCEPTED")
                    print("Verification: SIMULATED_LOST_ACK")
                    print(log_message)
                else:
                    verified_view = client.get_discussion_view(
                        settings.course_id, settings.discussion_topic_id
                    )
                    verified_id = find_reply(
                        verified_view["view"], decision.target_entry_id or "", state.pending_action
                    )
                    if verified_id:
                        state.record_own(verified_id, "reply")
                        state.pending_action = None
                        state.mark_seen(
                            item
                            for item in new_items
                            if item.parent_entry_id == decision.target_entry_id
                        )
                        state.mark_seen(
                            [DiscussionItem(verified_id, "reply", decision.target_entry_id or "", "", "")]
                        )
                        memory_store.save(state)
                        verification = "VERIFIED"
                        print("Write result: POST_ACCEPTED")
                        print("Verification: VERIFIED")
                    else:
                        verification = "FAILED_PENDING_RECONCILIATION"
                        print("Write result: POST_ACCEPTED")
                        print("Verification: FAILED_PENDING_RECONCILIATION")
            except (CanvasClientError, MemoryError) as error:
                write_result = "FAILED"
                log_message = "Canvas reply failed; pending action retained for recovery."
                print(f"Write result: FAILED ({error})")
    elif decision.action is Action.SKIP:
        print("Decision: SKIP")
        print(f"Reason: {decision.reason}")
    else:
        print("Decision: ERROR")
        print(f"Reason: {decision.reason}")

    try:
        log_cycle(
            log_path, entries=entry_total, replies=reply_total, new_items=len(new_items),
            decision=decision.action.value, target_id=decision.target_entry_id,
            dry_run=not settings.canvas_write_enabled, control_state=control,
            write_result=write_result, verification=verification, recovery=recovery,
            rate_limit=rate_limit, message=log_message,
            total_unseen=len(new_items), candidate_threads=candidate_count,
            model_action=decision.action.value, reply_word_count=len(decision.reply_text.split()),
            incremental_value_score=decision.incremental_value_score,
            decision_reason=decision.reason,
            model_rationale=decision.model_rationale,
        )
    except OSError as error:
        print(f"Log error: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
