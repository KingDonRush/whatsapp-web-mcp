from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from .browser_policy import resolve_browser_policy


@dataclasses.dataclass(frozen=True)
class SourceProfile:
    id: str
    label: str
    kind: str
    priority: int
    capabilities: tuple[str, ...]
    paths: tuple[Path, ...]
    local_data_role: str
    automation_role: str
    notes: tuple[str, ...] = ()

    def detected_paths(self) -> list[str]:
        out: list[str] = []
        for path in self.paths:
            if path.exists():
                out.append(str(path))
        return out

    def to_json(self) -> dict[str, Any]:
        detected = self.detected_paths()
        status = "detected" if detected else "not_detected"
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "priority": self.priority,
            "status": status,
            "capabilities": list(self.capabilities),
            "paths": [str(path) for path in self.paths],
            "detected_paths": detected,
            "local_data_role": self.local_data_role,
            "automation_role": self.automation_role,
            "notes": list(self.notes),
        }


def _home(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def registry() -> list[SourceProfile]:
    return [
        SourceProfile(
            id="whatsapp_web",
            label="WhatsApp Web",
            kind="browser",
            priority=10,
            capabilities=(
                "dom_search",
                "visible_text",
                "reply_context",
                "historical_scroll",
                "group_sender_labels",
                "media_metadata",
                "message_scoped_media_capture",
                "send_draft",
                "send_confirmation_gate",
            ),
            paths=(),
            local_data_role="disabled; local SQLite/IndexedDB snapshots are not part of the public MCP contract",
            automation_role="attach to browser/DOM and use WhatsApp Web search plus message nodes",
            notes=(
                "Only active automation target. Other wrappers, local SQLite snapshots and API-token backends are intentionally not advertised.",
            ),
        ),
    ]


def source_profiles() -> list[dict[str, Any]]:
    return [profile.to_json() for profile in registry()]


def source_profile_by_id(source_id: str) -> SourceProfile | None:
    normalized = source_id.strip().casefold()
    for profile in registry():
        if profile.id == normalized:
            return profile
    return None


def select_profiles(source_ids: list[str] | None = None) -> list[SourceProfile]:
    if not source_ids:
        return sorted(registry(), key=lambda item: item.priority)
    selected: list[SourceProfile] = []
    seen: set[str] = set()
    for source_id in source_ids:
        profile = source_profile_by_id(source_id)
        if profile and profile.id not in seen:
            selected.append(profile)
            seen.add(profile.id)
    return selected


def search_steps_for_source(
    profile: SourceProfile,
    contact_query: str | None,
    message_query: str | None,
    date_from: str | None,
    date_to: str | None,
) -> list[dict[str, Any]]:
    return [
        {
            "action": "attach_dom_or_accessibility_bridge",
            "detail": (
                "Attach to the browser/webview DOM when possible; use accessibility tree only "
                "when direct DOM attachment is not available."
            ),
        },
        {
            "action": "dom_contact_search",
            "detail": "Use WhatsApp's contact/search UI with structured selectors, not OCR.",
            "contact_query": contact_query,
        },
        {
            "action": "dom_message_search_and_scroll",
            "detail": (
                "Use in-chat search for message_query when present, then run historical scroll "
                "until the requested date range is covered, no new messages appear, or max_scroll_pages is reached."
            ),
            "message_query": message_query,
            "date_from": date_from,
            "date_to": date_to,
            "max_scroll_pages_default": 80,
        },
        {
            "action": "dom_extract_message_nodes",
            "detail": (
                "Extract direction, visible text, timestamp, quoted/reply preview, attachment "
                "labels and stable DOM/data ids into the common JSON schema."
            ),
        },
        {
            "action": "web_media_capture",
            "detail": (
                "Capture media from inside each rendered message node, then keep any unmatched "
                "capture in unassigned_media_captures instead of attaching by guess."
            ),
        },
    ]


def automated_search_plan(
    contact_query: str | None = None,
    message_query: str | None = None,
    source_ids: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    message_types: list[str] | None = None,
    browser_mode: str | None = None,
    login_mode: str | None = None,
) -> dict[str, Any]:
    profiles = select_profiles(source_ids)
    return {
        "schema": "whatsapp.automation.search_plan.v1",
        "preferred_read_path": "dom_or_accessibility_tree",
        "last_resort": "screenshot_ocr_only_if_dom_and_accessibility_fail",
        "query": {
            "contact_query": contact_query,
            "message_query": message_query,
            "source_ids": source_ids,
            "date_from": date_from,
            "date_to": date_to,
            "message_types": message_types,
            "browser_mode": browser_mode,
            "login_mode": login_mode,
        },
        "browser_policy": resolve_browser_policy(
            category="read",
            process="automated_search",
            browser_mode=browser_mode,
            login_mode=login_mode,
        ),
        "sources": [
            {
                "source_id": profile.id,
                "label": profile.label,
                "status": "web_session_required",
                "capabilities": list(profile.capabilities),
                "detected_paths": [],
                "steps": search_steps_for_source(profile, contact_query, message_query, date_from, date_to),
            }
            for profile in profiles
        ],
        "common_output_contract": {
            "schema": "whatsapp.conversation.common.v1",
            "message_fields": [
                "source_id",
                "chat_jid",
                "message_id",
                "record_id",
                "direction",
                "timestamp_iso",
                "type",
                "text",
                "text_status",
                "reply_to",
                "forwarded",
                "media",
                "sender_name",
                "sender_status",
            ],
        },
        "agent_rule": [
            "Do not use screenshots as the normal search/export path.",
            "Use rendered DOM/accessibility nodes for text and reply structure.",
            "Do not use local SQLite/IndexedDB snapshots for media metadata or search.",
            "If WhatsApp Web is not detected, report that the user must open/login WhatsApp Web or provide an attachable browser session.",
        ],
    }
