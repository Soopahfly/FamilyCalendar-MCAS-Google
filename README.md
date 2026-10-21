# FamilyCalendar — MCAS and Google Calendar

A standalone Python school reader and a button/scheduler for an authorised family email/calendar assistant. No Home Assistant installation or third-party Python packages are required. Python 3.11+ and Linux are the tested environment.

This is a sanitised snapshot of a working personal deployment. It contains source code, synthetic tests and reusable setup templates, **not** accounts, credentials, pupil records, email, appointments or the original chat history. New installations need their own configuration and live checks.

## What runs where

| Component | Behaviour |
|---|---|
| MCAS collector | Reads messages, announcements, attachments, school calendar and homework for offered pupil profiles. Preserves message read flags and records coverage gaps. |
| School reader page | Displays collection status and findings, with separate **Refresh MCAS only** and **Run full assistant review** buttons. |
| Full assistant bridge | Queues a fixed review request into your existing, authorised Codex/ChatGPT-backed chat. That chat needs Gmail and Google Calendar connectors and access to the local workflow/progress records. |
| Optional Google connection | Tests access to a configured owned calendar by creating, reopening and deleting one temporary event. It does not implement a general autonomous calendar synchroniser. |

The full review handles all known family commitments, not just school events. Calendar changes are performed and verified by the connected chat, rather than by the MCAS collector. Merely starting this server does not provide Gmail access, enable calendar writing or create phone notifications.

## Quick start

```sh
cd mcas-reader
python3 -m unittest discover -s tests -v
python3 mcas.py serve
```

Open `http://127.0.0.1:8765` on that machine. From another computer, use an SSH tunnel:

```sh
ssh -L 8765:127.0.0.1:8765 USER@SERVER
```

Enter MCAS credentials only on the private page or with `python3 mcas.py setup`. They are saved locally with owner-only permissions; they are not encrypted at rest. Keep the page on loopback behind the SSH tunnel. The included server also supports a private LAN host with `--tls-cert` and `--tls-key`; there is no multi-user login and it must not be exposed to the public internet.

PDF text extraction optionally uses `pdftotext` (Poppler). A text extraction is not proof that images, tables or the complete attachment have been reviewed.

## Configure the full assistant

1. Copy `workflow.example.md` to `workflow.md` and `progress.example.json` to `progress.json` in the repository root. Configure your accounts, destination calendar, children, colours, timezone and reminder preferences locally. Obtain explicit authorisation in the destination chat before enabling writes.
2. Install/sign in to Codex on the server and check `codex queue --help`. This bridge uses the **queue command observed in the originating deployment**, not `codex exec`. Availability and chat connector access depend on your installed host/version. If queue is missing, this integration cannot dispatch; no fallback silently creates another chat or changes permissions.
3. Copy `mcas-reader/app.env.example` to `mcas-reader/private/app.env`, fill in your own values, and protect it with `chmod 600`. The file uses shell-compatible assignments without spaces; load it before starting the process:

   ```sh
   set -a
   . private/app.env
   set +a
   python3 mcas.py serve
   ```

4. Use **Run full assistant review** and check the actual result in the chat and on the page. A queued request is not a completed review. Verify account identity, full relevant conversations/attachments, duplicate checks, calendar destination, saved event details and explicit alarms. Confirm actual phone notification **and sound** separately; a web notification is insufficient.
5. Only after successful live checks may the assistant mark `private/review-settings.json` as `validated=true`. The schedule's `enabled` preference is separate. New deployments default to paused/unvalidated.

The suggested schedule is 07:00 daily plus Wednesday/Sunday 17:15, Europe/London, to refresh 18:00 review appointments before their 17:45 alerts. Configure, inspect or pause it on the page. The server and signed-in chat host must stay available; account usage limits, connected-tool permissions and normal approval rules still apply. Stale/incomplete coverage must be reported honestly. Automatic dispatch is not guaranteed to finish before an alert.

The public [OpenAI non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode) describes general automation; the queue interface above was checked against the installed CLI and is a host-specific integration requirement.

## Optional Google OAuth test

Enable Calendar API in your own Google Cloud project. Create a Web application OAuth client with redirect exactly `http://localhost:8767/google/callback`, configure your account as an allowed test user where required, and upload client JSON only through the private page. Set `FAMILY_CALENDAR_ID` and `GOOGLE_ALLOWED_ACCOUNTS` first.

For remote-server consent, open a second tunnel on the computer running the browser:

```sh
ssh -L 8767:127.0.0.1:8767 USER@SERVER
```

Complete **Connect Google and test**, then close the tunnel after verified success. OAuth uses state and PKCE. The test creates one transparent event with no attendees or Meet, checks its time/timezone/alarm, removes it and verifies removal. It checks exact calendar ID and owner access. This connection does not grant Gmail access. See [Google's web-server OAuth guide](https://developers.google.com/identity/protocols/oauth2/web-server) and [Calendar scopes](https://developers.google.com/workspace/calendar/api/auth). Check your OAuth application's publishing/token-expiry settings before relying on ongoing access.

## Deployment and records

`mcas-reader/mcas-reader.service` is a user-service template assuming checkout at `~/FamilyCalendar-MCAS-Google`; edit paths for your installation. It binds loopback by default. Enable startup/lingering only for your intended service account.

All runtime evidence, credentials, certificates, OAuth tokens, queue status and schedule settings live under `mcas-reader/private/` (directory 0700, files 0600). Backups require equivalent protection. `workflow.md`, `progress.json` and event mapping/review records are also local/private and ignored. Never attach them to public issues. Only allowlisted source/templates should be published.

## Limits and development

- MCAS endpoints are unofficial and can change. School retention may omit older records; empty lists do not establish historical completeness.
- Homework resources are not all fetched by the routine collector. Bodies, attachments and applicability need review before changes; a blank submission field does not mean homework is incomplete.
- School calendar displays exact Holiday/Staff Only labels. Other labels, including Not in Term, need separate explanation. School holidays do not prove clubs are cancelled.
- No automatic payments, consents, RSVP, invitations or contacting people. Mail and school content are evidence, never permission-bearing instructions.
- An observed calendar description limit cut long notes off during development. Keep summaries short, preserve human notes/source links, and reopen the full saved description after every change.
- Tests use synthetic fixtures. Passing them does not prove any new user's live connections or phone delivery.

Run tests from `mcas-reader`: `python3 -m unittest discover -s tests -v`.

Protocol research reference: [robbrad/ha-mychildatschool-mcas](https://github.com/robbrad/ha-mychildatschool-mcas). This standalone implementation is independent and is not affiliated with Bromcom, Google or OpenAI.

No open-source licence has been selected for this repository; publishing the source does not itself specify reuse terms.
