# FastAPI/aiosqlite variant of the generic admin-settings module.
#
# Differences from the Flask/sqlite3 variant (clist_server_admin.sqlite):
#   - All DB access is async (aiosqlite), opening a fresh connection per
#     call — matches a lifespan-managed db_path style, not a cached g.db object.
#   - Admin gating is a FastAPI dependency (make_admin_dependency), not a
#     zero-arg verify_token() returning (username, err). Pass it the app's
#     existing verify_token dependency; it wraps it with an is_admin() check
#     and raises HTTPException(403) on failure.
#
# Contract:
#   SettingField(key, label, type, sensitive, validate, help)
#     - type in {"text", "url", "secret", "boolean"}
#     - "boolean" values are stored/transmitted as the literal strings
#       "true"/"false" (same TEXT column as everything else) — the CList
#       renderer draws a checkbox for this type and the app treats the
#       string "false" as off, anything else as on.
#     - validate: optional str -> str|None (error message, or None if valid)
#   await init_settings_table(db_path)                — call from app startup/lifespan
#   await get_setting(db_path, key, default=None)      — call at point of use, every call
#   is_admin(username)                                 — username in ADMIN_USERNAMES
#   make_admin_dependency(verify_token_dep)             — FastAPI dependency factory
#   register_admin_settings_routes(app, schema, db_path, admin_dependency, defaults=None)
#     — registers GET/PUT /admin/settings
#
# Adopting an app:
#   1. pip install this package with the [fastapi] extra (see README.md for
#      the pinned-commit install line).
#   2. Call await init_settings_table(db_path) from the app's lifespan/startup.
#   3. Declare SETTINGS_SCHEMA, build admin_dep = make_admin_dependency(verify_token),
#      and call register_admin_settings_routes(app, SETTINGS_SCHEMA, db_path, admin_dep, defaults).
#   4. Replace frozen constant reads at their point of use with
#      await get_setting(db_path, "KEY", default=os.environ.get("KEY", "...")).
#   5. Add ADMIN_USERNAMES=<comma-separated usernames> to .env / compose environment.
#   6. Update that app's CLAUDE.md: new env var, new /admin/settings
#      endpoints, DB-override-falls-back-to-env precedence.

import os
from collections import namedtuple

import aiosqlite
from fastapi import Depends, FastAPI, HTTPException

SettingField = namedtuple("SettingField", ["key", "label", "type", "sensitive", "validate", "help"])
SettingField.__new__.__defaults__ = (None, None)  # validate, help are optional

ADMIN_USERNAMES = {
    u.strip() for u in os.environ.get("ADMIN_USERNAMES", "").split(",") if u.strip()
}


def is_admin(username):
    return bool(username) and username in ADMIN_USERNAMES


async def init_settings_table(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


async def get_setting(db_path, key, default=None):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return row[0]
            return default


async def _set_settings(db_path, data: dict):
    async with aiosqlite.connect(db_path) as db:
        for key, value in data.items():
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value),
            )
        await db.commit()


def _mask(value):
    # Fixed-length mask regardless of real length, so the mask itself never
    # leaks how long the underlying secret is.
    if not value:
        return None
    return "•" * 8 + value[-4:]


def make_admin_dependency(verify_token_dep):
    """Wraps the app's existing verify_token dependency with an is_admin()
    check. Returns a FastAPI dependency usable as Depends(admin_dep)."""
    async def _admin_dependency(user: dict = Depends(verify_token_dep)) -> dict:
        if not is_admin(user.get("username")):
            raise HTTPException(status_code=403, detail="Forbidden")
        return user
    return _admin_dependency


def register_admin_settings_routes(app: FastAPI, schema, db_path, admin_dependency, defaults=None):
    """Registers GET/PUT /admin/settings on `app`. `schema` is a list of
    SettingField. `db_path`/`admin_dependency` are the app's SQLite path and
    the dependency from make_admin_dependency(). `defaults` is an optional
    {key: value} map of the env-sourced fallback each field would resolve to
    if no admin override exists yet — the same value the app's own
    point-of-use get_setting(..., default=...) call uses. Without it, GET
    would show every unsaved field as empty even though the app is actually
    running on a real value from the environment."""
    schema_by_key = {f.key: f for f in schema}
    defaults = defaults or {}

    def _schema_json():
        return [
            {"key": f.key, "label": f.label, "type": f.type, "sensitive": f.sensitive, "help": f.help}
            for f in schema
        ]

    async def _masked_values():
        out = {}
        for f in schema:
            raw = await get_setting(db_path, f.key, default=defaults.get(f.key))
            out[f.key] = _mask(raw) if f.sensitive else raw
        return out

    @app.get("/admin/settings")
    async def admin_get_settings(user: dict = Depends(admin_dependency)):
        return {"schema": _schema_json(), "values": await _masked_values()}

    @app.put("/admin/settings")
    async def admin_put_settings(data: dict, user: dict = Depends(admin_dependency)):
        for key in data:
            if key not in schema_by_key:
                raise HTTPException(status_code=400, detail=f"Unknown setting: {key}")
        for key, value in data.items():
            validate = schema_by_key[key].validate
            if validate:
                error = validate(value)
                if error:
                    raise HTTPException(status_code=400, detail=f"{key}: {error}")

        # Validate-then-write: nothing is persisted unless every key in the
        # request passes, so a partial PUT never leaves settings half-applied.
        await _set_settings(db_path, data)
        return {"schema": _schema_json(), "values": await _masked_values()}
