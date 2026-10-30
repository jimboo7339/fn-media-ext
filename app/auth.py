"""Single-admin accounts and cookie sessions (stdlib only)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from . import db

ITERATIONS = 200_000
SESSION_SECONDS = 30 * 86_400


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


def account_count() -> int:
    with db.session_scope() as con:
        return con.execute("SELECT COUNT(*) AS n FROM account").fetchone()["n"]


def first_account() -> dict | None:
    with db.session_scope() as con:
        row = con.execute("SELECT * FROM account ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def create_account(username: str, password: str) -> bool:
    username = username.strip()
    if not username or len(password) < 6:
        return False
    try:
        with db.session_scope() as con:
            con.execute(
                "INSERT INTO account(username, passwd, created_at) VALUES(?, ?, ?)",
                (username, hash_password(password), int(time.time() * 1000)),
            )
        return True
    except Exception:  # noqa: BLE001 - unique constraint
        return False


def check_login(username: str, password: str) -> dict | None:
    with db.session_scope() as con:
        row = con.execute("SELECT * FROM account WHERE username = ?", (username.strip(),)).fetchone()
    if row and verify_password(password, row["passwd"]):
        return dict(row)
    return None


def new_session(account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db.session_scope() as con:
        con.execute(
            "INSERT INTO session(token, account_id, created_at, expires_at) VALUES(?, ?, ?, ?)",
            (token, account_id, now * 1000, (now + SESSION_SECONDS) * 1000),
        )
    return token


def account_by_token(token: str | None) -> dict | None:
    if not token:
        return None
    now = int(time.time() * 1000)
    with db.session_scope() as con:
        row = con.execute(
            "SELECT a.* FROM session s JOIN account a ON a.id = s.account_id "
            "WHERE s.token = ? AND s.expires_at > ?",
            (token, now),
        ).fetchone()
    return dict(row) if row else None


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with db.session_scope() as con:
        con.execute("DELETE FROM session WHERE token = ?", (token,))


def change_password(account_id: int, old: str, new: str) -> bool:
    with db.session_scope() as con:
        row = con.execute("SELECT passwd FROM account WHERE id = ?", (account_id,)).fetchone()
        if not row or not verify_password(old, row["passwd"]) or len(new) < 6:
            return False
        con.execute("UPDATE account SET passwd = ? WHERE id = ?", (hash_password(new), account_id))
        con.execute("DELETE FROM session WHERE account_id = ?", (account_id,))
    return True
