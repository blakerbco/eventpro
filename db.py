"""
AUCTIONFINDER — PostgreSQL Database for Users, Wallets & Transactions

All user data stored in the same Railway PostgreSQL instance as the IRS data.
No more SQLite, no more data loss on deploys.
"""

import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.extras
from werkzeug.security import generate_password_hash, check_password_hash

DB_CONN_STRING = (
    os.environ.get("IRS_DB_CONNECTION")
    or os.environ.get("DATABASE_URL")
    or os.environ.get("DATABASE_PRIVATE_URL")
    or "postgresql://localhost/irs"
)

_src = "IRS_DB_CONNECTION" if os.environ.get("IRS_DB_CONNECTION") else \
       "DATABASE_URL" if os.environ.get("DATABASE_URL") else \
       "DATABASE_PRIVATE_URL" if os.environ.get("DATABASE_PRIVATE_URL") else "FALLBACK"
print(f"[DB] Using PostgreSQL: {_src} -> {DB_CONN_STRING[:30]}...")

# Thread-local storage for connections
_local = threading.local()


def _get_conn():
    """Return a per-thread PostgreSQL connection."""
    if not hasattr(_local, "conn") or _local.conn is None or _local.conn.closed:
        _local.conn = psycopg2.connect(DB_CONN_STRING)
        _local.conn.autocommit = False
    return _local.conn


def _fetchone(cur):
    """Fetch one row as dict or None."""
    row = cur.fetchone()
    if row is None:
        return None
    cols = [desc[0] for desc in cur.description]
    return dict(zip(cols, row))


def _fetchall(cur):
    """Fetch all rows as list of dicts."""
    rows = cur.fetchall()
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in rows]


def init_db():
    """Create tables if they don't exist and ensure admin user exists."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            phone TEXT DEFAULT '',
            company TEXT DEFAULT '',
            is_admin INTEGER DEFAULT 0,
            is_trial INTEGER DEFAULT 0,
            trial_expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            balance_cents INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            type TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            description TEXT,
            job_id TEXT,
            stripe_intent_id TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            token TEXT UNIQUE NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS search_jobs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            job_id TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'running',
            nonprofit_count INTEGER DEFAULT 0,
            found_count INTEGER DEFAULT 0,
            billable_count INTEGER DEFAULT 0,
            total_cost_cents INTEGER DEFAULT 0,
            results_summary TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP,
            expires_at TIMESTAMP DEFAULT (NOW() + INTERVAL '6 months')
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            subject TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            priority TEXT DEFAULT 'normal',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS ticket_messages (
            id SERIAL PRIMARY KEY,
            ticket_id INTEGER REFERENCES tickets(id),
            sender_id INTEGER REFERENCES users(id),
            is_admin INTEGER DEFAULT 0,
            message TEXT NOT NULL,
            read_by_user INTEGER DEFAULT 0,
            read_by_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS exclusive_leads (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            job_id TEXT NOT NULL,
            nonprofit_name TEXT NOT NULL,
            event_title TEXT NOT NULL,
            event_url TEXT NOT NULL,
            purchased_at TIMESTAMP DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS research_cache (
            id SERIAL PRIMARY KEY,
            cache_key TEXT UNIQUE NOT NULL,
            result_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'uncertain',
            event_title TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP NOT NULL DEFAULT (NOW() + INTERVAL '30 days')
        );

        CREATE TABLE IF NOT EXISTS result_files (
            id SERIAL PRIMARY KEY,
            job_id TEXT NOT NULL,
            format TEXT NOT NULL,
            content BYTEA NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(job_id, format)
        );

        CREATE TABLE IF NOT EXISTS job_results (
            id SERIAL PRIMARY KEY,
            job_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            result_json TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(job_id, domain)
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            key_hash TEXT NOT NULL,
            label TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT NOW(),
            is_active BOOLEAN DEFAULT TRUE
        );
    """)
    conn.commit()

    # Migration: add stripe_intent_id column if missing
    try:
        cur.execute(
            "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS stripe_intent_id TEXT"
        )
        conn.commit()
    except Exception:
        conn.rollback()

    # Migration: add expires_at to research_cache if missing
    try:
        cur.execute(
            "ALTER TABLE research_cache ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP NOT NULL DEFAULT (NOW() + INTERVAL '30 days')"
        )
        conn.commit()
    except Exception:
        conn.rollback()

    # Migration: add email_verified column to users
    try:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        conn.rollback()

    # Backfill: mark all existing users as verified so they don't get locked out
    try:
        cur.execute("UPDATE users SET email_verified = 1 WHERE email_verified = 0 OR email_verified IS NULL")
        backfilled = cur.rowcount
        conn.commit()
        if backfilled:
            print(f"[DB] Backfilled email_verified=1 for {backfilled} existing user(s)", flush=True)
    except Exception as e:
        print(f"[DB] Backfill email_verified error: {e}", flush=True)
        conn.rollback()

    # Create email_verification_tokens table
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_verification_tokens (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                token TEXT UNIQUE NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
    except Exception as e:
        print(f"[DB] Create email_verification_tokens error: {e}", flush=True)
        conn.rollback()

    # Create drip_emails_sent table for tracking drip campaign sends
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS drip_emails_sent (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                drip_key TEXT NOT NULL,
                sent_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, drip_key)
            )
        """)
        conn.commit()
    except Exception as e:
        print(f"[DB] Create drip_emails_sent error: {e}", flush=True)
        conn.rollback()

    # Migration: add input_domains and resumed_from columns to search_jobs
    try:
        cur.execute("ALTER TABLE search_jobs ADD COLUMN IF NOT EXISTS input_domains TEXT")
        cur.execute("ALTER TABLE search_jobs ADD COLUMN IF NOT EXISTS resumed_from TEXT")
        conn.commit()
    except Exception as e:
        print(f"[DB] Migration search_jobs columns error: {e}", flush=True)
        conn.rollback()

    # Remove stale fallback admin account if real admin is configured
    admin_email = os.environ.get("AUCTIONFINDER_ADMIN_EMAIL", "").strip().lower()
    admin_password = os.environ.get("AUCTIONFINDER_PASSWORD", "")
    if admin_email and admin_email != "admin@auctionfinder.local":
        cur.execute("SELECT id FROM users WHERE email = 'admin@auctionfinder.local'")
        stale = _fetchone(cur)
        if stale:
            cur.execute("DELETE FROM wallets WHERE user_id = %s", (stale["id"],))
            cur.execute("DELETE FROM transactions WHERE user_id = %s", (stale["id"],))
            cur.execute("DELETE FROM users WHERE id = %s", (stale["id"],))
            conn.commit()
            print("[DB CLEANUP] Removed stale admin@auctionfinder.local account")

    # Create or update admin user from env vars
    print(f"[ADMIN INIT] email env={'set' if admin_email else 'MISSING'} -> '{admin_email}'")
    print(f"[ADMIN INIT] password env={'set' if admin_password else 'MISSING'} (len={len(admin_password)})")
    if not admin_email or not admin_password:
        print("[ADMIN INIT] WARNING: AUCTIONFINDER_ADMIN_EMAIL or AUCTIONFINDER_PASSWORD not set, skipping admin creation")
    else:
        cur.execute("SELECT id FROM users WHERE email = %s", (admin_email,))
        row = _fetchone(cur)
        if not row:
            pw_hash = generate_password_hash(admin_password)
            cur.execute(
                "INSERT INTO users (email, password_hash, is_admin) VALUES (%s, %s, 1) RETURNING id",
                (admin_email, pw_hash),
            )
            new_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO wallets (user_id, balance_cents) VALUES (%s, 0)",
                (new_id,),
            )
            conn.commit()
            print(f"[ADMIN INIT] Created admin user: {admin_email} (id={new_id})")
        else:
            pw_hash = generate_password_hash(admin_password)
            cur.execute("UPDATE users SET password_hash = %s, is_admin = 1 WHERE id = %s",
                        (pw_hash, row["id"]))
            conn.commit()
            print(f"[ADMIN INIT] Updated admin user: {admin_email} (id={row['id']})")

    # Ensure blake@auctionintel.us is always admin
    cur.execute("SELECT id FROM users WHERE email = %s", ("blake@auctionintel.us",))
    blake = _fetchone(cur)
    if blake:
        cur.execute("UPDATE users SET is_admin = 1 WHERE id = %s", (blake["id"],))
        conn.commit()

    # Ensure all admin users are always email-verified
    cur.execute("UPDATE users SET email_verified = 1 WHERE is_admin = 1 AND (email_verified = 0 OR email_verified IS NULL)")
    conn.commit()

    cur.close()


TRIAL_PROMO_CODE = "26AUCTION26"
TRIAL_CREDIT_CENTS = 2000  # $20.00
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
    """Create a new user and wallet. Returns user_id."""
    conn = _get_conn()
    cur = conn.cursor()
    pw_hash = generate_password_hash(password)

    is_trial = 0
    trial_expires = None
    initial_balance = 0

    if promo_code.strip().upper() == TRIAL_PROMO_CODE:
        is_trial = 1
        trial_expires = datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
        initial_balance = TRIAL_CREDIT_CENTS

    cur.execute(
        "INSERT INTO users (email, password_hash, phone, company, is_trial, trial_expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (email, pw_hash, phone, company, is_trial, trial_expires),
    )
    user_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO wallets (user_id, balance_cents) VALUES (%s, %s)",
        (user_id, initial_balance),
    )
    if initial_balance > 0:
        cur.execute(
            "INSERT INTO transactions (user_id, type, amount_cents, description) "
            "VALUES (%s, 'topup', %s, 'Free trial credit (promo: 26AUCTION26)')",
            (user_id, initial_balance),
        )
    conn.commit()
    cur.close()
    return user_id


def authenticate(email: str, password: str) -> Optional[Dict[str, Any]]:
    """Verify credentials. Returns user dict or None."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, email, password_hash, is_admin, is_trial, email_verified FROM users WHERE email = %s",
        (email,),
    )
    row = _fetchone(cur)
    cur.close()
    if row and check_password_hash(row["password_hash"], password):
        return {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"]),
                "is_trial": bool(row["is_trial"]),
                "email_verified": bool(row.get("email_verified"))}
    return None


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Get user by id."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, email, is_admin, is_trial, email_verified FROM users WHERE id = %s",
        (user_id,),
    )
    row = _fetchone(cur)
    cur.close()
    if row:
        return {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"]),
                "is_trial": bool(row["is_trial"]),
                "email_verified": bool(row.get("email_verified"))}
    return None


def get_user_full(user_id: int) -> Optional[Dict[str, Any]]:
    """Get user with all fields including created_at."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, email, is_admin, created_at FROM users WHERE id = %s",
        (user_id,),
    )
    row = _fetchone(cur)
    cur.close()
    if row:
        return {"id": row["id"], "email": row["email"], "is_admin": bool(row["is_admin"]),
                "created_at": str(row["created_at"]) if row["created_at"] else None}
    return None


def update_password(user_id: int, new_password: str):
    """Update user's password."""
    conn = _get_conn()
    cur = conn.cursor()
    pw_hash = generate_password_hash(new_password)
    cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (pw_hash, user_id))
    conn.commit()
    cur.close()


def get_spending_summary(user_id: int) -> Dict[str, Any]:
    """Get billing summary for a user."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(ABS(amount_cents)), 0) FROM transactions WHERE user_id = %s AND type = 'research_fee'",
        (user_id,),
    )
    research = cur.fetchone()[0]
    cur.execute(
        "SELECT COALESCE(SUM(ABS(amount_cents)), 0) FROM transactions WHERE user_id = %s AND type = 'lead_fee'",
        (user_id,),
    )
    leads = cur.fetchone()[0]
    cur.execute(
        "SELECT COALESCE(SUM(ABS(amount_cents)), 0) FROM transactions WHERE user_id = %s AND type = 'exclusive_lead'",
        (user_id,),
    )
    exclusive = cur.fetchone()[0]
    cur.execute(
        "SELECT COALESCE(SUM(ABS(amount_cents)), 0) FROM transactions WHERE user_id = %s AND type = 'lead_refund'",
        (user_id,),
    )
    refunds = cur.fetchone()[0]
    cur.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE user_id = %s AND type = 'topup'",
        (user_id,),
    )
    topups = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(DISTINCT job_id) FROM transactions WHERE user_id = %s AND job_id IS NOT NULL",
        (user_id,),
    )
    job_count = cur.fetchone()[0]
    cur.close()
    return {
        "research_fees": research,
        "lead_fees": leads,
        "exclusive_fees": exclusive,
        "refunds": refunds,
        "total_topups": topups,
        "total_spent": research + leads + exclusive - refunds,
        "job_count": job_count,
    }


def get_job_breakdowns(user_id: int, limit: int = 20) -> list:
    """Get per-job billing breakdown."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT job_id, MIN(created_at) as started,
           SUM(CASE WHEN type='research_fee' THEN ABS(amount_cents) ELSE 0 END) as research_cost,
           SUM(CASE WHEN type='lead_fee' THEN ABS(amount_cents) ELSE 0 END) as lead_cost,
           SUM(CASE WHEN type='research_fee' THEN 1 ELSE 0 END) as nonprofits_searched,
           SUM(CASE WHEN type='lead_fee' THEN 1 ELSE 0 END) as leads_found
        FROM transactions
        WHERE user_id = %s AND job_id IS NOT NULL
        GROUP BY job_id
        ORDER BY MIN(created_at) DESC
        LIMIT %s""",
        (user_id, limit),
    )
    result = _fetchall(cur)
    cur.close()
    # Convert datetime objects to strings
    for r in result:
        if r.get("started") and not isinstance(r["started"], str):
            r["started"] = str(r["started"])
    return result


def get_balance(user_id: int) -> int:
    """Return wallet balance in cents."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT balance_cents FROM wallets WHERE user_id = %s",
        (user_id,),
    )
    row = _fetchone(cur)
    cur.close()
    return row["balance_cents"] if row else 0


def add_funds(user_id: int, amount_cents: int, description: str = "Stripe top-up",
              stripe_intent_id: str = None) -> bool:
    """Credit wallet from a Stripe payment. Returns False if duplicate intent."""
    conn = _get_conn()
    cur = conn.cursor()
    if stripe_intent_id:
        cur.execute(
            "SELECT id FROM transactions WHERE stripe_intent_id = %s",
            (stripe_intent_id,),
        )
        if _fetchone(cur):
            cur.close()
            return False
    cur.execute(
        "UPDATE wallets SET balance_cents = balance_cents + %s WHERE user_id = %s",
        (amount_cents, user_id),
    )
    cur.execute(
        "INSERT INTO transactions (user_id, type, amount_cents, description, stripe_intent_id) "
        "VALUES (%s, 'topup', %s, %s, %s)",
        (user_id, amount_cents, description, stripe_intent_id),
    )
    conn.commit()
    cur.close()
    return True


def charge_research_fee(user_id: int, count: int, job_id: str, fee_cents_each: int = 4):
    """Deduct research fee (per-nonprofit). Returns total charged."""
    total = count * fee_cents_each
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE wallets SET balance_cents = balance_cents - %s WHERE user_id = %s",
        (total, user_id),
    )
    cur.execute(
        "INSERT INTO transactions (user_id, type, amount_cents, description, job_id) "
        "VALUES (%s, 'research_fee', %s, %s, %s)",
        (user_id, -total, f"Research fee: {count} nonprofit(s) @ ${fee_cents_each/100:.2f}", job_id),
    )
    conn.commit()
    cur.close()
    return total


def charge_lead_fee(user_id: int, tier: str, price_cents: int, job_id: str, nonprofit_name: str = ""):
    """Deduct lead fee for a billable result. Returns price_cents charged."""
    if price_cents <= 0:
        return 0
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE wallets SET balance_cents = balance_cents - %s WHERE user_id = %s",
        (price_cents, user_id),
    )
    cur.execute(
        "INSERT INTO transactions (user_id, type, amount_cents, description, job_id) "
        "VALUES (%s, 'lead_fee', %s, %s, %s)",
        (user_id, -price_cents, f"Lead fee ({tier}): {nonprofit_name}", job_id),
    )
    conn.commit()
    cur.close()
    return price_cents


def has_sufficient_balance(user_id: int, estimated_cost_cents: int) -> bool:
    """Check if user has enough balance for the estimated cost."""
    return get_balance(user_id) >= estimated_cost_cents


def get_transactions(user_id: int, limit: int = 50) -> list:
    """Return recent transactions for a user."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, type, amount_cents, description, job_id, created_at "
        "FROM transactions WHERE user_id = %s ORDER BY id DESC LIMIT %s",
        (user_id, limit),
    )
    result = _fetchall(cur)
    cur.close()
    for r in result:
        if r.get("created_at") and not isinstance(r["created_at"], str):
            r["created_at"] = str(r["created_at"])
    return result


def get_research_fee_cents(total_nonprofits: int) -> int:
    """Tiered research fee per nonprofit."""
    if total_nonprofits <= 10000:
        return 4    # $0.04
    elif total_nonprofits <= 50000:
        return 3    # $0.03
    else:
        return 2    # $0.02


# ─── Search Job Persistence ──────────────────────────────────────────────────

def create_search_job(user_id: int, job_id: str, nonprofit_count: int):
    """Record a new search job."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO search_jobs (user_id, job_id, status, nonprofit_count) VALUES (%s, %s, 'running', %s)",
        (user_id, job_id, nonprofit_count),
    )
    conn.commit()
    cur.close()


def complete_search_job(job_id: str, found_count: int, billable_count: int,
                        total_cost_cents: int, results_summary: str = ""):
    """Mark a search job as complete with results."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """UPDATE search_jobs SET status='complete', found_count=%s, billable_count=%s,
           total_cost_cents=%s, results_summary=%s, completed_at=NOW()
           WHERE job_id=%s""",
        (found_count, billable_count, total_cost_cents, results_summary, job_id),
    )
    conn.commit()
    cur.close()


def save_job_checkpoint(job_id: str, results_json: str):
    """Save partial results as a checkpoint to the DB (survives connection drops)."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE search_jobs SET results_summary = %s WHERE job_id = %s",
            (results_json, job_id),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[CHECKPOINT ERROR] {job_id}: {e}", flush=True)
    finally:
        cur.close()


def fail_search_job(job_id: str, error: str = ""):
    """Mark a search job as failed."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE search_jobs SET status='error', results_summary=%s, completed_at=NOW() WHERE job_id=%s",
        (error, job_id),
    )
    conn.commit()
    cur.close()


def get_user_jobs(user_id: int, limit: int = 50) -> list:
    """Get user's past search jobs (non-expired)."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT job_id, status, nonprofit_count, found_count, billable_count,
                  total_cost_cents, results_summary, created_at, completed_at
           FROM search_jobs
           WHERE user_id = %s AND expires_at > NOW()
           ORDER BY created_at DESC LIMIT %s""",
        (user_id, limit),
    )
    result = _fetchall(cur)
    cur.close()
    for r in result:
        for k in ("created_at", "completed_at"):
            if r.get(k) and not isinstance(r[k], str):
                r[k] = str(r[k])
    return result


def cleanup_expired_jobs():
    """Delete expired search jobs (older than 6 months)."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM search_jobs WHERE expires_at <= NOW()")
    conn.commit()
    cur.close()


def cleanup_stale_running_jobs():
    """Mark ALL 'running' jobs as failed on server startup.
    If the server just started, no jobs can actually be running — they're all zombies."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """UPDATE search_jobs SET status='error', results_summary='Server restarted — job interrupted',
           completed_at=NOW()
           WHERE status='running'""",
    )
    cleaned = cur.rowcount
    conn.commit()
    cur.close()
    if cleaned:
        print(f"[DB] Cleaned up {cleaned} stale running job(s)", flush=True)
    return cleaned


# ─── Per-Result Checkpoints (job_results) ─────────────────────────────────────

def save_single_result(job_id: str, domain: str, result_dict: Dict[str, Any]):
    """Save one research result row immediately after processing."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO job_results (job_id, domain, result_json)
               VALUES (%s, %s, %s)
               ON CONFLICT (job_id, domain) DO UPDATE SET result_json = EXCLUDED.result_json""",
            (job_id, domain, _json.dumps(result_dict, ensure_ascii=False)),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SAVE_RESULT ERROR] {job_id}/{domain}: {e}", flush=True)
    finally:
        cur.close()


def get_completed_domains(job_id: str) -> set:
    """Return set of domains already processed for a job."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT domain FROM job_results WHERE job_id = %s", (job_id,))
    rows = _fetchall(cur)
    cur.close()
    return {r["domain"] for r in rows}


def get_completed_results(job_id: str) -> list:
    """Return all result dicts for a job."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT domain, result_json FROM job_results WHERE job_id = %s ORDER BY id", (job_id,))
    rows = _fetchall(cur)
    cur.close()
    results = []
    for r in rows:
        try:
            results.append(_json.loads(r["result_json"]))
        except (_json.JSONDecodeError, TypeError) as e:
            print(f"[GET_RESULTS WARN] {job_id}/{r['domain']}: {e}", flush=True)
    return results


def save_job_input_domains(job_id: str, domains: list):
    """Store the full original domain list as JSON on the search_jobs row."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE search_jobs SET input_domains = %s WHERE job_id = %s",
            (_json.dumps(domains, ensure_ascii=False), job_id),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SAVE_INPUT ERROR] {job_id}: {e}", flush=True)
    finally:
        cur.close()


def get_job_input_domains(job_id: str) -> Optional[list]:
    """Load original domain list from DB. Returns list or None."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT input_domains FROM search_jobs WHERE job_id = %s", (job_id,))
    row = _fetchone(cur)
    cur.close()
    if not row or not row.get("input_domains"):
        return None
    try:
        return _json.loads(row["input_domains"])
    except (_json.JSONDecodeError, TypeError):
        return None


def get_search_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Get a single search job by job_id."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT job_id, user_id, status, nonprofit_count, found_count, billable_count,
                  total_cost_cents, results_summary, input_domains, resumed_from,
                  created_at, completed_at
           FROM search_jobs WHERE job_id = %s""",
        (job_id,),
    )
    row = _fetchone(cur)
    cur.close()
    if row:
        for k in ("created_at", "completed_at"):
            if row.get(k) and not isinstance(row[k], str):
                row[k] = str(row[k])
    return row


# ─── API Key Management ──────────────────────────────────────────────────────

import hashlib as _hashlib


def create_api_key(user_id: int, label: str = "") -> str:
    """Generate an API key, store SHA-256 hash, return plaintext (shown once)."""
    plaintext = "ak_" + secrets.token_hex(32)
    key_hash = _hashlib.sha256(plaintext.encode()).hexdigest()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO api_keys (user_id, key_hash, label) VALUES (%s, %s, %s)",
        (user_id, key_hash, label),
    )
    conn.commit()
    cur.close()
    return plaintext


def validate_api_key(key: str) -> Optional[int]:
    """Validate an API key. Returns user_id or None."""
    if not key or not key.startswith("ak_"):
        return None
    key_hash = _hashlib.sha256(key.encode()).hexdigest()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id FROM api_keys WHERE key_hash = %s AND is_active = TRUE",
        (key_hash,),
    )
    row = _fetchone(cur)
    cur.close()
    return row["user_id"] if row else None


def revoke_api_key(key_id: int, user_id: int) -> bool:
    """Revoke an API key. Returns True if found and revoked."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE api_keys SET is_active = FALSE WHERE id = %s AND user_id = %s",
        (key_id, user_id),
    )
    affected = cur.rowcount
    conn.commit()
    cur.close()
    return affected > 0


def get_user_api_keys(user_id: int) -> list:
    """Get all API keys for a user (hash masked, for display)."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, key_hash, label, is_active, created_at FROM api_keys WHERE user_id = %s ORDER BY id DESC",
        (user_id,),
    )
    rows = _fetchall(cur)
    cur.close()
    for r in rows:
        r["key_hash"] = r["key_hash"][:8] + "..."
        if r.get("created_at") and not isinstance(r["created_at"], str):
            r["created_at"] = str(r["created_at"])
    return rows


# ─── Result File Storage (persists across deploys) ───────────────────────────

def save_result_file(job_id: str, fmt: str, content: bytes):
    """Store a result file (csv/json/xlsx) in the database."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO result_files (job_id, format, content)
           VALUES (%s, %s, %s)
           ON CONFLICT (job_id, format) DO UPDATE SET content = EXCLUDED.content""",
        (job_id, fmt, content),
    )
    conn.commit()
    cur.close()


def get_result_file(job_id: str, fmt: str) -> Optional[bytes]:
    """Retrieve a result file from the database. Returns bytes or None."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT content FROM result_files WHERE job_id = %s AND format = %s",
        (job_id, fmt),
    )
    row = _fetchone(cur)
    cur.close()
    if row:
        content = row["content"]
        # psycopg2 returns memoryview for BYTEA; convert to bytes
        if isinstance(content, memoryview):
            return bytes(content)
        return content
    return None


# ─── Password Reset Tokens ───────────────────────────────────────────────────

def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Get user by email address."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, email, is_admin, is_trial FROM users WHERE email = %s",
        (email.lower(),),
    )
    row = _fetchone(cur)
    cur.close()
    if row:
        return {"id": row["id"], "email": row["email"],
                "is_admin": bool(row["is_admin"]), "is_trial": bool(row["is_trial"])}
    return None


def create_reset_token(user_id: int) -> str:
    """Generate a secure reset token with 15-minute expiry. Returns token string."""
    conn = _get_conn()
    cur = conn.cursor()
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    cur.execute(
        "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (%s, %s, %s)",
        (user_id, token, expires_at),
    )
    conn.commit()
    cur.close()
    return token


def validate_reset_token(token: str) -> Optional[int]:
    """Check token exists, not expired, not used. Returns user_id or None."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, expires_at, used FROM password_reset_tokens WHERE token = %s",
        (token,),
    )
    row = _fetchone(cur)
    cur.close()
    if not row or row["used"]:
        return None
    expires = row["expires_at"]
    if isinstance(expires, str):
        expires = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    elif expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        return None
    return row["user_id"]


def consume_reset_token(token: str):
    """Mark a reset token as used."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE password_reset_tokens SET used = 1 WHERE token = %s", (token,))
    conn.commit()
    cur.close()


# ─── Email Verification Tokens ───────────────────────────────────────────────

def create_verification_token(user_id: int) -> str:
    """Generate a secure email verification token with 24-hour expiry. Returns token string."""
    conn = _get_conn()
    cur = conn.cursor()
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    cur.execute(
        "INSERT INTO email_verification_tokens (user_id, token, expires_at) VALUES (%s, %s, %s)",
        (user_id, token, expires_at),
    )
    conn.commit()
    cur.close()
    return token


def validate_verification_token(token: str) -> Optional[int]:
    """Check token exists, not expired, not used. Returns user_id or None."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, expires_at, used FROM email_verification_tokens WHERE token = %s",
        (token,),
    )
    row = _fetchone(cur)
    cur.close()
    if not row or row["used"]:
        return None
    expires = row["expires_at"]
    if isinstance(expires, str):
        expires = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    elif expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        return None
    return row["user_id"]


def consume_verification_token(token: str):
    """Mark verification token as used and set user's email_verified = 1."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id FROM email_verification_tokens WHERE token = %s AND used = 0",
        (token,),
    )
    row = _fetchone(cur)
    if row:
        cur.execute("UPDATE email_verification_tokens SET used = 1 WHERE token = %s", (token,))
        cur.execute("UPDATE users SET email_verified = 1 WHERE id = %s", (row["user_id"],))
        conn.commit()
    cur.close()


# ─── Support Tickets ──────────────────────────────────────────────────────────

_VALID_STATUSES = {"open", "pending", "urgent", "resolved"}
_VALID_PRIORITIES = {"low", "normal", "high", "urgent"}


def create_ticket(user_id: int, subject: str, message: str, priority: str = "normal") -> int:
    """Create a ticket with its first message. Returns ticket_id."""
    if priority not in _VALID_PRIORITIES:
        priority = "normal"
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tickets (user_id, subject, priority) VALUES (%s, %s, %s) RETURNING id",
        (user_id, subject, priority),
    )
    ticket_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO ticket_messages (ticket_id, sender_id, is_admin, message, read_by_user, read_by_admin) "
        "VALUES (%s, %s, 0, %s, 1, 0)",
        (ticket_id, user_id, message),
    )
    conn.commit()
    cur.close()
    return ticket_id


def get_ticket(ticket_id: int) -> Optional[Dict[str, Any]]:
    """Get a single ticket by id."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT t.*, u.email as user_email FROM tickets t JOIN users u ON t.user_id = u.id WHERE t.id = %s",
        (ticket_id,),
    )
    row = _fetchone(cur)
    cur.close()
    if row:
        for k in ("created_at", "updated_at"):
            if row.get(k) and not isinstance(row[k], str):
                row[k] = str(row[k])
    return row


def get_tickets_for_user(user_id: int) -> list:
    """Get all tickets for a specific user."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM ticket_messages tm WHERE tm.ticket_id = t.id AND tm.read_by_user = 0 AND tm.is_admin = 1) as unread "
        "FROM tickets t WHERE t.user_id = %s ORDER BY t.updated_at DESC",
        (user_id,),
    )
    result = _fetchall(cur)
    cur.close()
    for r in result:
        for k in ("created_at", "updated_at"):
            if r.get(k) and not isinstance(r[k], str):
                r[k] = str(r[k])
    return result


def get_all_tickets() -> list:
    """Get all tickets (admin view)."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT t.*, u.email as user_email, "
        "(SELECT COUNT(*) FROM ticket_messages tm WHERE tm.ticket_id = t.id AND tm.read_by_admin = 0 AND tm.is_admin = 0) as unread "
        "FROM tickets t JOIN users u ON t.user_id = u.id ORDER BY "
        "CASE t.status WHEN 'urgent' THEN 0 WHEN 'open' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END, t.updated_at DESC",
    )
    result = _fetchall(cur)
    cur.close()
    for r in result:
        for k in ("created_at", "updated_at"):
            if r.get(k) and not isinstance(r[k], str):
                r[k] = str(r[k])
    return result


def get_ticket_messages(ticket_id: int) -> list:
    """Get all messages for a ticket."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT tm.*, u.email as sender_email FROM ticket_messages tm "
        "JOIN users u ON tm.sender_id = u.id WHERE tm.ticket_id = %s ORDER BY tm.created_at ASC",
        (ticket_id,),
    )
    result = _fetchall(cur)
    cur.close()
    for r in result:
        if r.get("created_at") and not isinstance(r["created_at"], str):
            r["created_at"] = str(r["created_at"])
    return result


def add_ticket_message(ticket_id: int, sender_id: int, message: str, is_admin: bool = False) -> int:
    """Add a reply to a ticket. Returns message_id."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ticket_messages (ticket_id, sender_id, is_admin, message, read_by_user, read_by_admin) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (ticket_id, sender_id, 1 if is_admin else 0, message,
         1 if not is_admin else 0, 1 if is_admin else 0),
    )
    msg_id = cur.fetchone()[0]
    cur.execute("UPDATE tickets SET updated_at = NOW() WHERE id = %s", (ticket_id,))
    conn.commit()
    cur.close()
    return msg_id


def update_ticket_status(ticket_id: int, status: str):
    """Update ticket status. Validates against whitelist."""
    if status not in _VALID_STATUSES:
        return
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE tickets SET status = %s, updated_at = NOW() WHERE id = %s",
        (status, ticket_id),
    )
    conn.commit()
    cur.close()


def mark_messages_read_by_user(ticket_id: int):
    """Mark all admin messages as read by the user."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE ticket_messages SET read_by_user = 1 WHERE ticket_id = %s AND is_admin = 1",
        (ticket_id,),
    )
    conn.commit()
    cur.close()


def mark_messages_read_by_admin(ticket_id: int):
    """Mark all user messages as read by admin."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE ticket_messages SET read_by_admin = 1 WHERE ticket_id = %s AND is_admin = 0",
        (ticket_id,),
    )
    conn.commit()
    cur.close()


def get_unread_ticket_count(user_id: int, is_admin: bool = False) -> int:
    """Get count of tickets with unread messages for nav badge."""
    conn = _get_conn()
    cur = conn.cursor()
    if is_admin:
        cur.execute(
            "SELECT COUNT(DISTINCT t.id) FROM tickets t "
            "JOIN ticket_messages tm ON tm.ticket_id = t.id "
            "WHERE tm.is_admin = 0 AND tm.read_by_admin = 0",
        )
    else:
        cur.execute(
            "SELECT COUNT(DISTINCT t.id) FROM tickets t "
            "JOIN ticket_messages tm ON tm.ticket_id = t.id "
            "WHERE t.user_id = %s AND tm.is_admin = 1 AND tm.read_by_user = 0",
            (user_id,),
        )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else 0


# ─── Exclusive Leads ──────────────────────────────────────────────────────────

EXCLUSIVE_LEAD_PRICE_CENTS = 250  # $2.50 flat


def purchase_exclusive_lead(user_id: int, job_id: str, nonprofit_name: str,
                            event_title: str, event_url: str) -> bool:
    """Purchase exclusivity for a specific event lead. Returns True on success.
    Refunds the original lead_fee tier charge so lock price replaces (not adds to) it."""
    conn = _get_conn()
    cur = conn.cursor()
    # Check if already exclusive
    cur.execute(
        "SELECT id FROM exclusive_leads WHERE event_url = %s AND event_title = %s",
        (event_url, event_title),
    )
    if _fetchone(cur):
        cur.close()
        return False

    # Find and refund the original lead_fee for this nonprofit in this job
    refund_cents = 0
    cur.execute(
        "SELECT id, amount_cents FROM transactions "
        "WHERE user_id = %s AND job_id = %s AND type = 'lead_fee' "
        "AND description LIKE %s LIMIT 1",
        (user_id, job_id, f"%{nonprofit_name[:30]}%"),
    )
    original_fee = _fetchone(cur)
    if original_fee:
        refund_cents = abs(original_fee["amount_cents"])
        print(f"[LOCK] Refunding original lead fee: ${refund_cents/100:.2f} for {nonprofit_name}", flush=True)

    # Net charge = lock price minus refund
    net_charge = EXCLUSIVE_LEAD_PRICE_CENTS - refund_cents

    # Check balance (net_charge could be negative if tier fee > lock price, but that shouldn't happen)
    if net_charge > 0:
        cur.execute("SELECT balance_cents FROM wallets WHERE user_id = %s", (user_id,))
        bal = _fetchone(cur)
        if not bal or bal["balance_cents"] < net_charge:
            cur.close()
            return False

    # Refund original tier fee
    if refund_cents > 0:
        cur.execute("UPDATE wallets SET balance_cents = balance_cents + %s WHERE user_id = %s",
                    (refund_cents, user_id))
        cur.execute(
            "INSERT INTO transactions (user_id, type, amount_cents, description, job_id) "
            "VALUES (%s, 'lead_refund', %s, %s, %s)",
            (user_id, refund_cents,
             f"Tier fee refund (lock replaces): {nonprofit_name[:40]}", job_id),
        )

    # Charge lock price
    cur.execute("UPDATE wallets SET balance_cents = balance_cents - %s WHERE user_id = %s",
                (EXCLUSIVE_LEAD_PRICE_CENTS, user_id))
    cur.execute(
        "INSERT INTO transactions (user_id, type, amount_cents, description, job_id) "
        "VALUES (%s, 'exclusive_lead', %s, %s, %s)",
        (user_id, -EXCLUSIVE_LEAD_PRICE_CENTS,
         f"Priority lead lock: {event_title[:60]} ({nonprofit_name[:40]})", job_id),
    )
    # Record exclusivity
    cur.execute(
        "INSERT INTO exclusive_leads (user_id, job_id, nonprofit_name, event_title, event_url) "
        "VALUES (%s, %s, %s, %s, %s)",
        (user_id, job_id, nonprofit_name, event_title, event_url),
    )
    conn.commit()
    cur.close()
    return True


def is_lead_exclusive(event_url: str, event_title: str) -> Optional[int]:
    """Check if an event lead is exclusive. Returns owner user_id or None."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id FROM exclusive_leads WHERE event_url = %s AND event_title = %s",
        (event_url, event_title),
    )
    row = _fetchone(cur)
    cur.close()
    return row["user_id"] if row else None


def get_user_exclusive_leads(user_id: int) -> list:
    """Get all exclusive leads for a user."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM exclusive_leads WHERE user_id = %s ORDER BY purchased_at DESC",
        (user_id,),
    )
    result = _fetchall(cur)
    cur.close()
    return result


# ─── Research Cache ──────────────────────────────────────────────────────────

import json as _json
import re as _re


def _cache_key(nonprofit: str) -> str:
    """Normalize nonprofit name/domain to a stable cache key."""
    return nonprofit.strip().lower()


def _parse_event_date(date_str: str) -> Optional[datetime]:
    """Try to parse M/D/YYYY event date. Returns datetime or None."""
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip()
    # Match M/D/YYYY or MM/DD/YYYY
    m = _re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", date_str)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _compute_expiry(result: Dict[str, Any]) -> datetime:
    """Compute cache expiry based on result status and event date.
    - found/3rdpty_found with event_date: expires day after event_date
    - not_found: 30 days
    - error/uncertain: 7 days
    """
    status = result.get("status", "uncertain")
    now = datetime.now(timezone.utc)

    if status in ("found", "3rdpty_found"):
        event_dt = _parse_event_date(result.get("event_date", ""))
        if event_dt:
            # Expire the day after the event
            return event_dt + timedelta(days=1)
        # Found but no parseable date — keep 90 days
        return now + timedelta(days=90)

    if status == "not_found":
        return now + timedelta(days=30)

    # error — retry in 7 days
    if status == "error":
        return now + timedelta(days=7)

    # uncertain — retry in 1 hour (usually means API issue, not real data)
    return now + timedelta(hours=1)


def flush_uncertain_cache():
    """Delete all 'uncertain' cache entries. Called on startup to clear bad API results."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM research_cache WHERE status = 'uncertain'")
    flushed = cur.rowcount
    conn.commit()
    cur.close()
    if flushed:
        print(f"[DB] Flushed {flushed} uncertain cache entries", flush=True)
    return flushed


def cache_get(nonprofit: str) -> Optional[Dict[str, Any]]:
    """Look up a cached research result. Returns the result dict or None if missing/expired."""
    key = _cache_key(nonprofit)
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT result_json, expires_at FROM research_cache WHERE cache_key = %s",
        (key,),
    )
    row = _fetchone(cur)
    cur.close()
    if not row:
        return None

    # Check expiry
    expires = row["expires_at"]
    if expires:
        if isinstance(expires, str):
            try:
                expires = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                expires = None
        elif expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires and datetime.now(timezone.utc) > expires:
            # Expired — delete and return None
            conn2 = _get_conn()
            cur2 = conn2.cursor()
            cur2.execute("DELETE FROM research_cache WHERE cache_key = %s", (key,))
            conn2.commit()
            cur2.close()
            return None

    try:
        return _json.loads(row["result_json"])
    except (_json.JSONDecodeError, TypeError):
        return None


def cache_put(nonprofit: str, result: Dict[str, Any]):
    """Save a research result to the cache. Overwrites any existing entry.
    Expiry is computed automatically based on status and event_date."""
    key = _cache_key(nonprofit)
    result_json = _json.dumps(result, ensure_ascii=False)
    status = result.get("status", "uncertain")
    event_title = result.get("event_title", "")
    expires_at = _compute_expiry(result)
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO research_cache (cache_key, result_json, status, event_title, expires_at)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (cache_key) DO UPDATE SET
               result_json = EXCLUDED.result_json,
               status = EXCLUDED.status,
               event_title = EXCLUDED.event_title,
               expires_at = EXCLUDED.expires_at,
               created_at = NOW()""",
        (key, result_json, status, event_title, expires_at),
    )
    conn.commit()
    cur.close()


# ─── Drip Campaign ───────────────────────────────────────────────────────────

def get_trial_users_for_drip():
    """Get trial users eligible for drip emails with their signup age in days."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.email, u.created_at,
               EXTRACT(EPOCH FROM (NOW() - u.created_at)) / 86400 AS days_since_signup,
               (SELECT COUNT(*) FROM search_jobs WHERE user_id = u.id) AS search_count
        FROM users u
        WHERE u.is_trial = 1
          AND u.created_at > NOW() - INTERVAL '10 days'
        ORDER BY u.created_at
    """)
    rows = _fetchall(cur)
    cur.close()
    return rows


def get_drips_sent(user_id: int):
    """Get set of drip_key values already sent to this user."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT drip_key FROM drip_emails_sent WHERE user_id = %s", (user_id,))
    rows = _fetchall(cur)
    cur.close()
    return {r["drip_key"] for r in rows}


def record_drip_sent(user_id: int, drip_key: str):
    """Record that a drip email was sent. Idempotent via UNIQUE constraint."""
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO drip_emails_sent (user_id, drip_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user_id, drip_key),
        )
        conn.commit()
    except Exception as e:
        print(f"[DRIP] Error recording drip '{drip_key}' for user {user_id}: {e}", flush=True)
        conn.rollback()
    cur.close()
