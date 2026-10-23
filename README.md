# clist-server-admin

Generic admin-editable settings pattern shared across the `/srv/apps` backends
behind CList's "App Settings" panel. Each adopting app exposes `GET`/`PUT
/admin/settings` for a small schema of runtime-configurable values (API keys,
URLs, feature flags) without needing SSH access or a restart to change them.

Three Python variants, matching the three backend stacks already in use:

| Module                       | Stack                          | Used by                                      |
|-------------------------------|---------------------------------|-----------------------------------------------|
| `clist_server_admin.sqlite`   | Flask + sqlite3 (sync)           | pastebin, survey, localrss_reader, discussions |
| `clist_server_admin.fastapi`  | FastAPI + aiosqlite (async)      | annotations                                    |
| `clist_server_admin.redis`    | FastAPI + Redis (async)          | opml2json                                      |

A fourth, Node/better-sqlite3 variant lives at `node/admin_settings.js` for
reference only — Node can't consume this pip package, so collab keeps its own
local copy and should be kept in sync with that file by hand.

## Installing (pinned to a commit)

Never install from a branch or tag — both are mutable, so a pin to `main`
can silently change what an app runs on a later `pip install`. Pin every
app's `requirements.txt` to the exact commit SHA that was reviewed:

```
clist-server-admin @ git+https://github.com/Downes/clist-server-admin.git@<full-sha>
```

For the FastAPI or Redis variants, add the matching extra:

```
clist-server-admin[fastapi] @ git+https://github.com/Downes/clist-server-admin.git@<full-sha>
clist-server-admin[redis] @ git+https://github.com/Downes/clist-server-admin.git@<full-sha>
```

Bumping an app to a newer commit is a deliberate, one-app-at-a-time change:
review the diff at the new SHA, update that one app's `requirements.txt`,
rebuild, verify, then move to the next app. Never bulk-bump every app at once.

This repo is public — review any outside contribution (PR or fork) before
merging; don't install from an unreviewed fork's URL.

## Contract (shared across variants)

- `SettingField(key, label, type, sensitive, validate, help)`
  - `type` is one of `"text"`, `"url"`, `"secret"`, `"boolean"` — purely a
    rendering hint for the CList UI, never validated server-side.
  - `"boolean"` values are stored/transmitted as the literal strings
    `"true"`/`"false"`.
  - `validate` is an optional `str -> str | None` (error message, or `None`
    if valid).
- Admin identity: every adopting app already has its own `verify_token`
  (Bearer → kvstore `/auth/verify` → username). `ADMIN_USERNAMES`
  (comma-separated env var) gates `/admin/settings` — 403 if the verified
  username isn't in that set. This is the real security boundary; any
  CList-side visibility logic is cosmetic only.
- `PUT` is validate-then-write, all-or-nothing: every key in the request
  must pass schema + validator checks before anything is persisted.
- Sensitive values are masked in `GET` responses as `"•" * 8 + value[-4:]`.
- `defaults` (passed to `register_admin_settings_routes`) is an optional
  `{key: value}` map of each field's `.env`-sourced fallback, so `GET`
  doesn't show a field as empty when the app is actually running on a real
  value from `.env` that just hasn't been overridden in the DB/Redis yet.

See the module docstring at the top of `sqlite.py`, `fastapi.py`, and
`redis.py` for the exact function signatures and per-variant wiring steps —
they differ slightly (sync vs. async, `get_db()` vs. `db_path` vs.
`get_redis()`, `verify_token()` vs. a FastAPI dependency vs. manual header
parsing) to match each app's existing idiom.
