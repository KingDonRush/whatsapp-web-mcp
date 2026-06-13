# WhatsApp Web MCP Agent Instructions

## Operational contract

1. Use WhatsApp Web through the persistent Playwright profile.
2. Do not read SQLite, IndexedDB snapshots or desktop-wrapper databases.
3. Prefer DOM/accessibility extraction. Screenshots are diagnostic artifacts.
4. Treat browser profiles, QR images, exports and pending sends as sensitive.
5. Default to headless. Use headed only for the operation requested by the user.
6. Use WhisperX as the transcription backend. Video is converted to audio first.
7. Never associate captured media with a message unless the match is reliable.
8. Keep unmatched captures in `unassigned_media_captures`.
9. Never send from a search, export, probe or draft request.
10. Sending requires explicit intent, prepare, preview and exact token confirmation.
11. Do not bypass the confirmation gate, including in test chats.
12. Dispatch support is currently verified for plain text and documents,
    including ZIP files.
13. Media and reply probes must cancel without sending and return `sent=false`.
14. Keep other media, reply and forwarding dispatch blocked until their own
    confirmed Web UI smoke has passed.
15. Consume a confirmation token after a successful dispatch so it cannot be
    replayed. Preserve it after failures so the user can explicitly retry.

## Validation

```bash
python -m unittest discover -s tests -v
python -m compileall server.py whatsapp_web_mcp tests
```
