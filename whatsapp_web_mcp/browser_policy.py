from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .constants import STATE_ROOT

STATE_DIR = STATE_ROOT
POLICY_PATH = STATE_DIR / "browser-policy.json"
PROFILE_ROOT = STATE_DIR / "browser-profiles"
ARTIFACT_ROOT = STATE_DIR / "browser-artifacts"
WHATSAPP_WEB_URL = "https://web.whatsapp.com/"

BROWSER_MODES = {"headless", "headed"}
BROWSER_MODE_ALIASES = {
    "headless": "headless",
    "hidden": "headless",
    "headed": "headed",
    "headful": "headed",
    "visible": "headed",
    "show": "headed",
    "shown": "headed",
    "non_headless": "headed",
    "non-headless": "headed",
    "nao_headless": "headed",
    "não_headless": "headed",
}

LOGIN_MODES = {"qr_artifact", "headed_then_headless", "reuse_session"}
LOGIN_MODE_ALIASES = {
    "qr": "qr_artifact",
    "qr_artifact": "qr_artifact",
    "qr_in_chat": "qr_artifact",
    "chat_qr": "qr_artifact",
    "send_qr": "qr_artifact",
    "headed_then_headless": "headed_then_headless",
    "visible_then_headless": "headed_then_headless",
    "show_login_then_headless": "headed_then_headless",
    "headed_login": "headed_then_headless",
    "reuse": "reuse_session",
    "reuse_session": "reuse_session",
    "existing_session": "reuse_session",
}

DEFAULT_POLICY: dict[str, Any] = {
    "schema": "whatsapp.browser.policy.v1",
    "global": {
        "browser_mode": "headless",
        "login_mode": "qr_artifact",
    },
    "categories": {},
    "processes": {},
}

_PLAYWRIGHT: Any | None = None
_SESSIONS: dict[str, dict[str, Any]] = {}

LOGGED_IN_SELECTORS: dict[str, str] = {
    "logged_chat_list_pt": '[aria-label="Lista de conversas"]',
    "logged_chat_list_en": '[aria-label="Chat list"]',
    "logged_pane_side": "#pane-side",
}
LOGIN_REQUIRED_SELECTORS: dict[str, str] = {
    "login_text_pt": "text=Use o WhatsApp no seu computador",
    "login_text_en": "text=Use WhatsApp on your computer",
    "qr_canvas": "canvas",
}


def _normalized_key(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^a-z0-9_.-]+", "_", value.strip().casefold()).strip("_.-")
    if not normalized:
        raise ValueError(f"{label} cannot be empty")
    return normalized


def _safe_session_id(value: str | None) -> str:
    return _normalized_key(value or "default", "session_id") or "default"


def normalize_browser_mode(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = BROWSER_MODE_ALIASES.get(value.strip().casefold().replace(" ", "_"))
    if normalized not in BROWSER_MODES:
        raise ValueError(f"browser_mode must be one of {', '.join(sorted(BROWSER_MODES))}")
    return normalized


def normalize_login_mode(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = LOGIN_MODE_ALIASES.get(value.strip().casefold().replace(" ", "_"))
    if normalized not in LOGIN_MODES:
        raise ValueError(f"login_mode must be one of {', '.join(sorted(LOGIN_MODES))}")
    return normalized


def browser_policy_file() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return POLICY_PATH


def _merged_policy(payload: dict[str, Any] | None) -> dict[str, Any]:
    policy = copy.deepcopy(DEFAULT_POLICY)
    if not isinstance(payload, dict):
        return policy
    for key in ("global", "categories", "processes"):
        if isinstance(payload.get(key), dict):
            policy[key].update(payload[key])
    policy["schema"] = DEFAULT_POLICY["schema"]
    policy["global"]["browser_mode"] = normalize_browser_mode(
        policy["global"].get("browser_mode")
    ) or DEFAULT_POLICY["global"]["browser_mode"]
    policy["global"]["login_mode"] = normalize_login_mode(
        policy["global"].get("login_mode")
    ) or DEFAULT_POLICY["global"]["login_mode"]
    return policy


def load_browser_policy(policy_path: Path | None = None) -> dict[str, Any]:
    path = policy_path or browser_policy_file()
    if not path.exists():
        return copy.deepcopy(DEFAULT_POLICY)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid browser policy JSON: {path}") from exc
    return _merged_policy(payload)


def save_browser_policy(policy: dict[str, Any], policy_path: Path | None = None) -> dict[str, Any]:
    merged = _merged_policy(policy)
    path = policy_path or browser_policy_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def _apply_scope_value(
    values: dict[str, str],
    sources: dict[str, str],
    scope: dict[str, Any],
    source: str,
) -> None:
    browser_mode = normalize_browser_mode(scope.get("browser_mode"))
    login_mode = normalize_login_mode(scope.get("login_mode"))
    if browser_mode:
        values["browser_mode"] = browser_mode
        sources["browser_mode"] = source
    if login_mode:
        values["login_mode"] = login_mode
        sources["login_mode"] = source


def resolve_browser_policy(
    category: str | None = None,
    process: str | None = None,
    browser_mode: str | None = None,
    login_mode: str | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    category_key = _normalized_key(category or "general", "category")
    process_key = _normalized_key(process, "process") if process else None
    loaded = _merged_policy(policy or load_browser_policy())

    values = {
        "browser_mode": loaded["global"]["browser_mode"],
        "login_mode": loaded["global"]["login_mode"],
    }
    sources = {"browser_mode": "global", "login_mode": "global"}

    category_scope = loaded["categories"].get(category_key)
    if isinstance(category_scope, dict):
        _apply_scope_value(values, sources, category_scope, f"category:{category_key}")

    process_scope = (
        loaded["processes"].get(category_key, {}).get(process_key)
        if process_key
        else None
    )
    if isinstance(process_scope, dict):
        _apply_scope_value(values, sources, process_scope, f"process:{category_key}.{process_key}")

    override_browser_mode = normalize_browser_mode(browser_mode)
    override_login_mode = normalize_login_mode(login_mode)
    if override_browser_mode:
        values["browser_mode"] = override_browser_mode
        sources["browser_mode"] = "operation_override"
    if override_login_mode:
        values["login_mode"] = override_login_mode
        sources["login_mode"] = "operation_override"

    login_browser_mode = (
        "headed"
        if values["login_mode"] == "headed_then_headless"
        else values["browser_mode"]
    )
    qr_delivery = {
        "qr_artifact": {
            "method": "return_artifact_to_chat",
            "description": "When login is required in headless mode, capture the WhatsApp Web QR as an artifact path/base64 for the agent to show in chat.",
        },
        "headed_then_headless": {
            "method": "temporary_headed_browser",
            "description": "Open a visible browser for login/QR scan, then restart later operations with the resolved operation browser_mode.",
        },
        "reuse_session": {
            "method": "reuse_existing_session",
            "description": "Assume the persistent browser profile is already authenticated.",
        },
    }[values["login_mode"]]

    return {
        "schema": "whatsapp.browser.resolved_policy.v1",
        "category": category_key,
        "process": process_key,
        "browser_mode": values["browser_mode"],
        "login_mode": values["login_mode"],
        "preference_sources": sources,
        "one_shot_override": bool(override_browser_mode or override_login_mode),
        "login_phase": {
            "browser_mode": login_browser_mode,
            "qr_delivery": qr_delivery,
        },
        "operation_phase": {
            "browser_mode": values["browser_mode"],
        },
        "agent_rule": [
            "Operation overrides apply only to the current tool call.",
            "Persisted defaults are global, category-level, or process-level inside a category.",
            "Default browser mode is headless unless policy says otherwise.",
        ],
    }


def set_browser_preference(
    category: str | None = None,
    process: str | None = None,
    browser_mode: str | None = None,
    login_mode: str | None = None,
    reset: bool = False,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    policy = load_browser_policy(policy_path=policy_path)
    category_key = _normalized_key(category, "category") if category else None
    process_key = _normalized_key(process, "process") if process else None
    if process_key and not category_key:
        raise ValueError("process defaults require a category")

    if reset:
        if process_key and category_key:
            policy["processes"].get(category_key, {}).pop(process_key, None)
            if not policy["processes"].get(category_key):
                policy["processes"].pop(category_key, None)
            scope = f"process:{category_key}.{process_key}"
        elif category_key:
            policy["categories"].pop(category_key, None)
            policy["processes"].pop(category_key, None)
            scope = f"category:{category_key}"
        else:
            policy["global"] = copy.deepcopy(DEFAULT_POLICY["global"])
            scope = "global"
        saved = save_browser_policy(policy, policy_path=policy_path)
        return {"schema": "whatsapp.browser.preference.update.v1", "scope": scope, "policy": saved}

    normalized_browser_mode = normalize_browser_mode(browser_mode)
    normalized_login_mode = normalize_login_mode(login_mode)
    if not normalized_browser_mode and not normalized_login_mode:
        raise ValueError("browser_mode, login_mode or reset=True is required")

    target: dict[str, Any]
    if process_key and category_key:
        target = policy["processes"].setdefault(category_key, {}).setdefault(process_key, {})
        scope = f"process:{category_key}.{process_key}"
    elif category_key:
        target = policy["categories"].setdefault(category_key, {})
        scope = f"category:{category_key}"
    else:
        target = policy["global"]
        scope = "global"

    if normalized_browser_mode:
        target["browser_mode"] = normalized_browser_mode
    if normalized_login_mode:
        target["login_mode"] = normalized_login_mode

    saved = save_browser_policy(policy, policy_path=policy_path)
    return {"schema": "whatsapp.browser.preference.update.v1", "scope": scope, "policy": saved}


def _browser_binary() -> str | None:
    configured = os.environ.get("WHATSAPP_MCP_BROWSER_BIN")
    if configured:
        path = Path(configured).expanduser()
        if path.exists():
            return str(path.resolve())
    for candidate in ("google-chrome", "chromium", "chromium-browser", "brave-browser"):
        path = shutil.which(candidate)
        if path:
            return path
    for candidate in (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _display_env_for_headed() -> dict[str, str] | None:
    display = os.environ.get("DISPLAY")
    if not display:
        for socket in (Path("/tmp/.X11-unix/X0"), Path("/tmp/.X11-unix/X20")):
            if socket.exists():
                display = f":{socket.name[1:]}"
                break
    if not display:
        return None
    env = dict(os.environ)
    env["DISPLAY"] = display
    xauthority = Path.home() / ".Xauthority"
    if xauthority.exists() and "XAUTHORITY" not in env:
        env["XAUTHORITY"] = str(xauthority)
    return env


def browser_runtime_status() -> dict[str, Any]:
    browser_binary = _browser_binary()
    playwright_spec = importlib.util.find_spec("playwright")
    headed_env = _display_env_for_headed()
    return {
        "schema": "whatsapp.browser.runtime.v1",
        "playwright_python": {
            "available": playwright_spec is not None,
            "loader": type(playwright_spec.loader).__name__ if playwright_spec and playwright_spec.loader else None,
        },
        "browser_binary": {
            "path": browser_binary,
            "available": bool(browser_binary),
            "launch_strategy": "system_binary" if browser_binary else "playwright_managed_browser",
        },
        "headed_display": {
            "available": bool(headed_env and headed_env.get("DISPLAY")),
            "display": headed_env.get("DISPLAY") if headed_env else None,
            "xauthority": headed_env.get("XAUTHORITY") if headed_env else None,
        },
        "profile_root": str(PROFILE_ROOT),
        "artifact_root": str(ARTIFACT_ROOT),
        "active_sessions": sorted(_SESSIONS),
        "dispatcher_status": "text_dispatch_verified_after_token_confirmation; media_reply_and_forwarded_dispatch_blocked_until_real_web_ui_smoke_passes",
    }


def get_browser_page(session_id: str | None = "default") -> Any | None:
    session = _SESSIONS.get(_safe_session_id(session_id))
    return session.get("page") if session else None


def _validate_whatsapp_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "web.whatsapp.com":
        raise ValueError("Only https://web.whatsapp.com/ is allowed for this browser session")
    return url


def _start_playwright() -> Any:
    global _PLAYWRIGHT
    if _PLAYWRIGHT is None:
        from playwright.sync_api import sync_playwright

        _PLAYWRIGHT = sync_playwright().start()
    return _PLAYWRIGHT


async def _start_playwright_async() -> Any:
    global _PLAYWRIGHT
    if _PLAYWRIGHT is None:
        from playwright.async_api import async_playwright

        _PLAYWRIGHT = await async_playwright().start()
    return _PLAYWRIGHT


async def detect_whatsapp_auth_state(page: Any, timeout_ms: int = 15000) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + max(timeout_ms, 0) / 1000
    matched: str | None = None
    state = "loading_or_unknown"

    while True:
        for name, selector in LOGGED_IN_SELECTORS.items():
            try:
                if await page.locator(selector).count():
                    matched = name
                    state = "logged_in"
                    break
            except Exception:
                continue
        if matched:
            break

        for name, selector in LOGIN_REQUIRED_SELECTORS.items():
            try:
                if await page.locator(selector).count():
                    matched = name
                    state = "login_required"
                    break
            except Exception:
                continue
        if matched or asyncio.get_running_loop().time() >= deadline:
            break
        await page.wait_for_timeout(1000)

    return {
        "schema": "whatsapp.browser.auth_state.v1",
        "state": state,
        "matched": matched,
        "requires_login": state == "login_required",
    }


async def open_browser_session_async(
    category: str | None = "login",
    process: str | None = "web_login",
    browser_mode: str | None = None,
    login_mode: str | None = None,
    session_id: str | None = "default",
    url: str = WHATSAPP_WEB_URL,
    capture_qr: bool = True,
    force_restart: bool = False,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    safe_session = _safe_session_id(session_id)
    policy = resolve_browser_policy(
        category=category,
        process=process,
        browser_mode=browser_mode,
        login_mode=login_mode,
    )
    launch_mode = policy["login_phase"]["browser_mode"]
    url = _validate_whatsapp_url(url)
    status = browser_runtime_status()
    if not status["playwright_python"]["available"]:
        raise RuntimeError("Playwright is not installed in this MCP Python environment")
    if safe_session in _SESSIONS and not force_restart:
        session = _SESSIONS[safe_session]
        return {
            "schema": "whatsapp.browser.session.v1",
            "status": "already_open",
            "session_id": safe_session,
            "browser_policy": policy,
            "browser_session": {
                "browser_mode": session["browser_mode"],
                "profile_dir": session["profile_dir"],
                "url": session.get("url"),
            },
            "auth_state": session.get("auth_state"),
            "note": "Use force_restart=True to reopen this session with a different headless/headed mode.",
        }
    if safe_session in _SESSIONS:
        await close_browser_session_async(safe_session)

    profile_dir = PROFILE_ROOT / safe_session
    artifact_dir = ARTIFACT_ROOT / safe_session
    profile_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    playwright = await _start_playwright_async()
    context = None
    try:
        launch_kwargs: dict[str, Any] = {}
        if status["browser_binary"]["path"]:
            launch_kwargs["executable_path"] = status["browser_binary"]["path"]
        if launch_mode == "headed":
            headed_env = _display_env_for_headed()
            if headed_env:
                launch_kwargs["env"] = headed_env
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=(launch_mode == "headless"),
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-dev-shm-usage",
                "--no-default-browser-check",
                "--no-first-run",
            ],
            **launch_kwargs,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        auth_state = await detect_whatsapp_auth_state(
            page,
            timeout_ms=min(max(timeout_ms - 1000, 5000), 60000),
        )
        screenshot_file: str | None = None
        if capture_qr:
            screenshot_path = artifact_dir / "whatsapp-web-login.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            screenshot_file = str(screenshot_path)
        _SESSIONS[safe_session] = {
            "context": context,
            "page": page,
            "browser_mode": launch_mode,
            "profile_dir": str(profile_dir),
            "artifact_dir": str(artifact_dir),
            "url": page.url,
            "auth_state": auth_state,
        }
        screenshot_artifact = {
            "file_path": screenshot_file,
            "kind": "login_qr" if auth_state["requires_login"] else "session_state",
            "delivery": policy["login_phase"]["qr_delivery"],
        } if screenshot_file else None
        return {
            "schema": "whatsapp.browser.session.v1",
            "status": "opened",
            "session_id": safe_session,
            "browser_policy": policy,
            "browser_session": {
                "browser_mode": launch_mode,
                "profile_dir": str(profile_dir),
                "url": page.url,
            },
            "auth_state": auth_state,
            "screenshot_artifact": screenshot_artifact,
            "qr_artifact": screenshot_artifact if auth_state["requires_login"] else None,
        }
    except Exception:
        if context is not None:
            await context.close()
        raise


def open_browser_session(
    category: str | None = "login",
    process: str | None = "web_login",
    browser_mode: str | None = None,
    login_mode: str | None = None,
    session_id: str | None = "default",
    url: str = WHATSAPP_WEB_URL,
    capture_qr: bool = True,
    force_restart: bool = False,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            open_browser_session_async(
                category=category,
                process=process,
                browser_mode=browser_mode,
                login_mode=login_mode,
                session_id=session_id,
                url=url,
                capture_qr=capture_qr,
                force_restart=force_restart,
                timeout_ms=timeout_ms,
            )
        )
    raise RuntimeError("open_browser_session cannot run inside an event loop; use open_browser_session_async")


async def close_browser_session_async(session_id: str | None = "default") -> dict[str, Any]:
    global _PLAYWRIGHT
    safe_session = _safe_session_id(session_id)
    session = _SESSIONS.pop(safe_session, None)
    if not session:
        return {
            "schema": "whatsapp.browser.session.close.v1",
            "status": "not_open",
            "session_id": safe_session,
        }
    await session["context"].close()
    if not _SESSIONS and _PLAYWRIGHT is not None:
        await _PLAYWRIGHT.stop()
        _PLAYWRIGHT = None
    return {
        "schema": "whatsapp.browser.session.close.v1",
        "status": "closed",
        "session_id": safe_session,
        "browser_mode": session["browser_mode"],
        "profile_dir": session["profile_dir"],
    }


def close_browser_session(session_id: str | None = "default") -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(close_browser_session_async(session_id))
    raise RuntimeError("close_browser_session cannot run inside an event loop; use close_browser_session_async")
