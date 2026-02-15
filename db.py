"""
AUCTIONFINDER — SQLite Database for Users, Wallets & Transactions

Provides user authentication, wallet balance management, and an audit log
of all charges (research fees, lead fees) and top-ups (Stripe payments).
"""

import os
import secrets
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.environ.get("AUCTIONFINDER_DB", "auctionfinder.db")

# Ensure parent directory exists (important for Railway volume mount)
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

print(f"[DB] Using database at: {DB_PATH}")

# Thread-local storage for connections (SQLite is not thread-safe by default)
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Return a per-thread SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    """Create tables if they don't exist and ensure admin user exists."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            phone TEXT DEFAULT '',
            company TEXT DEFAULT '',
            is_admin INTEGER DEFAULT 0,
            is_trial INTEGER DEFAULT 0,
            trial_expires_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            balance_cents INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            type TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            description TEXT,
            job_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            token TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS search_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            job_id TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'running',
            nonprofit_count INTEGER DEFAULT 0,
            found_count INTEGER DEFAULT 0,
            billable_count INTEGER DEFAULT 0,
            total_cost_cents INTEGER DEFAULT 0,
            results_summary TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            expires_at TEXT DEFAULT (datetime('now', '+6 months'))
        );
    """)
    conn.commit()

    # Create or update admin user from env vars
    admin_email = os.environ.get("AUCTIONFINDER_ADMIN_EMAIL", "admin@auctionfinder.local").strip().lower()
    admin_password = os.environ.get("AUCTIONFINDER_PASSWORD", "admin")
    row = conn.execute("SELECT id FROM users WHERE email = ?", (admin_email,)).fetchone()
    if not row:
        pw_hash = generate_password_hash(admin_password)
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, is_admin) VALUES (?, ?, 1)",
            (admin_email, pw_hash),
        )
        conn.execute(
            "INSERT INTO wallets (user_id, balance_cents) VALUES (?, 0)",
            (cur.lastrowid,),
        )
        conn.commit()
    else:
        # Update password and ensure admin flag on every startup
        pw_hash = generate_password_hash(admin_password)
        conn.execute("UPDATE users SET password_hash = ?, is_admin = 1 WHERE id = ?",
                      (pw_hash, row["id"]))
        conn.commit()

    # Ensure blake@auctionintel.us is always admin
    blake = conn.execute("SELECT id FROM users WHERE email = ?", ("blake@auctionintel.us",)).fetchone()
    if blake:
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (blake["id"],))
        conn.commit()


TRIAL_PROMO_CODE = "26AUCTION26"
TRIAL_CREDIT_CENTS = 5000  # $50.00
TRIAL_DAYS = 7

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "mail.com", "protonmail.com", "zoho.com", "yandex.com",
    "live.com", "msn.com", "me.com", "inbox.com", "gmx.com",
}


def is_work_email(email: str) -> bool:
    """Check if email is a work email (not a free provider)."""
    domain = email.lower().split("@")[-1] if "@" in email else ""
    return domain not in FREE_EMAIL_DOMAINS


def create_user(email: str, password: str, phone: str = "",
                company: str = "", promo_code: str = "") -> int:
    """Create a new user and wallet. Returns user_id.
    If valid promo code, gives trial credit."""
    conn = _get_conn()
    pw_hash = generate_password_hash(password)

    is_trial = 0
    trial_expires = None
    initial_balance = 0

    if promo_code.strip().upper() == TRIAL_PROMO_CODE:
        is_trial = 1
        trial_expires = f"datetime('now', '+{TRIAL_DAYS} days')"
        initial_balance = TRIAL_CREDIT_CENTS

    if trial_expires:
        cur = conn.execute(
            f"INSERT INTO users (email, password_hash, phone, company, is_trial, trial_expires_at) "
            f"VALUES (?, ?, ?, ?, ?, {trial_expires})",
            (email, pw_hash, phone, company, is_trial),
        )
    else:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, phone, company) VALUES (?, ?, ?, ?)",
            (email, pw_hash, phone, company),
        )
    user_id = cur.lastrowid
    conn.execute(
        "INSERT INTO wallets (user_id, balance_cents) VALUES (?, ?)",
        (user_id, initial_balance),
    )
    if initial_balance > 0:
        conn.execute(
            "INSERT INTO transactions (user_id, type, amount_cents, description) "
            "VALUES (?, 'topup', ?, 'Free trial credit (promo: 26AUCTION26)')",
            (user_id, initial_balance),
        )
    conn.commit()
    return user_id


def authenticate(email: str, password: str) -> Optional[Dict[str, Any]]:
    """Verify credentials. Returns user dict or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, email, password_hash, is_admin, is_trial FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    if row and check_password_hash(row["password_hash"], password):
        return {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"]),
                "is_trial": bool(row["is_trial"])}
    return None


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Get user by id."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, email, is_admin, is_trial FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row:
        return {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"]),
                "is_trial": bool(row["is_trial"])}
    return None


def get_user_full(user_id: int) -> Optional[Dict[str, Any]]:
    """Get user with all fields including created_at."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, email, is_admin, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if row:
        return {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"]), "created_at": row["created_at"]}
    return None


def update_password(user_id: int, new_password: str):
    """Update user's password."""
    conn = _get_conn()
    pw_hash = generate_password_hash(new_password)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))
    conn.commit()


def get_spending_summary(user_id: int) -> Dict[str, Any]:
    """Get billing summary for a user."""
    conn = _get_conn()
    research = conn.execute(
        "SELECT COALESCE(SUM(ABS(amount_cents)), 0) FROM transactions WHERE user_id = ? AND type = 'research_fee'",
        (user_id,),
    ).fetchone()[0]
    leads = conn.execute(
        "SELECT COALESCE(SUM(ABS(amount_cents)), 0) FROM transactions WHERE user_id = ? AND type = 'lead_fee'",
        (user_id,),
    ).fetchone()[0]
    topups = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE user_id = ? AND type = 'topup'",
        (user_id,),
    ).fetchone()[0]
    job_count = conn.execute(
        "SELECT COUNT(DISTINCT job_id) FROM transactions WHERE user_id = ? AND job_id IS NOT NULL",
        (user_id,),
    ).fetchone()[0]
    return {
        "research_fees": research,
        "lead_fees": leads,
        "total_topups": topups,
        "total_spent": research + leads,
        "job_count": job_count,
    }


def get_job_breakdowns(user_id: int, limit: int = 20) -> list:
    """Get per-job billing breakdown."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT job_id, MIN(created_at) as started,
           SUM(CASE WHEN type='research_fee' THEN ABS(amount_cents) ELSE 0 END) as research_cost,
           SUM(CASE WHEN type='lead_fee' THEN ABS(amount_cents) ELSE 0 END) as lead_cost,
           SUM(CASE WHEN type='research_fee' THEN 1 ELSE 0 END) as nonprofits_searched,
           SUM(CASE WHEN type='lead_fee' THEN 1 ELSE 0 END) as leads_found
        FROM transactions
        WHERE user_id = ? AND job_id IS NOT NULL
        GROUP BY job_id
        ORDER BY MIN(created_at) DESC
        LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_balance(user_id: int) -> int:
    """Return wallet balance in cents."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT balance_cents FROM wallets WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return row["balance_cents"] if row else 0


def add_funds(user_id: int, amount_cents: int, description: str = "Stripe top-up"):
    """Credit wallet from a Stripe payment."""
    conn = _get_conn()
    conn.execute(
        "UPDATE wallets SET balance_cents = balance_cents + ? WHERE user_id = ?",
        (amount_cents, user_id),
    )
    conn.execute(
        "INSERT INTO transactions (user_id, type, amount_cents, description) VALUES (?, 'topup', ?, ?)",
        (user_id, amount_cents, description),
    )
    conn.commit()


def charge_research_fee(user_id: int, count: int, job_id: str, fee_cents_each: int = 8):
    """Deduct research fee (per-nonprofit). Returns total charged."""
    total = count * fee_cents_each
    conn = _get_conn()
    conn.execute(
        "UPDATE wallets SET balance_cents = balance_cents - ? WHERE user_id = ?",
        (total, user_id),
    )
    conn.execute(
        "INSERT INTO transactions (user_id, type, amount_cents, description, job_id) "
        "VALUES (?, 'research_fee', ?, ?, ?)",
        (user_id, -total, f"Research fee: {count} nonprofit(s) @ ${fee_cents_each/100:.2f}", job_id),
    )
    conn.commit()
    return total


def charge_lead_fee(user_id: int, tier: str, price_cents: int, job_id: str, nonprofit_name: str = ""):
    """Deduct lead fee for a billable result. Returns price_cents charged."""
    if price_cents <= 0:
        return 0
    conn = _get_conn()
    conn.execute(
        "UPDATE wallets SET balance_cents = balance_cents - ? WHERE user_id = ?",
        (price_cents, user_id),
    )
    conn.execute(
        "INSERT INTO transactions (user_id, type, amount_cents, description, job_id) "
        "VALUES (?, 'lead_fee', ?, ?, ?)",
        (user_id, -price_cents, f"Lead fee ({tier}): {nonprofit_name}", job_id),
    )
    conn.commit()
    return price_cents


def has_sufficient_balance(user_id: int, estimated_cost_cents: int) -> bool:
    """Check if user has enough balance for the estimated cost."""
    return get_balance(user_id) >= estimated_cost_cents


def get_transactions(user_id: int, limit: int = 50) -> list:
    """Return recent transactions for a user."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, type, amount_cents, description, job_id, created_at "
        "FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_research_fee_cents(total_nonprofits: int) -> int:
    """Tiered research fee per nonprofit."""
    if total_nonprofits <= 10000:
        return 8    # $0.08
    elif total_nonprofits <= 50000:
        return 7    # $0.07
    else:
        return 6    # $0.06


# ─── Search Job Persistence ──────────────────────────────────────────────────

def create_search_job(user_id: int, job_id: str, nonprofit_count: int):
    """Record a new search job."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO search_jobs (user_id, job_id, status, nonprofit_count) VALUES (?, ?, 'running', ?)",
        (user_id, job_id, nonprofit_count),
    )
    conn.commit()


def complete_search_job(job_id: str, found_count: int, billable_count: int,
                        total_cost_cents: int, results_summary: str = ""):
    """Mark a search job as complete with results."""
    conn = _get_conn()
    conn.execute(
        """UPDATE search_jobs SET status='complete', found_count=?, billable_count=?,
           total_cost_cents=?, results_summary=?, completed_at=datetime('now')
           WHERE job_id=?""",
        (found_count, billable_count, total_cost_cents, results_summary, job_id),
    )
    conn.commit()


def fail_search_job(job_id: str, error: str = ""):
    """Mark a search job as failed."""
    conn = _get_conn()
    conn.execute(
        "UPDATE search_jobs SET status='error', results_summary=?, completed_at=datetime('now') WHERE job_id=?",
        (error, job_id),
    )
    conn.commit()


def get_user_jobs(user_id: int, limit: int = 50) -> list:
    """Get user's past search jobs (non-expired)."""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT job_id, status, nonprofit_count, found_count, billable_count,
                  total_cost_cents, results_summary, created_at, completed_at
           FROM search_jobs
           WHERE user_id = ? AND expires_at > datetime('now')
           ORDER BY created_at DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def cleanup_expired_jobs():
    """Delete expired search jobs (older than 6 months)."""
    conn = _get_conn()
    conn.execute("DELETE FROM search_jobs WHERE expires_at <= datetime('now')")
    conn.commit()


# ─── Password Reset Tokens ───────────────────────────────────────────────────

def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Get user by email address."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, email, is_admin, is_trial FROM users WHERE email = ?",
        (email.lower(),),
    ).fetchone()
    if row:
        return {"id": row["id"], "email": row["email"],
                "is_admin": bool(row["is_admin"]), "is_trial": bool(row["is_trial"])}
    return None


def create_reset_token(user_id: int) -> str:
    """Generate a secure reset token with 15-minute expiry. Returns token string."""
    conn = _get_conn()
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?, ?, ?)",
        (user_id, token, expires_at),
    )
    conn.commit()
    return token


def validate_reset_token(token: str) -> Optional[int]:
    """Check token exists, not expired, not used. Returns user_id or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT user_id, expires_at, used FROM password_reset_tokens WHERE token = ?",
        (token,),
    ).fetchone()
    if not row or row["used"]:
        return None
    expires = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        return None
    return row["user_id"]


def consume_reset_token(token: str):
    """Mark a reset token as used."""
    conn = _get_conn()
    conn.execute("UPDATE password_reset_tokens SET used = 1 WHERE token = ?", (token,))
    conn.commit()
