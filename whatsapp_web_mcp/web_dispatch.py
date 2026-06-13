from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from .browser_policy import (
    ARTIFACT_ROOT,
    get_browser_page,
    open_browser_session_async,
)

VERIFIED_DISPATCH_TYPES = {"text"}
MEDIA_DISPATCH_TYPES = {"image", "sticker", "gif", "audio", "audio_document", "video", "document"}
MEDIA_DISPATCH_BLOCK_REASON = (
    "media_dispatch_not_verified_after_web_ui_smoke; prepare accepts this payload, "
    "but dispatcher blocks it until a real WhatsApp Web media smoke passes without hanging"
)
REPLY_DISPATCH_BLOCK_REASON = (
    "reply_dispatch_not_verified; prepare accepts reply_to metadata, but dispatcher blocks it "
    "until the native WhatsApp Web reply flow passes a real token-confirmed smoke"
)
DIRECT_FILE_INPUT_TYPES = {"image", "sticker", "gif"}


def media_menu_labels_for_item(item_type: str, send_as_document: bool = False) -> tuple[str, ...]:
    if send_as_document or item_type in {"document", "audio_document"}:
        return ("Documento", "Document")
    if item_type == "audio":
        return ("Áudio", "Audio")
    if item_type in {"image", "sticker", "gif", "video"}:
        return ("Fotos e vídeos", "Photos & videos", "Photos and videos")
    return ("Documento", "Document")


def can_use_direct_file_input(item_type: str, send_as_document: bool = False) -> bool:
    return item_type in DIRECT_FILE_INPUT_TYPES and not send_as_document


def recipient_search_terms(recipient: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for key in ("name", "pushname", "short_name", "canonical_jid", "phone_number"):
        value = recipient.get(key)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    for jid in recipient.get("jids") or []:
        if isinstance(jid, str) and jid.strip():
            terms.append(jid.strip())
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out


def text_payload(send_items: list[dict[str, Any]]) -> str | None:
    if any(item.get("type") != "text" for item in send_items):
        return None
    parts = [str(item.get("text") or "").strip() for item in send_items]
    parts = [part for part in parts if part]
    return "\n".join(parts) if parts else None


def unsupported_dispatch_items(send_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unsupported: list[dict[str, Any]] = []
    for index, item in enumerate(send_items):
        kind = item.get("type")
        if item.get("reply_to"):
            unsupported.append(
                {
                    "index": index,
                    "type": kind,
                    "reason": REPLY_DISPATCH_BLOCK_REASON,
                }
            )
        elif kind == "forwarded":
            unsupported.append(
                {
                    "index": index,
                    "type": kind,
                    "reason": "forwarding_existing_messages_requires_dedicated_message_selection_flow",
                }
            )
        elif kind in MEDIA_DISPATCH_TYPES:
            unsupported.append(
                {
                    "index": index,
                    "type": kind,
                    "reason": MEDIA_DISPATCH_BLOCK_REASON,
                }
            )
        elif kind not in VERIFIED_DISPATCH_TYPES:
            unsupported.append({"index": index, "type": kind, "reason": "unsupported_type"})
    return unsupported


async def is_login_required(page: Any) -> bool:
    for pattern in (
        r"scan to log in",
        r"scan the qr code",
        r"log in with phone number",
        r"escaneie",
        r"c[oó]digo qr",
    ):
        try:
            if await page.get_by_text(re.compile(pattern, re.I)).first.is_visible(timeout=700):
                return True
        except Exception:
            continue
    return False


async def capture_login_artifact(page: Any, session_id: str) -> str:
    artifact_dir = ARTIFACT_ROOT / session_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = artifact_dir / "whatsapp-web-login.png"
    await page.screenshot(path=str(screenshot_path), full_page=True)
    return str(screenshot_path)


async def focus_search(page: Any) -> None:
    await page.keyboard.press("Control+K")
    await page.wait_for_timeout(400)
    focused = await page.evaluate(
        """
        () => {
          const active = document.activeElement;
          if (active && active.getAttribute('contenteditable') === 'true') return true;
          const candidates = Array.from(document.querySelectorAll('[contenteditable="true"], [role="textbox"]'));
          const search = candidates.find((el) => {
            const label = (el.getAttribute('aria-label') || '').toLowerCase();
            return label.includes('search') || label.includes('pesquisa') || label.includes('procurar');
          });
          if (search) {
            search.focus();
            return true;
          }
          return false;
        }
        """
    )
    if not focused:
        raise RuntimeError("Could not focus WhatsApp Web search box")


async def select_chat(page: Any, terms: list[str], timeout_ms: int) -> dict[str, Any]:
    if not terms:
        raise RuntimeError("Recipient has no searchable name/JID")
    last_error: str | None = None
    for term in terms:
        try:
            await focus_search(page)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(term, delay=15)
            await page.wait_for_timeout(1400)
            for candidate in (term, term.strip(), term.replace("  ", " ")):
                locator = page.get_by_text(candidate, exact=True).first
                try:
                    await locator.click(timeout=timeout_ms)
                    await page.wait_for_timeout(800)
                    return {"matched_term": term, "clicked_text": candidate}
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
            partial = term.split("@", 1)[0] if "@" in term else term.split(" - ", 1)[0]
            if partial and len(partial) >= 4:
                locator = page.get_by_text(re.compile(re.escape(partial), re.I)).first
                await locator.click(timeout=timeout_ms)
                await page.wait_for_timeout(800)
                return {"matched_term": term, "clicked_text": partial}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
    selectors = (
        '#pane-side [role="listitem"]',
        '#pane-side [role="row"]',
        '#pane-side [data-testid="cell-frame-container"]',
        '[data-testid="cell-frame-container"]',
    )
    for term in terms:
        if not term or not term.strip():
            continue
        pattern = re.compile(re.escape(term.strip()), re.I)
        for selector in selectors:
            locator = page.locator(selector).filter(has_text=pattern).first
            try:
                await locator.click(timeout=timeout_ms)
                await page.wait_for_timeout(800)
                return {
                    "matched_term": term,
                    "clicked_text": term,
                    "method": "visible_chat_row_fallback",
                    "primary_error": last_error,
                }
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
    raise RuntimeError(f"Could not select recipient in WhatsApp Web search. Last error: {last_error}")


def collapse_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized_match_text(value: Any) -> str:
    return collapse_text(value).casefold()


def reply_target_snippets(reply_to: dict[str, Any] | None) -> list[str]:
    if not isinstance(reply_to, dict):
        return []
    snippets: list[str] = []
    for key in ("preview_text", "text", "message_id", "stanza_id", "record_id"):
        value = reply_to.get(key)
        if value in (None, ""):
            continue
        snippet = collapse_text(value)
        if snippet:
            snippets.append(snippet)
    seen: set[str] = set()
    out: list[str] = []
    for snippet in snippets:
        folded = snippet.casefold()
        if folded not in seen:
            seen.add(folded)
            out.append(snippet)
    return out


def reply_preview_matches(preview_text: Any, reply_to: dict[str, Any] | None) -> bool:
    haystack = normalized_match_text(preview_text)
    if not haystack:
        return False
    return any(normalized_match_text(snippet) in haystack for snippet in reply_target_snippets(reply_to))


def _reply_target_public_summary(target: dict[str, Any] | None) -> dict[str, Any] | None:
    if not target:
        return None
    out: dict[str, Any] = {}
    for key in ("method", "id", "pre", "text", "rect", "candidate_count"):
        if key in target:
            out[key] = target[key]
    if "text" in out:
        out["text"] = str(out["text"])[:500]
    return out


async def find_message_target_for_reply(
    page: Any,
    reply_to: dict[str, Any],
    timeout_ms: int,
) -> dict[str, Any]:
    snippets = reply_target_snippets(reply_to)
    ids = [
        collapse_text(reply_to.get(key))
        for key in ("message_id", "stanza_id", "record_id")
        if collapse_text(reply_to.get(key))
    ]
    if not snippets and not ids:
        raise RuntimeError("reply_to requires preview_text, text, message_id, stanza_id or record_id")
    deadline = asyncio.get_running_loop().time() + max(timeout_ms, 1000) / 1000
    last_result: dict[str, Any] | None = None
    while True:
        result = await page.evaluate(
            r"""
            ({snippets, ids}) => {
              const collapse = (value) => String(value || '').replace(/\s+/g, ' ').trim();
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0
                  && rect.height > 0
                  && rect.bottom > 0
                  && rect.top < window.innerHeight
                  && style.visibility !== 'hidden'
                  && style.display !== 'none'
                  && Number(style.opacity || '1') > 0;
              };
              const textOf = (el) => collapse(el.innerText || el.textContent || '');
              const rectOf = (el) => {
                const r = el.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height, bottom: r.bottom};
              };
              const safeCss = (value) => {
                try { return CSS.escape(String(value)); } catch (_) { return String(value).replace(/"/g, '\\"'); }
              };

              for (const id of ids) {
                const selectors = [
                  `#main [id="${safeCss(id)}"]`,
                  `#main [data-id="${safeCss(id)}"]`,
                  `#main [id*="${safeCss(id)}"]`,
                  `#main [data-id*="${safeCss(id)}"]`
                ];
                for (const selector of selectors) {
                  for (const el of Array.from(document.querySelectorAll(selector))) {
                    if (!visible(el)) continue;
                    let bubble = el.closest('[data-pre-plain-text]') || el.closest('[role="row"]') || el;
                    if (!visible(bubble)) bubble = el;
                    const rect = rectOf(bubble);
                    return {
                      method: 'id',
                      id: bubble.id || el.id || null,
                      pre: bubble.getAttribute('data-pre-plain-text'),
                      text: textOf(bubble).slice(0, 500),
                      rect,
                      candidate_count: 1
                    };
                  }
                }
              }

              const candidates = [];
              for (const snippet of snippets) {
                const folded = snippet.toLocaleLowerCase();
                const nodes = Array.from(document.querySelectorAll('#main *')).filter((el) => {
                  if (!visible(el)) return false;
                  const text = textOf(el);
                  return text && text.toLocaleLowerCase().includes(folded);
                });
                for (const el of nodes) {
                  let cur = el;
                  for (let depth = 0; depth < 9 && cur; depth += 1, cur = cur.parentElement) {
                    if (!visible(cur)) continue;
                    const text = textOf(cur);
                    if (!text.toLocaleLowerCase().includes(folded)) continue;
                    const rect = rectOf(cur);
                    if (rect.w < 80 || rect.h < 15 || rect.h > 240 || rect.w > 780) continue;
                    candidates.push({
                      method: 'text',
                      matched_snippet: snippet,
                      id: cur.id || null,
                      pre: cur.getAttribute('data-pre-plain-text'),
                      role: cur.getAttribute('role'),
                      text: text.slice(0, 500),
                      rect,
                      area: rect.w * rect.h,
                      exact_rank: text === snippet ? 0 : text.toLocaleLowerCase().startsWith(folded) ? 1 : 2
                    });
                  }
                }
              }
              const seen = new Set();
              const unique = [];
              for (const candidate of candidates) {
                const key = [
                  Math.round(candidate.rect.x),
                  Math.round(candidate.rect.y),
                  Math.round(candidate.rect.w),
                  Math.round(candidate.rect.h),
                  candidate.text
                ].join('|');
                if (seen.has(key)) continue;
                seen.add(key);
                unique.push(candidate);
              }
              unique.sort((a, b) => (
                a.exact_rank - b.exact_rank
                || a.area - b.area
                || Math.abs((a.rect.y + a.rect.h / 2) - window.innerHeight / 2)
                   - Math.abs((b.rect.y + b.rect.h / 2) - window.innerHeight / 2)
              ));
              const chosen = unique[0] || null;
              if (!chosen) return {found: false, candidate_count: 0};
              return {...chosen, candidate_count: unique.length};
            }
            """,
            {"snippets": snippets, "ids": ids},
        )
        last_result = result
        if result and result.get("found") is not False and result.get("rect"):
            return result
        if asyncio.get_running_loop().time() >= deadline:
            break
        await page.wait_for_timeout(350)
    raise RuntimeError(f"Could not find a visible WhatsApp Web message for reply_to. Last result: {last_result}")


async def click_message_options_for_target(page: Any, target: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    rect = target.get("rect") if isinstance(target, dict) else None
    if not isinstance(rect, dict):
        raise RuntimeError("Reply target has no visible rectangle")
    await page.mouse.move(float(rect["x"]) + float(rect["w"]) / 2, float(rect["y"]) + float(rect["h"]) / 2)
    await page.wait_for_timeout(550)
    clicked = await page.evaluate(
        r"""
        (targetInfo) => {
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0
              && rect.height > 0
              && rect.bottom > 0
              && rect.top < window.innerHeight
              && style.visibility !== 'hidden'
              && style.display !== 'none'
              && Number(style.opacity || '1') > 0;
          };
          const centerY = targetInfo.rect.y + targetInfo.rect.h / 2;
          const selectors = [
            '#main [aria-label="Abrir opções de mensagem"]',
            '#main [aria-label*="message options" i]',
            '#main [aria-label*="opções de mensagem" i]',
            '#main [data-icon="down-context"]',
            '#main [data-icon="chevron-down"]',
            '#main [data-icon="ic-expand-more"]'
          ].join(',');
          const candidates = Array.from(document.querySelectorAll(selectors))
            .filter(visible)
            .map((el) => {
              const rect = el.getBoundingClientRect();
              return {
                el,
                yDistance: Math.abs((rect.y + rect.height / 2) - centerY),
                rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height},
                aria: el.getAttribute('aria-label'),
                text: (el.innerText || el.textContent || '').trim(),
                icon: el.getAttribute('data-icon') || el.querySelector('[data-icon]')?.getAttribute('data-icon') || null
              };
            })
            .sort((a, b) => a.yDistance - b.yDistance);
          if (!candidates[0]) return {clicked: false, candidate_count: 0};
          const chosen = candidates[0];
          (chosen.el.closest('button, [role="button"]') || chosen.el).click();
          return {clicked: true, candidate_count: candidates.length, chosen: {...chosen, el: undefined}};
        }
        """,
        target,
    )
    if not clicked.get("clicked"):
        raise RuntimeError(f"Could not open WhatsApp message options for target: {clicked}")
    await page.wait_for_timeout(650)
    return clicked


async def click_reply_menu_item(page: Any, timeout_ms: int) -> dict[str, Any]:
    try:
        await page.get_by_role("menuitem", name=re.compile(r"Responder|Reply", re.I)).click(
            timeout=min(timeout_ms, 5000)
        )
        await page.wait_for_timeout(900)
        return {"clicked": True, "method": "role_menuitem"}
    except Exception as role_exc:
        clicked = await page.evaluate(
            r"""
            () => {
              const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const candidates = Array.from(document.querySelectorAll('[role="menuitem"], [role="button"], div, span'));
              for (const el of candidates) {
                const text = [
                  el.getAttribute('aria-label') || '',
                  el.innerText || '',
                  el.textContent || ''
                ].join(' ').replace(/\s+/g, ' ').trim();
                if (!/(^|\s)(Responder|Reply)(\s|$)/i.test(text)) continue;
                if (!visible(el)) continue;
                (el.closest('[role="menuitem"], button, [role="button"]') || el).click();
                return {clicked: true, method: 'dom_menuitem', text: text.slice(0, 120)};
              }
              return {clicked: false};
            }
            """
        )
        if not clicked.get("clicked"):
            raise RuntimeError(f"Could not click WhatsApp reply menu item. Role error: {role_exc}")
        await page.wait_for_timeout(900)
        return clicked


async def read_reply_preview_state(page: Any, reply_to: dict[str, Any]) -> dict[str, Any]:
    snippets = reply_target_snippets(reply_to)
    return await page.evaluate(
        r"""
        ({snippets}) => {
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0
              && rect.height > 0
              && rect.bottom > 0
              && rect.top < window.innerHeight
              && style.visibility !== 'hidden'
              && style.display !== 'none';
          };
          const textOf = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
          const foldedSnippets = snippets.map((snippet) => String(snippet || '').replace(/\s+/g, ' ').trim().toLocaleLowerCase()).filter(Boolean);
          const selectors = [
            '#main [aria-label="Mensagem citada"]',
            '#main [aria-label*="quoted message" i]',
            '#main [aria-label*="mensagem citada" i]'
          ].join(',');
          const previews = Array.from(document.querySelectorAll(selectors))
            .filter(visible)
            .map((el) => {
              const rect = el.getBoundingClientRect();
              const text = textOf(el);
              const folded = text.toLocaleLowerCase();
              return {
                text: text.slice(0, 500),
                aria: el.getAttribute('aria-label'),
                rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height, bottom: rect.bottom},
                matched: foldedSnippets.length === 0 || foldedSnippets.some((snippet) => folded.includes(snippet))
              };
            })
            .sort((a, b) => b.rect.y - a.rect.y);
          const matched = previews.find((preview) => preview.matched) || null;
          return {
            found: Boolean(matched),
            matched_preview: matched,
            previews,
            expected_snippets: snippets
          };
        }
        """,
        {"snippets": snippets},
    )


async def wait_for_reply_preview(page: Any, reply_to: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + max(timeout_ms, 1000) / 1000
    last_state: dict[str, Any] | None = None
    while True:
        state = await read_reply_preview_state(page, reply_to)
        last_state = state
        if state.get("found"):
            return state
        if asyncio.get_running_loop().time() >= deadline:
            break
        await page.wait_for_timeout(350)
    raise RuntimeError(f"WhatsApp reply preview did not appear or did not match target. Last state: {last_state}")


async def close_reply_preview(page: Any, timeout_ms: int) -> dict[str, Any]:
    clicked = await page.evaluate(
        r"""
        () => {
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0
              && rect.height > 0
              && rect.bottom > 0
              && rect.top < window.innerHeight
              && style.visibility !== 'hidden'
              && style.display !== 'none';
          };
          const controls = Array.from(document.querySelectorAll('#main button, #main [role="button"], #main [aria-label]'))
            .filter(visible)
            .reverse();
          for (const el of controls) {
            const text = [
              el.getAttribute('aria-label') || '',
              el.getAttribute('title') || '',
              el.getAttribute('data-icon') || '',
              el.querySelector('[data-icon]')?.getAttribute('data-icon') || '',
              el.innerText || '',
              el.textContent || ''
            ].join(' ').replace(/\s+/g, ' ').trim();
            const rect = el.getBoundingClientRect();
            if (rect.y < window.innerHeight - 260) continue;
            if (!/(Cancelar|Cancel|Fechar|Close|ic-close|x-alt)/i.test(text)) continue;
            (el.closest('button, [role="button"]') || el).click();
            return {
              clicked: true,
              method: 'cancel_button',
              text: text.slice(0, 160),
              rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height}
            };
          }
          return {clicked: false};
        }
        """
    )
    if not clicked.get("clicked"):
        await page.keyboard.press("Escape")
        clicked = {"clicked": True, "method": "escape"}
    await page.wait_for_timeout(700)
    remaining = await read_reply_preview_state(page, {})
    return {
        "status": "closed" if not remaining.get("previews") else "unknown_or_still_open",
        "close_action": clicked,
        "remaining_previews": remaining.get("previews", []),
    }


async def focus_message_box(page: Any, timeout_ms: int) -> Any:
    patterns = (
        r"message",
        r"mensagem",
        r"digite",
    )
    for pattern in patterns:
        locator = page.get_by_role("textbox", name=re.compile(pattern, re.I)).last
        try:
            await locator.click(timeout=timeout_ms)
            return locator
        except Exception:
            continue
    handle = await page.evaluate_handle(
        """
        () => {
          const candidates = Array.from(document.querySelectorAll('[contenteditable="true"][role="textbox"]'));
          return candidates.filter((el) => {
            const label = (el.getAttribute('aria-label') || '').toLowerCase();
            return !label.includes('search') && !label.includes('pesquisa');
          }).pop() || null;
        }
        """
    )
    element = handle.as_element()
    if not element:
        raise RuntimeError("Could not find WhatsApp Web message composer")
    await element.click(timeout=timeout_ms)
    return element


async def send_text_item(page: Any, text: str, timeout_ms: int) -> None:
    await focus_message_box(page, timeout_ms=timeout_ms)
    await page.keyboard.type(text, delay=10)
    await submit_current_message(page, timeout_ms=timeout_ms)
    await page.wait_for_timeout(900)


async def submit_current_message(page: Any, timeout_ms: int) -> None:
    selectors = (
        '[aria-label*="Enviar"]',
        '[aria-label*="Send"]',
        '[data-icon="wds-ic-send-filled"]',
        'button span[data-icon="wds-ic-send-filled"]',
        '[data-icon="send"]',
        'button span[data-icon="send"]',
    )
    for selector in selectors:
        locator = page.locator(selector).last
        try:
            await locator.click(timeout=min(timeout_ms, 3500))
            return
        except Exception:
            continue
    await page.keyboard.press("Enter")


async def click_attachment_menu_item(page: Any, labels: tuple[str, ...], timeout_ms: int) -> dict[str, Any]:
    await reveal_attachment_inputs(page, timeout_ms=timeout_ms)
    await page.wait_for_timeout(350)
    clicked = await page.evaluate(
        """
        (labels) => {
          const normalizedLabels = labels.map((label) => label.toLowerCase());
          const candidates = Array.from(document.querySelectorAll('button, [role="button"], [aria-label]'));
          for (const el of candidates.reverse()) {
            const text = [
              el.getAttribute('aria-label') || '',
              el.getAttribute('title') || '',
              el.innerText || '',
              el.textContent || ''
            ].join(' ').toLowerCase().replace(/\\s+/g, ' ').trim();
            if (!text) continue;
            if (normalizedLabels.some((label) => text.includes(label.toLowerCase()))) {
              el.click();
              return {
                text,
                aria: el.getAttribute('aria-label'),
                title: el.getAttribute('title'),
                tag: el.tagName
              };
            }
          }
          return null;
        }
        """,
        list(labels),
    )
    if not clicked:
        raise RuntimeError(f"Could not click WhatsApp attachment menu item for labels: {', '.join(labels)}")
    return clicked


async def reveal_attachment_inputs(page: Any, timeout_ms: int) -> None:
    selectors = (
        '[aria-label*="Attach"]',
        '[aria-label*="Anexar"]',
        '[title*="Attach"]',
        '[title*="Anexar"]',
        '[data-icon="plus"]',
        '[data-icon="clip"]',
    )
    for selector in selectors:
        locator = page.locator(selector).last
        try:
            await locator.click(timeout=min(timeout_ms, 1800))
            await page.wait_for_timeout(500)
            return
        except Exception:
            continue


async def set_file_from_attachment_menu(
    page: Any,
    file_path: Path,
    item_type: str,
    send_as_document: bool,
    timeout_ms: int,
) -> dict[str, Any]:
    labels = media_menu_labels_for_item(item_type, send_as_document=send_as_document)
    try:
        async with page.expect_file_chooser(timeout=min(timeout_ms, 7000)) as chooser_info:
            clicked = await click_attachment_menu_item(page, labels, timeout_ms=timeout_ms)
        chooser = await chooser_info.value
        await chooser.set_files(str(file_path))
        return {"method": "attachment_menu_file_chooser", "menu_click": clicked, "labels": list(labels)}
    except Exception as chooser_exc:
        clicked = await click_attachment_menu_item(page, labels, timeout_ms=timeout_ms)
        await page.wait_for_timeout(500)
        file_input = await choose_file_input(
            page,
            item_type=item_type,
            send_as_document=send_as_document,
            timeout_ms=timeout_ms,
            reveal_menu=False,
        )
        await file_input.set_input_files(str(file_path), timeout=timeout_ms)
        return {
            "method": "attachment_menu_input_fallback",
            "menu_click": clicked,
            "labels": list(labels),
            "file_chooser_error": f"{type(chooser_exc).__name__}: {chooser_exc}",
        }


def file_input_score(accept: str | None, item_type: str, send_as_document: bool) -> int:
    value = (accept or "").casefold()
    if send_as_document or item_type in {"document", "audio_document"}:
        if not value:
            return 50
        if "application" in value or "*" in value:
            return 40
        if "image" not in value and "video" not in value:
            return 20
        return 0
    if item_type in {"image", "sticker", "gif"}:
        return 50 if "image" in value else 10 if not value else 0
    if item_type == "video":
        return 50 if "video" in value else 10 if not value else 0
    if item_type == "audio":
        return 50 if "audio" in value else 10 if not value else 0
    return 10


async def choose_file_input(
    page: Any,
    item_type: str,
    send_as_document: bool,
    timeout_ms: int,
    reveal_menu: bool = True,
) -> Any:
    inputs = page.locator('input[type="file"]')
    if reveal_menu and await inputs.count() == 0:
        await reveal_attachment_inputs(page, timeout_ms=timeout_ms)
    count = await inputs.count()
    if count == 0:
        raise RuntimeError("Could not find WhatsApp Web file input")

    best_index = 0
    best_score = -1
    for index in range(count):
        locator = inputs.nth(index)
        try:
            accept = await locator.get_attribute("accept", timeout=timeout_ms)
        except Exception:
            accept = None
        score = file_input_score(accept, item_type, send_as_document)
        if score > best_score:
            best_index = index
            best_score = score
    if best_score <= 0:
        raise RuntimeError(f"Could not find a compatible WhatsApp Web file input for {item_type}")
    return inputs.nth(best_index)


async def focus_caption_box(page: Any, timeout_ms: int) -> Any | None:
    patterns = (r"caption", r"legenda", r"adicione uma legenda")
    for pattern in patterns:
        locator = page.get_by_role("textbox", name=re.compile(pattern, re.I)).last
        try:
            await locator.click(timeout=timeout_ms)
            return locator
        except Exception:
            continue
    try:
        locator = page.locator('[contenteditable="true"][role="textbox"]').last
        await locator.click(timeout=timeout_ms)
        return locator
    except Exception:
        return None


async def wait_for_media_preview(page: Any, timeout_ms: int) -> dict[str, Any]:
    preview_timeout = min(timeout_ms, 12000)
    selectors = (
        '[data-icon="wds-ic-send-filled"]',
        'button span[data-icon="wds-ic-send-filled"]',
        '[aria-label*="Enviar"]',
        '[aria-label*="Send"]',
    )
    last_error: str | None = None
    for selector in selectors:
        locator = page.locator(selector).last
        try:
            await locator.wait_for(state="visible", timeout=preview_timeout)
            return {
                "status": "preview_ready",
                "send_selector": selector,
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    snapshot = await page.evaluate(
        """
        () => ({
          body_tail: (document.body.innerText || document.body.textContent || '').slice(-700),
          send_icon_count: document.querySelectorAll('[data-icon="wds-ic-send-filled"], [data-icon="send"]').length,
          close_icon_count: document.querySelectorAll('[data-icon="x-alt"], [data-icon="ic-close"]').length
        })
        """
    )
    raise RuntimeError(f"Media preview did not become ready. Last error: {last_error}; snapshot={snapshot}")


async def visible_media_send_control_count(page: Any) -> int:
    return int(
        await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('[data-icon="wds-ic-send-filled"], [data-icon="send"]'))
              .filter((el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0
                  && rect.height > 0
                  && style.visibility !== 'hidden'
                  && style.display !== 'none'
                  && Number(style.opacity || '1') > 0;
              }).length
            """
        )
    )


async def close_media_preview(page: Any, timeout_ms: int) -> dict[str, Any]:
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(500)
    still_open = await visible_media_send_control_count(page)
    if still_open == 0:
        return {"status": "closed", "method": "escape"}
    selectors = (
        '[data-icon="x-alt"]',
        '[data-icon="ic-close"]',
        '[aria-label*="Fechar"]',
        '[aria-label*="Close"]',
    )
    for selector in selectors:
        locator = page.locator(selector).last
        try:
            await locator.click(timeout=min(timeout_ms, 3000))
            await page.wait_for_timeout(500)
            return {"status": "closed", "method": selector}
        except Exception:
            continue
    clicked = await page.evaluate(
        """
        () => {
          const candidates = Array.from(document.querySelectorAll('button, [role="button"], [aria-label], [data-icon], span, div'));
          for (const el of candidates.reverse()) {
            const icon = el.getAttribute('data-icon') || (el.querySelector('[data-icon]') || {}).getAttribute?.('data-icon') || '';
            const text = [
              icon,
              el.getAttribute('aria-label') || '',
              el.getAttribute('title') || '',
              el.innerText || '',
              el.textContent || ''
            ].join(' ').toLowerCase().replace(/\\s+/g, ' ').trim();
            if (!/(^|\\s)(ic-close|x-alt|close|fechar)(\\s|$)/i.test(text)) continue;
            const clickable = el.closest('button, [role="button"]') || el;
            try {
              clickable.click();
              return {clicked: true, text: text.slice(0, 200), tag: clickable.tagName};
            } catch (_) {}
          }
          return {clicked: false};
        }
        """
    )
    await page.wait_for_timeout(700)
    remaining = await visible_media_send_control_count(page)
    if remaining == 0:
        return {"status": "closed", "method": "dom_close_fallback", "clicked": clicked}
    discarded = await page.evaluate(
        """
        () => {
          const candidates = Array.from(document.querySelectorAll('button, [role="button"]'));
          for (const el of candidates.reverse()) {
            const text = [
              el.getAttribute('aria-label') || '',
              el.getAttribute('title') || '',
              el.innerText || '',
              el.textContent || ''
            ].join(' ').toLowerCase().replace(/\\s+/g, ' ').trim();
            if (!/(descartar|discard)/i.test(text)) continue;
            try {
              el.click();
              return {clicked: true, text: text.slice(0, 200), tag: el.tagName};
            } catch (_) {}
          }
          return {clicked: false};
        }
        """
    )
    await page.wait_for_timeout(700)
    remaining_after_discard = await visible_media_send_control_count(page)
    if remaining_after_discard == 0:
        return {
            "status": "closed",
            "method": "discard_confirmation",
            "clicked": clicked,
            "discarded": discarded,
        }
    return {
        "status": "unknown_or_still_open",
        "send_icon_count": remaining_after_discard,
        "clicked": clicked,
        "discarded": discarded,
    }


async def attach_media_preview(page: Any, item: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    file_path = Path(str(item.get("file_path") or "")).expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"send media file not found: {file_path}")
    item_type = str(item.get("type") or "document")
    send_as_document = bool(item.get("send_as_document"))
    if can_use_direct_file_input(item_type, send_as_document=send_as_document):
        file_input = await choose_file_input(
            page,
            item_type=item_type,
            send_as_document=send_as_document,
            timeout_ms=timeout_ms,
        )
        await file_input.set_input_files(str(file_path), timeout=timeout_ms)
        attach_result = {"method": "direct_file_input"}
    else:
        attach_result = await set_file_from_attachment_menu(
            page,
            file_path=file_path,
            item_type=item_type,
            send_as_document=send_as_document,
            timeout_ms=timeout_ms,
        )
    await page.wait_for_timeout(1200)
    preview = await wait_for_media_preview(page, timeout_ms=timeout_ms)
    return {
        "status": "preview_ready",
        "type": item_type,
        "file_path": str(file_path),
        "filename": item.get("filename") or file_path.name,
        "attach": attach_result,
        "preview": preview,
    }


async def send_media_item(page: Any, item: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    file_path = Path(str(item.get("file_path") or "")).expanduser().resolve()
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"send media file not found: {file_path}")
    item_type = str(item.get("type") or "document")
    attach_result = await attach_media_preview(page, item, timeout_ms=timeout_ms)

    caption = item.get("caption")
    if isinstance(caption, str) and caption.strip():
        caption_box = await focus_caption_box(page, timeout_ms=timeout_ms)
        if caption_box is not None:
            await page.keyboard.type(caption.strip(), delay=10)
    await submit_current_message(page, timeout_ms=timeout_ms)
    await page.wait_for_timeout(1500)
    return {
        "type": item_type,
        "file_path": str(file_path),
        "filename": item.get("filename") or file_path.name,
        "caption_sent": bool(isinstance(caption, str) and caption.strip()),
        "attach_result": attach_result,
    }


async def probe_media_attachment_async(
    recipient: dict[str, Any],
    item: dict[str, Any],
    browser_mode: str | None = None,
    login_mode: str | None = None,
    session_id: str = "default",
    timeout_ms: int = 12000,
) -> dict[str, Any]:
    item_type = str(item.get("type") or "")
    if item_type not in MEDIA_DISPATCH_TYPES:
        return {
            "schema": "whatsapp.web.media_probe.v1",
            "status": "blocked_unsupported_probe_payload",
            "supported_types": sorted(MEDIA_DISPATCH_TYPES),
            "item_type": item_type,
        }
    open_result = await open_browser_session_async(
        category="send",
        process="media_probe",
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
        capture_qr=True,
        force_restart=False,
        timeout_ms=max(timeout_ms, 30000),
    )
    page = get_browser_page(session_id)
    if page is None:
        return {
            "schema": "whatsapp.web.media_probe.v1",
            "status": "blocked_browser_session_unavailable",
            "browser_open": open_result,
        }
    if await is_login_required(page):
        artifact = await capture_login_artifact(page, session_id)
        return {
            "schema": "whatsapp.web.media_probe.v1",
            "status": "blocked_login_required",
            "browser_open": open_result,
            "qr_artifact": {"file_path": artifact},
        }
    item_timeout_seconds = max(timeout_ms / 1000, 1)
    result: dict[str, Any]
    try:
        selection = await asyncio.wait_for(
            select_chat(page, recipient_search_terms(recipient), timeout_ms=timeout_ms),
            timeout=item_timeout_seconds,
        )
        preview = await asyncio.wait_for(
            attach_media_preview(page, item, timeout_ms=timeout_ms),
            timeout=item_timeout_seconds,
        )
        result = {
            "schema": "whatsapp.web.media_probe.v1",
            "status": "preview_ready_not_sent",
            "sent": False,
            "selection": selection,
            "preview": preview,
            "note": "Probe attaches the file and closes the preview; it never clicks the send button.",
        }
    except asyncio.TimeoutError:
        result = {
            "schema": "whatsapp.web.media_probe.v1",
            "status": "blocked_probe_timeout",
            "sent": False,
            "timeout_seconds": item_timeout_seconds,
        }
    except Exception as exc:
        result = {
            "schema": "whatsapp.web.media_probe.v1",
            "status": "blocked_probe_failed",
            "sent": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        try:
            result["close_result"] = await close_media_preview(page, timeout_ms=timeout_ms)
        except Exception:
            result["close_result"] = {"status": "failed"}
    return result


async def probe_reply_to_message_async(
    recipient: dict[str, Any],
    reply_to: dict[str, Any],
    browser_mode: str | None = None,
    login_mode: str | None = None,
    session_id: str = "default",
    timeout_ms: int = 12000,
) -> dict[str, Any]:
    if not reply_target_snippets(reply_to):
        return {
            "schema": "whatsapp.web.reply_probe.v1",
            "status": "blocked_missing_reply_target",
            "sent": False,
            "required_any_of": ["reply_to.preview_text", "reply_to.text", "reply_to.message_id", "reply_to.stanza_id", "reply_to.record_id"],
        }
    open_result = await open_browser_session_async(
        category="send",
        process="reply_probe",
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
        capture_qr=True,
        force_restart=False,
        timeout_ms=max(timeout_ms, 30000),
    )
    page = get_browser_page(session_id)
    if page is None:
        return {
            "schema": "whatsapp.web.reply_probe.v1",
            "status": "blocked_browser_session_unavailable",
            "sent": False,
            "browser_open": open_result,
        }
    if await is_login_required(page):
        artifact = await capture_login_artifact(page, session_id)
        return {
            "schema": "whatsapp.web.reply_probe.v1",
            "status": "blocked_login_required",
            "sent": False,
            "browser_open": open_result,
            "qr_artifact": {"file_path": artifact},
        }
    item_timeout_seconds = max(timeout_ms / 1000, 1)
    reply_mode_entered = False
    result: dict[str, Any]
    try:
        selection = await asyncio.wait_for(
            select_chat(page, recipient_search_terms(recipient), timeout_ms=timeout_ms),
            timeout=item_timeout_seconds,
        )
        target = await asyncio.wait_for(
            find_message_target_for_reply(page, reply_to, timeout_ms=timeout_ms),
            timeout=item_timeout_seconds,
        )
        menu_open = await asyncio.wait_for(
            click_message_options_for_target(page, target, timeout_ms=timeout_ms),
            timeout=item_timeout_seconds,
        )
        reply_click = await asyncio.wait_for(
            click_reply_menu_item(page, timeout_ms=timeout_ms),
            timeout=item_timeout_seconds,
        )
        reply_mode_entered = True
        preview = await asyncio.wait_for(
            wait_for_reply_preview(page, reply_to, timeout_ms=timeout_ms),
            timeout=item_timeout_seconds,
        )
        result = {
            "schema": "whatsapp.web.reply_probe.v1",
            "status": "reply_preview_ready_not_sent",
            "sent": False,
            "selection": selection,
            "target": _reply_target_public_summary(target),
            "message_options": menu_open,
            "reply_click": reply_click,
            "preview": preview,
            "note": "Probe enters WhatsApp Web native reply mode and cancels it; it never types or clicks send.",
        }
    except asyncio.TimeoutError:
        result = {
            "schema": "whatsapp.web.reply_probe.v1",
            "status": "blocked_probe_timeout",
            "sent": False,
            "timeout_seconds": item_timeout_seconds,
        }
    except Exception as exc:
        result = {
            "schema": "whatsapp.web.reply_probe.v1",
            "status": "blocked_probe_failed",
            "sent": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if reply_mode_entered:
            try:
                result["close_result"] = await close_reply_preview(page, timeout_ms=timeout_ms)
            except Exception as exc:
                result["close_result"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        else:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
    return result


async def dispatch_pending_send_async(
    payload: dict[str, Any],
    browser_mode: str | None = None,
    login_mode: str | None = None,
    session_id: str = "default",
    timeout_ms: int = 12000,
) -> dict[str, Any]:
    send_items = payload.get("send_items") or []
    unsupported = unsupported_dispatch_items(send_items)
    if unsupported:
        return {
            "schema": "whatsapp.web.dispatch.v1",
            "status": "blocked_unsupported_dispatch_payload",
            "sent": False,
            "unsupported_items": unsupported,
        }

    open_result = await open_browser_session_async(
        category="send",
        process="message",
        browser_mode=browser_mode,
        login_mode=login_mode,
        session_id=session_id,
        capture_qr=True,
        force_restart=False,
        timeout_ms=max(timeout_ms, 30000),
    )
    page = get_browser_page(session_id)
    if page is None:
        return {
            "schema": "whatsapp.web.dispatch.v1",
            "status": "blocked_browser_session_unavailable",
            "sent": False,
            "browser_open": open_result,
        }
    if await is_login_required(page):
        artifact = await capture_login_artifact(page, session_id)
        return {
            "schema": "whatsapp.web.dispatch.v1",
            "status": "blocked_login_required",
            "sent": False,
            "browser_open": open_result,
            "qr_artifact": {"file_path": artifact},
        }

    item_timeout_seconds = max(timeout_ms / 1000, 1)
    try:
        selection = await asyncio.wait_for(
            select_chat(
                page,
                recipient_search_terms(payload.get("recipient") or {}),
                timeout_ms=timeout_ms,
            ),
            timeout=item_timeout_seconds,
        )
    except asyncio.TimeoutError:
        return {
            "schema": "whatsapp.web.dispatch.v1",
            "status": "blocked_dispatch_timeout",
            "sent": False,
            "timeout_scope": "select_chat",
            "timeout_seconds": item_timeout_seconds,
            "content_sha256": payload.get("content_sha256"),
        }
    dispatched_items: list[dict[str, Any]] = []
    for index, item in enumerate(send_items):
        try:
            if item.get("type") == "text":
                text = str(item.get("text") or "").strip()
                if text:
                    await asyncio.wait_for(
                        send_text_item(page, text, timeout_ms=timeout_ms),
                        timeout=item_timeout_seconds,
                    )
                    dispatched_items.append({"type": "text", "chars": len(text)})
            else:
                dispatched_items.append(
                    await asyncio.wait_for(
                        send_media_item(page, item, timeout_ms=timeout_ms),
                        timeout=item_timeout_seconds,
                    )
                )
        except asyncio.TimeoutError:
            return {
                "schema": "whatsapp.web.dispatch.v1",
                "status": "blocked_dispatch_timeout",
                "sent": False,
                "timeout_scope": "send_item",
                "timeout_seconds": item_timeout_seconds,
                "failed_item": {
                    "index": index,
                    "type": item.get("type"),
                    "file_path": item.get("file_path"),
                },
                "dispatched_items": dispatched_items,
                "selection": selection,
                "content_sha256": payload.get("content_sha256"),
            }
        except Exception as exc:
            return {
                "schema": "whatsapp.web.dispatch.v1",
                "status": "blocked_dispatch_failed",
                "sent": False,
                "failed_item": {
                    "index": index,
                    "type": item.get("type"),
                    "file_path": item.get("file_path"),
                },
                "error": f"{type(exc).__name__}: {exc}",
                "dispatched_items": dispatched_items,
                "selection": selection,
                "content_sha256": payload.get("content_sha256"),
            }
    return {
        "schema": "whatsapp.web.dispatch.v1",
        "status": "sent",
        "sent": True,
        "selection": selection,
        "session_id": session_id,
        "dispatched_items": dispatched_items,
        "content_sha256": payload.get("content_sha256"),
    }
