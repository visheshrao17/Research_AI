"""Accounts, sessions and the token pool.

Roles: admin (manages the pool, sees everything), user (signed up or created by the admin) and guest
(anonymous, time-limited, gets the guest allotment the admin configured).

Token accounting: the admin sets a total pool. Every LLM call is written to `token_ledger`, so
  consumed  = all tokens ever spent (the ledger survives user/run deletion)
  committed = what active users/guests still have left of their allotments
  available = pool - consumed - committed     <- what the admin can still hand out (or spend)
Granting tokens to someone requires `available` to cover it, so the pool can never be over-committed.
"""
import hashlib
import hmac
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from . import config, db

log = logging.getLogger("researchx.auth")
COOKIE = "rx_session"
_PBKDF2_ROUNDS = 240_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")

DEFAULT_SETTINGS = {
    "token_pool": 2_000_000,       # total tokens the admin owns
    "default_user_tokens": 150_000,  # allotment for new sign-ups
    "guest_tokens": 60_000,        # allotment per guest session (a quick run costs ~40-50k)
    "guest_enabled": True,
    "signup_enabled": True,
    "guest_max_depth": "standard",  # quick | standard | deep
    "guest_uploads": False,        # dataset analysis runs generated code; off for guests by default
}
DEPTH_ORDER = ["quick", "standard", "deep"]


# ---------------- settings ----------------
def settings() -> dict:
    return {k: db.get_setting(k, v) for k, v in DEFAULT_SETTINGS.items()}


def update_settings(patch: dict) -> dict:
    for k, v in patch.items():
        if k not in DEFAULT_SETTINGS or v is None:
            continue
        default = DEFAULT_SETTINGS[k]
        if isinstance(default, bool):
            v = bool(v)
        elif isinstance(default, int):
            v = int(v)
            if v < 0:
                raise HTTPException(400, f"{k} must be >= 0")
        elif k == "guest_max_depth" and v not in DEPTH_ORDER:
            raise HTTPException(400, "guest_max_depth must be quick|standard|deep")
        db.set_setting(k, v)
    if pool_status()["available"] < 0:
        # roll back only the pool size if it was shrunk below what is already spent/committed
        if "token_pool" in patch:
            st = pool_status()
            db.set_setting("token_pool", st["consumed"] + st["committed"])
            raise HTTPException(400, f"The pool can't be smaller than consumed + committed "
                                     f"({st['consumed'] + st['committed']:,} tokens). It was set to that minimum.")
    return settings()


# ---------------- passwords ----------------
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, rounds, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(dk.hex(), digest)
    except ValueError:
        return False


# ---------------- users ----------------
@dataclass
class User:
    id: str
    username: str
    role: str
    token_limit: int | None
    tokens_used: int
    disabled: bool
    expires_at: float | None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_guest(self) -> bool:
        return self.role == "guest"


def _row_to_user(r: dict) -> User:
    return User(id=r["id"], username=r["username"], role=r["role"], token_limit=r["token_limit"],
                tokens_used=r["tokens_used"], disabled=bool(r["disabled"]), expires_at=r["expires_at"])


def get_user(user_id: str) -> User | None:
    rows = db.query("SELECT * FROM users WHERE id=?", (user_id,))
    return _row_to_user(rows[0]) if rows else None


def create_user(username: str, password: str | None, role: str, token_limit: int | None,
                expires_at: float | None = None, ip: str | None = None) -> User:
    if role != "guest" and not USERNAME_RE.match(username):
        raise HTTPException(400, "Username must be 3-32 characters: letters, digits, _ . -")
    if password is not None and len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if db.query("SELECT 1 FROM users WHERE lower(username)=lower(?)", (username,)):
        raise HTTPException(409, "That username is taken")
    uid = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO users (id, username, password_hash, role, token_limit, tokens_used, created_at, expires_at, created_ip) "
        "VALUES (?,?,?,?,?,0,?,?,?)",
        (uid, username, hash_password(password) if password else None, role, token_limit, time.time(), expires_at, ip),
    )
    return get_user(uid)


def remaining(user: User) -> int | None:
    """Tokens this user can still spend (None = no personal cap). Always also bounded by the pool."""
    st = pool_status()
    pool_left = st["pool"] - st["consumed"]
    if user.token_limit is None:
        return max(0, min(pool_left, st["available"]))
    return max(0, min(user.token_limit - user.tokens_used, pool_left))


def public_user(user: User) -> dict:
    return {
        "id": user.id, "username": user.username, "role": user.role,
        "token_limit": user.token_limit, "tokens_used": user.tokens_used,
        "tokens_left": remaining(user), "expires_at": user.expires_at,
    }


def ensure_admin() -> None:
    """Create the admin from ADMIN_USERNAME / ADMIN_PASSWORD on first start; adopt runs that predate auth."""
    admins = db.query("SELECT id FROM users WHERE role='admin'")
    if not admins:
        pw = config.ADMIN_PASSWORD or secrets.token_urlsafe(12)
        admin = create_user(config.ADMIN_USERNAME, pw, "admin", None)
        if not config.ADMIN_PASSWORD:
            log.warning("Created admin '%s' with generated password: %s  (set ADMIN_PASSWORD in backend/.env)",
                        config.ADMIN_USERNAME, pw)
        admins = [{"id": admin.id}]
    db.execute("UPDATE runs SET user_id=? WHERE user_id IS NULL", (admins[0]["id"],))


# ---------------- pool ----------------
def pool_status() -> dict:
    pool = int(db.get_setting("token_pool", DEFAULT_SETTINGS["token_pool"]))
    consumed = db.query("SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS n FROM token_ledger")[0]["n"]
    now = time.time()
    committed = db.query(
        "SELECT COALESCE(SUM(MAX(token_limit - tokens_used, 0)), 0) AS n FROM users "
        "WHERE token_limit IS NOT NULL AND disabled=0 AND (expires_at IS NULL OR expires_at > ?)", (now,),
    )[0]["n"]
    return {"pool": pool, "consumed": consumed, "committed": committed,
            "available": pool - consumed - committed}


def grant(user: User, new_limit: int) -> None:
    """Set a user's allotment, checking the pool can cover the increase in what they may still spend."""
    if new_limit < 0:
        raise HTTPException(400, "Token limit must be >= 0")
    old_left = max((user.token_limit or 0) - user.tokens_used, 0) if user.token_limit is not None else 0
    new_left = max(new_limit - user.tokens_used, 0)
    st = pool_status()
    if new_left - old_left > st["available"]:
        raise HTTPException(400, f"Not enough unallocated tokens in the pool: {st['available']:,} available, "
                                 f"{new_left - old_left:,} needed. Raise the pool size first.")
    db.execute("UPDATE users SET token_limit=? WHERE id=?", (new_limit, user.id))


# ---------------- sessions ----------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user: User) -> tuple[str, float]:
    token = secrets.token_urlsafe(32)
    ttl = config.GUEST_HOURS * 3600 if user.is_guest else config.SESSION_DAYS * 86400
    expires = time.time() + ttl
    if user.expires_at:
        expires = min(expires, user.expires_at)
    db.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
               (_hash_token(token), user.id, time.time(), expires))
    return token, expires


def end_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def user_from_token(token: str | None) -> User | None:
    if not token:
        return None
    rows = db.query("SELECT user_id, expires_at FROM sessions WHERE token_hash=?", (_hash_token(token),))
    if not rows or rows[0]["expires_at"] < time.time():
        return None
    user = get_user(rows[0]["user_id"])
    if not user or user.disabled or (user.expires_at and user.expires_at < time.time()):
        return None
    db.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), user.id))
    return user


def purge_expired() -> int:
    now = time.time()
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return db.execute("UPDATE users SET disabled=1 WHERE role='guest' AND expires_at < ? AND disabled=0", (now,)).rowcount


def guests_today_from(ip: str) -> int:
    return db.query("SELECT COUNT(*) AS n FROM users WHERE role='guest' AND created_ip=? AND created_at > ?",
                    (ip, time.time() - 86400))[0]["n"]


# ---------------- FastAPI dependencies ----------------
def current_user(request: Request) -> User:
    user = user_from_token(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "Sign in required")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Admin only")
    return user


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
