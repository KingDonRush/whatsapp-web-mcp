# Security Policy

## Sensitive runtime data

The persistent browser profile contains an authenticated WhatsApp Web session.
Exported conversations, media, QR screenshots and pending-send tokens are also
sensitive.

- Never commit `state/`, `runtime/`, browser profiles, exports or `.env` files.
- Store the data directory on encrypted storage with permissions limited to the
  service account.
- Do not expose this stdio MCP server directly to the public internet.
- On a VPS, run it behind an authenticated MCP-capable harness and restrict
  access to trusted users.
- Rotate the WhatsApp linked-device session if a profile is copied or exposed.
- Sending remains confirmation-gated. Do not remove or bypass that gate.

## Reporting

Open a private security advisory in the GitHub repository when possible. Avoid
including WhatsApp content, session files, QR codes, tokens or credentials in
public issues.
