import csv
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db
import face_engine

app = Flask(__name__)
# Set SECRET_KEY in the environment for real deployments.
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
db.init_db()


# ------------------------------------------------------------------ helpers
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    if request.method == "POST" and request.path.startswith("/api/") and "user" not in session:
        return jsonify(error="Session expired - log in again."), 401
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
    if request.method == "POST" and (not sent or sent != session.get("_csrf")):
        abort(400, "Invalid CSRF token - go back, refresh the page and try again.")


@app.context_processor
def inject_globals():
    return {"settings": db.get_settings(), "current_user": session.get("user"),
            "role": session.get("role")}


def login_required(admin_only=False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if "user" not in session:
                if request.path.startswith("/api/"):
                    return jsonify(error="Session expired - log in again."), 401
                return redirect(url_for("login", next=request.path))
            if admin_only and session.get("role") != "admin":
                flash("Admin access required.", "danger")
                return redirect(url_for("dashboard"))
            return fn(*a, **kw)
        return wrapper
    return deco


def valid_date(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------- auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        conn = db.get_conn()
        row = conn.execute("SELECT * FROM staff WHERE username=?",
                           (request.form.get("username", "").strip(),)).fetchone()
        conn.close()
        if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user"], session["role"] = row["username"], row["role"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//")
                            else url_for("dashboard"))
        flash("Wrong username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required()
def account():
    if request.method == "POST":
        conn = db.get_conn()
        row = conn.execute("SELECT * FROM staff WHERE username=?", (session["user"],)).fetchone()
        new = request.form.get("new_password", "")
        if not check_password_hash(row["password_hash"], request.form.get("old_password", "")):
            flash("Current password is wrong.", "danger")
        elif len(new) < 6:
            flash("New password must be at least 6 characters.", "danger")
        else:
            conn.execute("UPDATE staff SET password_hash=? WHERE id=?",
                         (generate_password_hash(new), row["id"]))
            conn.commit()
            flash("Password changed.", "success")
        conn.close()
    return render_template("account.html")


# ---------------------------------------------------------------- dashboard
@app.route("/")
@login_required()
def dashboard():
    return render_template("dashboard.html", today=db.today())


@app.route("/api/live")
@login_required()
def api_live():
    """Polled every few seconds by the dashboard for real-time updates."""
    d = db.today()
    return jsonify(summary=db.day_summary(d), recent=db.recent(10),
                   hourly=db.hourly(d), trend=db.trend(14),
                   server_time=datetime.now().strftime("%H:%M:%S"))


# --------------------------------------------------------- attendance log
@app.route("/attendance")
@login_required()
def attendance():
    date = request.args.get("date") or db.today()
    if not valid_date(date):
        abort(400, "Invalid date")
    q = request.args.get("q", "").strip()
    conn = db.get_conn()
    rows = conn.execute("""SELECT a.rowid AS id, a.name, a.time, a.status, a.method, u.roll_no
                           FROM attendance a LEFT JOIN users u ON u.id=a.user_id
                           WHERE a.date=? AND a.name LIKE ? ORDER BY a.time""",
                        (date, f"%{q}%")).fetchall()
    absent = conn.execute("""SELECT id, name, roll_no FROM users WHERE active=1 AND id NOT IN
                             (SELECT user_id FROM attendance WHERE date=? AND user_id IS NOT NULL)
                             ORDER BY name""", (date,)).fetchall()
    conn.close()
    return render_template("attendance.html", date=date, q=q, rows=rows, absent=absent,
                           summary=db.day_summary(date))


@app.route("/attendance/mark", methods=["POST"])
@login_required()
def mark_manual():
    date = request.form.get("date", "")
    time = request.form.get("time") or datetime.now().strftime("%H:%M")
    if not valid_date(date):
        abort(400, "Invalid date")
    conn = db.get_conn()
    user = conn.execute("SELECT * FROM users WHERE id=?", (request.form.get("user_id"),)).fetchone()
    if not user:
        abort(404)
    status = request.form.get("status", "Present")
    if status not in ("Present", "Late"):
        abort(400)
    conn.execute("""INSERT OR REPLACE INTO attendance(name,time,date,user_id,status,method)
                    VALUES(?,?,?,?,?,'manual')""",
                 (user["name"], time + ":00" if len(time) == 5 else time, date, user["id"], status))
    conn.commit()
    conn.close()
    flash(f"Marked {user['name']} as {status}.", "success")
    return redirect(url_for("attendance", date=date))


@app.route("/attendance/<int:rowid>/delete", methods=["POST"])
@login_required(admin_only=True)
def delete_record(rowid):
    conn = db.get_conn()
    row = conn.execute("SELECT date FROM attendance WHERE rowid=?", (rowid,)).fetchone()
    if not row:
        abort(404)
    conn.execute("DELETE FROM attendance WHERE rowid=?", (rowid,))
    conn.commit()
    conn.close()
    flash("Record deleted.", "info")
    return redirect(url_for("attendance", date=row["date"]))


# ----------------------------------------------------------------- reports
def _range():
    end = request.args.get("end") or db.today()
    start = request.args.get("start") or (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
    if not (valid_date(start) and valid_date(end)):
        abort(400, "Invalid date")
    return start, end


@app.route("/reports")
@login_required()
def reports():
    start, end = _range()
    working, rows = db.student_report(start, end)
    return render_template("reports.html", start=start, end=end, working=working, rows=rows)


@app.route("/reports/export.csv")
@login_required()
def export_csv():
    start, end = _range()
    kind = request.args.get("kind", "records")
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "summary":
        working, rows = db.student_report(start, end)
        w.writerow(["Name", "Roll No", "Department", "Present", "Late", "Absent",
                    "Working days", "Attendance %"])
        for r in rows:
            w.writerow([r["name"], r["roll_no"] or "", r["department"] or "", r["present"],
                        r["late"], r["absent"], working, r["percent"]])
    else:
        conn = db.get_conn()
        w.writerow(["Date", "Time", "Name", "Roll No", "Status", "Method"])
        for r in conn.execute("""SELECT a.date, a.time, a.name, u.roll_no, a.status, a.method
                                 FROM attendance a LEFT JOIN users u ON u.id=a.user_id
                                 WHERE a.date BETWEEN ? AND ? ORDER BY a.date, a.time""",
                              (start, end)):
            w.writerow([r["date"], r["time"], r["name"], r["roll_no"] or "", r["status"], r["method"]])
        conn.close()
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=attendance_{kind}_{start}_to_{end}.csv"})


# ---------------------------------------------------------- user management
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,38}[A-Za-z0-9]$")   # becomes a folder name -> keep it safe


@app.route("/users")
@login_required()
def users():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY active DESC, name").fetchall()
    staff = conn.execute("SELECT id, username, role FROM staff ORDER BY username").fetchall()
    conn.close()
    face = {r["name"]: db.has_face_data(r["name"]) for r in rows}
    return render_template("users.html", users=rows, staff=staff, face=face)


@app.route("/users/new", methods=["GET", "POST"])
@app.route("/users/<int:uid>/edit", methods=["GET", "POST"])
@login_required(admin_only=True)
def user_form(uid=None):
    conn = db.get_conn()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None
    if uid and not user:
        abort(404)
    if request.method == "POST":
        f = {k: request.form.get(k, "").strip() for k in ("name", "roll_no", "email", "department")}
        if not NAME_RE.match(f["name"]):
            flash("Name: 2-40 chars, letters/digits/space/._- only, starting and ending with a letter or digit (it must match the face folder name).", "danger")
        else:
            try:
                if user:
                    if user["name"] != f["name"]:
                        face_engine.rename_person(user["name"], f["name"])
                    conn.execute("""UPDATE users SET name=?, roll_no=?, email=?, department=?, active=?
                                    WHERE id=?""", (f["name"], f["roll_no"], f["email"], f["department"],
                                                    1 if request.form.get("active") else 0, uid))
                    conn.execute("UPDATE attendance SET name=? WHERE user_id=?", (f["name"], uid))
                else:
                    conn.execute("INSERT INTO users(name,roll_no,email,department,created_at) VALUES(?,?,?,?,?)",
                                 (f["name"], f["roll_no"], f["email"], f["department"],
                                  datetime.now().isoformat(timespec="seconds")))
                conn.commit()
                flash("User saved.", "success")
                conn.close()
                return redirect(url_for("users"))
            except sqlite3.IntegrityError:
                flash("A user with that name already exists.", "danger")
    conn.close()
    return render_template("user_form.html", user=user)


@app.route("/users/<int:uid>/toggle", methods=["POST"])
@login_required(admin_only=True)
def user_toggle(uid):
    conn = db.get_conn()
    conn.execute("UPDATE users SET active = 1 - active WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/delete", methods=["POST"])
@login_required(admin_only=True)
def user_delete(uid):
    conn = db.get_conn()
    row = conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        abort(404)
    conn.execute("DELETE FROM attendance WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    face_engine.delete_person(row["name"])
    flash("User, their attendance records and face data deleted.", "info")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>")
@login_required()
def user_detail(uid):
    conn = db.get_conn()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        abort(404)
    recs = conn.execute("SELECT date, time, status, method FROM attendance WHERE user_id=? "
                        "ORDER BY date DESC LIMIT 60", (uid,)).fetchall()
    conn.close()
    start = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
    working, rows = db.student_report(start, db.today())
    me = next((r for r in rows if r["id"] == uid), None)
    return render_template("user_detail.html", user=user, recs=recs, me=me, working=working,
                           has_face=db.has_face_data(user["name"]))


@app.route("/users/rebuild-features", methods=["POST"])
@login_required(admin_only=True)
def rebuild_features():
    """Re-train every person from their photos (same result as features_extraction_to_csv.py)."""
    try:
        n = face_engine.train_all()
        flash(f"Face data rebuilt for {n} person(s).", "success")
    except Exception as e:
        flash(f"Could not rebuild face data: {e}", "danger")
    return redirect(url_for("users"))


# ------------------------------------------------- face scan + registration
def _image_from_request():
    f = request.files.get("image")
    if not f:
        abort(400, "No image")
    try:
        return face_engine.decode_image(f.read())
    except ValueError as e:
        abort(400, str(e))


@app.route("/scan")
@login_required()
def scan():
    ok, msg = face_engine.status()
    names, _ = face_engine.load_known()
    return render_template("scan.html", engine_ok=ok, engine_msg=msg, known=len(names))


@app.route("/api/scan", methods=["POST"])
@login_required()
def api_scan():
    """Receives one webcam frame, returns face boxes + names, and marks attendance."""
    img = _image_from_request()
    try:
        faces, (w, h) = face_engine.recognize(img)
    except RuntimeError as e:
        return jsonify(error=str(e)), 503
    out = []
    for f in faces:
        item = {"box": f["box"], "name": f["name"], "status": "unknown", "time": None}
        if f["name"]:
            conf = round(max(0.0, 1 - f["distance"] / face_engine.THRESHOLD), 2)
            inserted, status = db.log_attendance(f["name"], "face", conf)
            item["status"] = ("inactive" if status == "inactive" else
                              "marked" if inserted else "already")
            item["label"] = status if inserted else None
        out.append(item)
    return jsonify(faces=out)


@app.route("/users/<int:uid>/face")
@login_required(admin_only=True)
def face_register(uid):
    conn = db.get_conn()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not user:
        abort(404)
    ok, msg = face_engine.status()
    return render_template("face_register.html", user=user, engine_ok=ok, engine_msg=msg,
                           count=face_engine.photo_count(user["name"]))


def _user_name(uid):
    conn = db.get_conn()
    row = conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    return row["name"]


@app.route("/api/users/<int:uid>/face/photo", methods=["POST"])
@login_required(admin_only=True)
def api_face_photo(uid):
    name = _user_name(uid)
    img = _image_from_request()
    try:
        ok, msg, count = face_engine.save_photo(name, img)
    except RuntimeError as e:
        return jsonify(error=str(e)), 503
    return jsonify(ok=ok, message=msg, count=count)


@app.route("/api/users/<int:uid>/face/train", methods=["POST"])
@login_required(admin_only=True)
def api_face_train(uid):
    name = _user_name(uid)
    if face_engine.photo_count(name) < 3:
        return jsonify(ok=False, message="Capture at least 3 photos first (8-10 is best)."), 400
    try:
        ok = face_engine.train(name)
    except RuntimeError as e:
        return jsonify(error=str(e)), 503
    return jsonify(ok=ok, message="Face data saved - this person can now be recognised." if ok
                   else "No usable face found in the photos.")


@app.route("/users/<int:uid>/face/delete", methods=["POST"])
@login_required(admin_only=True)
def face_delete(uid):
    face_engine.delete_person(_user_name(uid))
    flash("Face data deleted.", "info")
    return redirect(url_for("users"))


# ----- staff accounts + settings (admin)
@app.route("/staff/new", methods=["POST"])
@login_required(admin_only=True)
def staff_new():
    u, p, r = (request.form.get("username", "").strip(), request.form.get("password", ""),
               request.form.get("role", "teacher"))
    if not re.match(r"^[A-Za-z0-9_.-]{3,30}$", u) or len(p) < 6 or r not in ("admin", "teacher"):
        flash("Username 3-30 chars, password 6+ chars, valid role required.", "danger")
    else:
        try:
            conn = db.get_conn()
            conn.execute("INSERT INTO staff(username,password_hash,role) VALUES(?,?,?)",
                         (u, generate_password_hash(p), r))
            conn.commit()
            conn.close()
            flash(f"Staff account '{u}' created.", "success")
        except sqlite3.IntegrityError:
            flash("Username already taken.", "danger")
    return redirect(url_for("users") + "#staff")


@app.route("/staff/<int:sid>/delete", methods=["POST"])
@login_required(admin_only=True)
def staff_delete(sid):
    conn = db.get_conn()
    row = conn.execute("SELECT username FROM staff WHERE id=?", (sid,)).fetchone()
    admins = conn.execute("SELECT COUNT(*) FROM staff WHERE role='admin'").fetchone()[0]
    target = conn.execute("SELECT role FROM staff WHERE id=?", (sid,)).fetchone()
    if not row:
        abort(404)
    if row["username"] == session["user"] or (target["role"] == "admin" and admins <= 1):
        flash("You can't delete yourself or the last admin.", "danger")
    else:
        conn.execute("DELETE FROM staff WHERE id=?", (sid,))
        conn.commit()
        flash("Staff account deleted.", "info")
    conn.close()
    return redirect(url_for("users") + "#staff")


@app.route("/settings", methods=["POST"])
@login_required(admin_only=True)
def save_settings():
    late = request.form.get("late_after", "")
    if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", late):
        db.set_setting("late_after", late)
    inst = request.form.get("institution", "").strip()[:60]
    if inst:
        db.set_setting("institution", inst)
    flash("Settings saved.", "success")
    return redirect(url_for("users") + "#staff")


if __name__ == "__main__":
    app.run(debug=True)
