"""Shared database layer for the web app (app.py) and the camera script
(attendance_taker.py). Uses the same attendance.db file as the original
project, and upgrades an old database in place (no data is lost)."""
import os
import sqlite3
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db")
FACES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "data_faces_from_camera")

DEFAULT_SETTINGS = {
    "late_after": "09:30",          # check-ins after this time are marked Late
    "institution": "My College",    # shown in navbar + reports
}


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    """Create tables and upgrade the original (name, time, date) table."""
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL UNIQUE,      -- must match the face-folder name
        roll_no     TEXT,
        email       TEXT,
        department  TEXT,
        active      INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS staff (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'teacher'   -- 'admin' or 'teacher'
    );
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS attendance (
        name TEXT, time TEXT, date DATE, UNIQUE(name, date)
    );
    """)
    # Upgrade the original table with the new columns
    cols = _columns(conn, "attendance")
    for col, ddl in [("user_id", "INTEGER"),
                     ("status", "TEXT DEFAULT 'Present'"),
                     ("method", "TEXT DEFAULT 'face'"),
                     ("confidence", "REAL")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE attendance ADD COLUMN {col} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_att_date ON attendance(date)")

    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))

    # Old records: create a user for every name already in attendance
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""INSERT OR IGNORE INTO users(name, created_at)
                    SELECT DISTINCT name, ? FROM attendance WHERE name IS NOT NULL""", (now,))
    conn.execute("""UPDATE attendance SET user_id =
                    (SELECT id FROM users WHERE users.name = attendance.name)
                    WHERE user_id IS NULL""")

    # First run: create a default admin (CHANGE THE PASSWORD after logging in)
    if conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 0:
        conn.execute("INSERT INTO staff(username, password_hash, role) VALUES (?,?,?)",
                     ("admin", generate_password_hash("admin123"), "admin"))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- settings
def get_settings():
    conn = get_conn()
    s = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}
    conn.close()
    return {**DEFAULT_SETTINGS, **s}


def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------- logging
def log_attendance(name, method="face", confidence=None, when=None, conn=None):
    """Mark `name` present for today. Returns (inserted: bool, status: str).
    Unknown names are auto-registered as users. Inactive users are ignored."""
    own = conn is None
    conn = conn or get_conn()
    when = when or datetime.now()
    date, tm = when.strftime("%Y-%m-%d"), when.strftime("%H:%M:%S")
    try:
        user = conn.execute("SELECT id, active FROM users WHERE name=?", (name,)).fetchone()
        if user is None:
            cur = conn.execute("INSERT INTO users(name, created_at) VALUES(?,?)",
                               (name, when.isoformat(timespec="seconds")))
            user_id = cur.lastrowid
        else:
            if not user["active"]:
                return False, "inactive"
            user_id = user["id"]
        late_after = get_settings()["late_after"]
        status = "Late" if tm[:5] > late_after else "Present"
        cur = conn.execute(
            "INSERT OR IGNORE INTO attendance(name,time,date,user_id,status,method,confidence)"
            " VALUES(?,?,?,?,?,?,?)", (name, tm, date, user_id, status, method, confidence))
        conn.commit()
        return cur.rowcount == 1, status
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------- analytics
def today():
    return datetime.now().strftime("%Y-%m-%d")


def day_summary(date):
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    rows = conn.execute("""SELECT status, COUNT(*) c FROM attendance
                           WHERE date=? GROUP BY status""", (date,)).fetchall()
    conn.close()
    by = {r["status"] or "Present": r["c"] for r in rows}
    present, late = by.get("Present", 0), by.get("Late", 0)
    marked = present + late
    return {"total": total, "present": present, "late": late, "marked": marked,
            "absent": max(total - marked, 0),
            "rate": round(100 * marked / total, 1) if total else 0.0}


def trend(days=14):
    end = datetime.now().date()
    start = end - timedelta(days=days - 1)
    conn = get_conn()
    rows = conn.execute("SELECT date, COUNT(*) c FROM attendance WHERE date>=? GROUP BY date",
                        (start.isoformat(),)).fetchall()
    conn.close()
    got = {r["date"]: r["c"] for r in rows}
    labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    return {"labels": labels, "values": [got.get(d, 0) for d in labels]}


def hourly(date):
    conn = get_conn()
    rows = conn.execute("""SELECT substr(time,1,2) h, COUNT(*) c FROM attendance
                           WHERE date=? GROUP BY h""", (date,)).fetchall()
    conn.close()
    got = {r["h"]: r["c"] for r in rows}
    hours = [f"{h:02d}" for h in range(6, 20)]
    return {"labels": [f"{h}:00" for h in hours], "values": [got.get(h, 0) for h in hours]}


def recent(limit=10):
    conn = get_conn()
    rows = conn.execute("""SELECT name, time, date, status, method FROM attendance
                           ORDER BY date DESC, time DESC LIMIT ?""", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def student_report(start, end):
    """Per-student totals between two dates (inclusive). Working days = days
    on which at least one student was marked."""
    conn = get_conn()
    working = conn.execute("SELECT COUNT(DISTINCT date) FROM attendance WHERE date BETWEEN ? AND ?",
                           (start, end)).fetchone()[0]
    rows = conn.execute("""
        SELECT u.id, u.name, u.roll_no, u.department,
               COALESCE(SUM(a.status='Present'),0) AS present,
               COALESCE(SUM(a.status='Late'),0)    AS late
        FROM users u
        LEFT JOIN attendance a ON a.user_id=u.id AND a.date BETWEEN ? AND ?
        WHERE u.active=1 GROUP BY u.id ORDER BY u.name""", (start, end)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        attended = d["present"] + d["late"]
        d["attended"] = attended
        d["absent"] = max(working - attended, 0)
        d["percent"] = round(100 * attended / working, 1) if working else 0.0
        out.append(d)
    return working, out


def has_face_data(name):
    """True if a face folder like person_3_<name> exists."""
    if not os.path.isdir(FACES_DIR):
        return False
    return any(f.split("_", 2)[-1] == name for f in os.listdir(FACES_DIR))
