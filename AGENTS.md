# WhatsApp Web MCP Agent Instructions

## Public API

Use somente:

- `whatsapp_status`
- `whatsapp_list_chats`
- `whatsapp_get_messages`
- `whatsapp_get_media`
- `whatsapp_export_chat`
- `whatsapp_transcribe_file`
- `whatsapp_prepare_action`
- `whatsapp_confirm_action`
- `whatsapp_action_status`

Todas as chamadas recebem um objeto `request`.

## Domain Contract

1. Ask for chats, messages, media, exports, transcriptions or actions as domain
   data. Do not manage browser sessions, selectors, scroll pages or UI state.
2. Never use an external Playwright/browser/computer-use tool to compensate for
   an MCP read failure. Return the short domain error and `diagnostics_id`.
3. Do not expose DOM, composer, persisted position, browser profile, Playwright
   or recovery mechanics to the user.
4. Use `whatsapp_status` first when authentication state is unknown.
5. Use `whatsapp_list_chats` to resolve ambiguity, then prefer `chat_id`.
6. For recent messages, trust `latest_boundary_verified`; pagination uses only
   the opaque `next_cursor`.
7. Reads never send, clear or replace user drafts.
8. Default operation mode is headless. Set `observe=true` only for the current
   operation when the user asks to watch it.
9. Use `whatsapp_get_media` with the exact `chat_id` and `message_id`. Never
   guess media associations.
10. Use WhisperX only. Videos are converted to audio before transcription.
11. Treat browser profiles, QR artifacts, exports, media and action files as
    sensitive.

## Sending

1. Sending requires an explicit user order.
2. Call `whatsapp_prepare_action` and show its preview.
3. Do not call `whatsapp_confirm_action` until the user literally confirms
   `CONFIRMO ENVIAR <action_id>`.
4. Pass `user_confirmed=true` only after that confirmation.
5. Never infer confirmation from approval of a draft, file, plan or recipient.
6. Never retry a send automatically when its outcome is ambiguous.
7. Text and documents, including ZIP files, are currently verified.
8. Other media, reply and forwarding remain blocked until their own confirmed
   WhatsApp Web smoke passes.
9. A successful action is consumed atomically and must not be replayed.

## Validation

```bash
python -m unittest discover -s tests -v
python -m compileall server.py whatsapp_web_mcp tests
```
