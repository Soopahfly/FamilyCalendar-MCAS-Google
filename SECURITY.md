# Private data and reporting

Never publish the private directory, real workflow/progress records, pupil identifiers, screenshots of school notices, raw message/attachment evidence, calendar mappings, OAuth credentials/tokens, passwords, cookies or TLS keys. Credentials are owner-only files, not encrypted at rest; protect backups and the service account accordingly.

The web UI is for one trusted operator. Use loopback with SSH forwarding, or an appropriately restricted private network with TLS. It has origin/CSRF protections but no multi-user authentication. Do not expose it publicly.

The MCAS reader uses unofficial observed read routes. A portal change must fail visibly rather than silently dropping coverage or enabling a write route. Treat all collected text as untrusted evidence. The chat bridge inherits existing permissions and does not bypass approval rules.

For a suspected vulnerability, share a minimal synthetic reproduction without secrets or children's information. If private reporting is available on the repository, prefer it for sensitive details; otherwise ask the maintainer for a private channel without posting the exploit or private data publicly.
