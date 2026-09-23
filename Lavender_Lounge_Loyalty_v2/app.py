import os
import io
import base64
import random
import sqlite3
import smtplib
import secrets
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

import qrcode
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, abort, send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "loyalty.db")))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "static" / "uploads")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def make_token():
    return secrets.token_urlsafe(24)


def get_setting(key, default=""):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = db()
    conn.execute(
        """INSERT INTO settings(key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (key, value),
    )
    conn.commit()
    conn.close()


def render_email_template(template, customer, **kwargs):
    values = {
        "name": customer["name"] if customer else "",
        "email": customer["email"] if customer else "",
        "phone": customer["phone"] if customer else "",
        **kwargs,
    }
    try:
        return template.format(**values)
    except Exception:
        return template


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        qr_token TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS login_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL COLLATE NOCASE,
        code TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        points INTEGER NOT NULL,
        amount REAL,
        note TEXT,
        performed_by TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(customer_id) REFERENCES customers(id)
    );

    CREATE TABLE IF NOT EXISTS rewards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        points_cost INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        reward_id INTEGER NOT NULL,
        points_spent INTEGER NOT NULL,
        performed_by TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(customer_id) REFERENCES customers(id),
        FOREIGN KEY(reward_id) REFERENCES rewards(id)
    );

    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS staff (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)

    # Migration for older DBs
    for col, ddl in [
        ("performed_by", "ALTER TABLE transactions ADD COLUMN performed_by TEXT"),
    ]:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
        if col not in cols:
            conn.execute(ddl)

    if conn.execute("SELECT COUNT(*) AS c FROM rewards").fetchone()["c"] == 0:
        conn.executemany(
            "INSERT INTO rewards(title, points_cost) VALUES (?, ?)",
            [
                ("Free drink", 100),
                ("10% discount", 200),
                ("Selected item", 350),
                ("VIP reward", 500),
            ],
        )

    if conn.execute("SELECT COUNT(*) AS c FROM admins").fetchone()["c"] == 0:
        conn.execute(
            """INSERT INTO admins(email, name, password_hash, active, created_at)
               VALUES (?, ?, ?, 1, ?)""",
            (
                os.getenv("ADMIN_EMAIL", "lavenderlounge.leb@gmail.com").strip().lower(),
                "Lavender Lounge Admin",
                generate_password_hash(os.getenv("ADMIN_PASSWORD", "lavender-admin")),
                now_iso(),
            ),
        )

    if conn.execute("SELECT COUNT(*) AS c FROM staff").fetchone()["c"] == 0:
        conn.execute(
            """INSERT INTO staff(email, name, password_hash, active, created_at)
               VALUES (?, ?, ?, 1, ?)""",
            (
                "staff@lavenderlounge.local",
                "Lavender Lounge Staff",
                generate_password_hash("lavender-staff"),
                now_iso(),
            ),
        )

    defaults = {
        "points_per_currency": "1",
        "logo_filename": "",
        "email_welcome_subject": "Welcome to Lavender Lounge Loyalty",
        "email_welcome_body": "Hi {name},\\n\\nWelcome to Lavender Lounge Loyalty. Your account is active.\\n\\nLavender Lounge",
        "email_points_subject": "You earned Lavender Lounge points",
        "email_points_body": "Hi {name},\\n\\nYou earned {points} points. Your new balance is {balance} points.\\n\\nLavender Lounge",
        "email_redeem_subject": "Lavender Lounge reward redeemed",
        "email_redeem_body": "Hi {name},\\n\\nYou redeemed {reward}. Your remaining balance is {balance} points.\\n\\nLavender Lounge",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k, v))

    conn.commit()
    conn.close()


def send_email(to_email, subject, body):
    gmail_address = os.getenv("GMAIL_ADDRESS", "lavenderlounge.leb@gmail.com").strip()
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()

    if not gmail_app_password:
        print("\\n--- LAVENDER LOUNGE EMAIL (DEV MODE) ---")
        print("FROM:", gmail_address)
        print("TO:", to_email)
        print("SUBJECT:", subject)
        print(body)
        print("----------------------------------------\\n")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Lavender Lounge <{gmail_address}>"
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)


def customer_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("customer_id"):
            return redirect(url_for("home"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


def staff_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("staff_id"):
            return redirect(url_for("staff_login"))
        return fn(*args, **kwargs)
    return wrapper


def current_operator():
    if session.get("admin_id"):
        return f"Admin: {session.get('admin_name','Admin')}"
    if session.get("staff_id"):
        return f"Staff: {session.get('staff_name','Staff')}"
    return "System"


@app.context_processor
def inject_branding():
    return {
        "brand_logo": get_setting("logo_filename", ""),
    }


@app.get("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/health")
def health():
    return {"status": "ok"}, 200


# ---------------- PUBLIC CUSTOMER ----------------

@app.route("/")
def home():
    if session.get("customer_id"):
        return redirect(url_for("card"))
    return render_template("home.html")


@app.route("/join", methods=["GET", "POST"])
def join():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()

        if not name or not phone or not email or "@" not in email:
            flash("Name, phone and a valid email are required.", "error")
            return render_template("join.html")

        conn = db()
        customer = conn.execute("SELECT * FROM customers WHERE email=?", (email,)).fetchone()
        is_new = customer is None

        if is_new:
            conn.execute(
                "INSERT INTO customers(email,name,phone,points,qr_token,created_at) VALUES (?,?,?,?,?,?)",
                (email, name, phone, 0, make_token(), now_iso()),
            )
        else:
            conn.execute("UPDATE customers SET name=?, phone=? WHERE id=?", (name, phone, customer["id"]))
        conn.commit()

        code = f"{random.randint(0,999999):06d}"
        expires = (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO login_codes(email,code,expires_at,created_at) VALUES (?,?,?,?)",
            (email, code, expires, now_iso()),
        )
        conn.commit()
        conn.close()

        send_email(
            email,
            "Your Lavender Lounge verification code",
            f"Hi {name},\\n\\nYour verification code is {code}.\\nIt expires in 10 minutes.\\n\\nLavender Lounge",
        )
        session["pending_email"] = email
        session["new_customer"] = is_new
        return redirect(url_for("verify"))

    return render_template("join.html")


@app.route("/customer-login", methods=["GET", "POST"])
def customer_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        conn = db()
        customer = conn.execute("SELECT * FROM customers WHERE email=?", (email,)).fetchone()
        if not customer:
            conn.close()
            flash("No loyalty account was found with this email.", "error")
            return render_template("customer_login.html")

        code = f"{random.randint(0,999999):06d}"
        expires = (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO login_codes(email,code,expires_at,created_at) VALUES (?,?,?,?)",
            (email, code, expires, now_iso()),
        )
        conn.commit()
        conn.close()

        send_email(
            email,
            "Your Lavender Lounge login code",
            f"Hi {customer['name']},\\n\\nYour login code is {code}.\\nIt expires in 10 minutes.\\n\\nLavender Lounge",
        )
        session["pending_email"] = email
        session["new_customer"] = False
        return redirect(url_for("verify"))

    return render_template("customer_login.html")


@app.route("/verify", methods=["GET", "POST"])
def verify():
    email = session.get("pending_email")
    if not email:
        return redirect(url_for("home"))

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        conn = db()
        row = conn.execute(
            """SELECT * FROM login_codes WHERE email=? AND code=? AND used=0
               ORDER BY id DESC LIMIT 1""",
            (email, code),
        ).fetchone()

        if not row:
            conn.close()
            flash("Invalid verification code.", "error")
            return render_template("verify.html", email=email)

        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            conn.close()
            flash("This code has expired.", "error")
            return render_template("verify.html", email=email)

        customer = conn.execute("SELECT * FROM customers WHERE email=?", (email,)).fetchone()
        conn.execute("UPDATE login_codes SET used=1 WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()

        session["customer_id"] = customer["id"]
        is_new = session.pop("new_customer", False)
        session.pop("pending_email", None)

        if is_new:
            subject = get_setting("email_welcome_subject")
            body = render_email_template(get_setting("email_welcome_body"), customer)
            send_email(customer["email"], subject, body)

        return redirect(url_for("card"))

    return render_template("verify.html", email=email)


@app.get("/card")
@customer_required
def card():
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (session["customer_id"],)).fetchone()
    txns = conn.execute(
        "SELECT * FROM transactions WHERE customer_id=? ORDER BY id DESC LIMIT 20",
        (customer["id"],),
    ).fetchall()
    rewards = conn.execute("SELECT * FROM rewards WHERE active=1 ORDER BY points_cost").fetchall()
    conn.close()
    next_reward = next((r for r in rewards if r["points_cost"] > customer["points"]), None)
    return render_template("card.html", customer=customer, txns=txns, rewards=rewards, next_reward=next_reward)


@app.get("/qr/<token>")
def qr_image(token):
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE qr_token=?", (token,)).fetchone()
    conn.close()
    if not customer:
        abort(404)

    payload = url_for("staff_customer_by_qr", token=token, _external=True)
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode()
    return f'<img alt="Lavender Lounge loyalty QR" src="data:image/png;base64,{encoded}">'


@app.get("/logout")
def logout():
    session.pop("customer_id", None)
    return redirect(url_for("home"))


# ---------------- ADMIN ----------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = db()
        admin = conn.execute("SELECT * FROM admins WHERE email=? AND active=1", (email,)).fetchone()
        conn.close()

        if admin and check_password_hash(admin["password_hash"], password):
            session.clear()
            session["admin_id"] = admin["id"]
            session["admin_name"] = admin["name"]
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect admin email or password.", "error")

    return render_template("admin_login.html")


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.get("/admin")
@admin_required
def admin_dashboard():
    conn = db()
    stats = {
        "customers": conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"],
        "points": conn.execute("SELECT COALESCE(SUM(points),0) c FROM customers").fetchone()["c"],
        "txns": conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"],
        "staff": conn.execute("SELECT COUNT(*) c FROM staff WHERE active=1").fetchone()["c"],
    }
    recent = conn.execute("SELECT * FROM customers ORDER BY id DESC LIMIT 8").fetchall()
    conn.close()
    return render_template("admin_dashboard.html", stats=stats, recent=recent)


@app.get("/admin/clients")
@admin_required
def admin_clients():
    q = request.args.get("q", "").strip()
    conn = db()
    if q:
        customers = conn.execute(
            """SELECT * FROM customers WHERE name LIKE ? OR email LIKE ? OR phone LIKE ?
               ORDER BY name COLLATE NOCASE""",
            (f"%{q}%", f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        customers = conn.execute("SELECT * FROM customers ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return render_template("admin_clients.html", customers=customers, q=q)


@app.route("/admin/client/add", methods=["GET", "POST"])
@admin_required
def admin_client_add():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip().lower()
        points = request.form.get("points", "0").strip()
        try:
            points = max(0, int(points))
        except ValueError:
            points = 0

        if not name or not phone or not email:
            flash("Name, phone and email are required.", "error")
            return render_template("admin_client_form.html", mode="add", customer=None)

        conn = db()
        try:
            conn.execute(
                "INSERT INTO customers(email,name,phone,points,qr_token,created_at) VALUES (?,?,?,?,?,?)",
                (email, name, phone, points, make_token(), now_iso()),
            )
            conn.commit()
            customer_id = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
            conn.close()

            subject = get_setting("email_welcome_subject")
            body = render_email_template(get_setting("email_welcome_body"), customer)
            send_email(email, subject, body)

            flash("Customer added and welcome email sent.", "success")
            return redirect(url_for("admin_customer", customer_id=customer_id))
        except sqlite3.IntegrityError:
            conn.close()
            flash("A customer with this email already exists.", "error")

    return render_template("admin_client_form.html", mode="add", customer=None)


@app.route("/admin/client/<int:customer_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_client_edit(customer_id):
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        conn.close()
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        if not name or not email or not phone:
            flash("Name, email and phone are required.", "error")
        else:
            try:
                conn.execute("UPDATE customers SET name=?,email=?,phone=? WHERE id=?", (name,email,phone,customer_id))
                conn.commit()
                conn.close()
                flash("Customer updated.", "success")
                return redirect(url_for("admin_customer", customer_id=customer_id))
            except sqlite3.IntegrityError:
                flash("Another customer already uses that email.", "error")

    conn.close()
    return render_template("admin_client_form.html", mode="edit", customer=customer)


@app.post("/admin/client/<int:customer_id>/delete")
@admin_required
def admin_client_delete(customer_id):
    conn = db()
    conn.execute("DELETE FROM transactions WHERE customer_id=?", (customer_id,))
    conn.execute("DELETE FROM redemptions WHERE customer_id=?", (customer_id,))
    conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
    conn.commit()
    conn.close()
    flash("Customer deleted.", "success")
    return redirect(url_for("admin_clients"))


@app.route("/admin/customer/<int:customer_id>", methods=["GET", "POST"])
@admin_required
def admin_customer(customer_id):
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        conn.close()
        abort(404)

    if request.method == "POST":
        action = request.form.get("action")
        note = request.form.get("note", "").strip()

        if action == "purchase":
            try:
                amount = float(request.form.get("amount", "0"))
            except ValueError:
                amount = 0
            if amount > 0:
                rate = float(get_setting("points_per_currency", "1") or "1")
                pts = int(round(amount * rate))
                conn.execute("UPDATE customers SET points=points+? WHERE id=?", (pts, customer_id))
                conn.execute(
                    """INSERT INTO transactions(customer_id,kind,points,amount,note,performed_by,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (customer_id, "purchase", pts, amount, note or "Purchase", current_operator(), now_iso()),
                )
                conn.commit()
                balance = conn.execute("SELECT points FROM customers WHERE id=?", (customer_id,)).fetchone()["points"]
                subject = get_setting("email_points_subject")
                body = render_email_template(get_setting("email_points_body"), customer, points=pts, balance=balance)
                send_email(customer["email"], subject, body)
                flash(f"Added {pts} points.", "success")

        elif action == "adjust":
            # Admin only
            try:
                pts = int(request.form.get("points", "0"))
            except ValueError:
                pts = 0
            current = conn.execute("SELECT points FROM customers WHERE id=?", (customer_id,)).fetchone()["points"]
            new_total = max(0, current + pts)
            applied = new_total - current
            conn.execute("UPDATE customers SET points=? WHERE id=?", (new_total, customer_id))
            conn.execute(
                """INSERT INTO transactions(customer_id,kind,points,note,performed_by,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (customer_id, "adjustment", applied, note or "Admin adjustment", current_operator(), now_iso()),
            )
            conn.commit()
            flash(f"Points adjusted by {applied}.", "success")

        elif action == "redeem":
            reward_id = int(request.form.get("reward_id", "0"))
            reward = conn.execute("SELECT * FROM rewards WHERE id=? AND active=1", (reward_id,)).fetchone()
            if reward:
                current = conn.execute("SELECT points FROM customers WHERE id=?", (customer_id,)).fetchone()["points"]
                if current >= reward["points_cost"]:
                    conn.execute("UPDATE customers SET points=points-? WHERE id=?", (reward["points_cost"], customer_id))
                    conn.execute(
                        """INSERT INTO transactions(customer_id,kind,points,note,performed_by,created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (customer_id, "redemption", -reward["points_cost"], f"Redeemed: {reward['title']}", current_operator(), now_iso()),
                    )
                    conn.execute(
                        """INSERT INTO redemptions(customer_id,reward_id,points_spent,performed_by,created_at)
                           VALUES (?,?,?,?,?)""",
                        (customer_id, reward["id"], reward["points_cost"], current_operator(), now_iso()),
                    )
                    conn.commit()
                    balance = current - reward["points_cost"]
                    subject = get_setting("email_redeem_subject")
                    body = render_email_template(get_setting("email_redeem_body"), customer, reward=reward["title"], balance=balance)
                    send_email(customer["email"], subject, body)
                    flash("Reward redeemed.", "success")
                else:
                    flash("Not enough points.", "error")

        elif action == "send_email":
            subject = request.form.get("subject", "").strip()
            message = request.form.get("message", "").strip()
            if subject and message:
                send_email(customer["email"], subject, f"Hi {customer['name']},\\n\\n{message}\\n\\nLavender Lounge")
                flash("Email sent.", "success")

        conn.close()
        return redirect(url_for("admin_customer", customer_id=customer_id))

    txns = conn.execute("SELECT * FROM transactions WHERE customer_id=? ORDER BY id DESC LIMIT 50", (customer_id,)).fetchall()
    rewards = conn.execute("SELECT * FROM rewards WHERE active=1 ORDER BY points_cost").fetchall()
    conn.close()
    return render_template("admin_customer.html", customer=customer, txns=txns, rewards=rewards)


@app.route("/admin/rewards", methods=["GET", "POST"])
@admin_required
def admin_rewards():
    conn = db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            title = request.form.get("title", "").strip()
            try:
                cost = int(request.form.get("points_cost", "0"))
            except ValueError:
                cost = 0
            if title and cost > 0:
                conn.execute("INSERT INTO rewards(title,points_cost,active) VALUES (?,?,1)", (title,cost))
                conn.commit()
                flash("Offer added.", "success")
        elif action == "toggle":
            rid = int(request.form.get("reward_id"))
            conn.execute("UPDATE rewards SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (rid,))
            conn.commit()
        elif action == "delete":
            rid = int(request.form.get("reward_id"))
            conn.execute("DELETE FROM rewards WHERE id=?", (rid,))
            conn.commit()
        return redirect(url_for("admin_rewards"))

    rewards = conn.execute("SELECT * FROM rewards ORDER BY active DESC, points_cost").fetchall()
    conn.close()
    return render_template("admin_rewards.html", rewards=rewards)


@app.route("/admin/branding", methods=["GET", "POST"])
@admin_required
def admin_branding():
    if request.method == "POST":
        file = request.files.get("logo")
        if file and file.filename:
            ext = file.filename.rsplit(".",1)[-1].lower()
            if ext not in ALLOWED_LOGO_EXTENSIONS:
                flash("Logo must be PNG, JPG, JPEG or WEBP.", "error")
            else:
                filename = f"lavender_logo.{ext}"
                file.save(UPLOAD_DIR / secure_filename(filename))
                set_setting("logo_filename", filename)
                flash("Logo updated.", "success")
                return redirect(url_for("admin_branding"))
    return render_template("admin_branding.html")


@app.route("/admin/email-templates", methods=["GET", "POST"])
@admin_required
def admin_email_templates():
    keys = [
        "email_welcome_subject","email_welcome_body",
        "email_points_subject","email_points_body",
        "email_redeem_subject","email_redeem_body"
    ]
    if request.method == "POST":
        for key in keys:
            set_setting(key, request.form.get(key, "").strip())
        flash("Email templates saved.", "success")
        return redirect(url_for("admin_email_templates"))

    values = {k: get_setting(k) for k in keys}
    return render_template("admin_email_templates.html", values=values)


@app.route("/admin/program-settings", methods=["GET", "POST"])
@admin_required
def admin_program_settings():
    if request.method == "POST":
        try:
            rate = float(request.form.get("points_per_currency", "1"))
            if rate <= 0:
                raise ValueError
            set_setting("points_per_currency", str(rate))
            flash("Loyalty settings saved.", "success")
        except ValueError:
            flash("Enter a valid positive points rate.", "error")
        return redirect(url_for("admin_program_settings"))

    return render_template("admin_program_settings.html", points_per_currency=get_setting("points_per_currency","1"))


@app.route("/admin/staff", methods=["GET", "POST"])
@admin_required
def admin_staff():
    conn = db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name","").strip()
            email = request.form.get("email","").strip().lower()
            password = request.form.get("password","")
            if name and email and password:
                try:
                    conn.execute(
                        "INSERT INTO staff(email,name,password_hash,active,created_at) VALUES (?,?,?,1,?)",
                        (email,name,generate_password_hash(password),now_iso()),
                    )
                    conn.commit()
                    flash("Staff account added.", "success")
                except sqlite3.IntegrityError:
                    flash("That staff email already exists.", "error")
        elif action == "toggle":
            sid = int(request.form.get("staff_id"))
            conn.execute("UPDATE staff SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (sid,))
            conn.commit()
        return redirect(url_for("admin_staff"))

    staff_rows = conn.execute("SELECT * FROM staff ORDER BY name").fetchall()
    conn.close()
    return render_template("admin_staff.html", staff_rows=staff_rows)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    conn = db()
    admin = conn.execute("SELECT * FROM admins WHERE id=?", (session["admin_id"],)).fetchone()
    if request.method == "POST":
        name = request.form.get("name","").strip()
        email = request.form.get("email","").strip().lower()
        current_password = request.form.get("current_password","")
        new_password = request.form.get("new_password","")
        if new_password:
            if not check_password_hash(admin["password_hash"], current_password):
                flash("Current password is incorrect.", "error")
            else:
                conn.execute("UPDATE admins SET name=?,email=?,password_hash=? WHERE id=?",
                             (name,email,generate_password_hash(new_password),admin["id"]))
                conn.commit()
                session["admin_name"] = name
                flash("Admin account updated.", "success")
        else:
            conn.execute("UPDATE admins SET name=?,email=? WHERE id=?", (name,email,admin["id"]))
            conn.commit()
            session["admin_name"] = name
            flash("Admin account updated.", "success")
        admin = conn.execute("SELECT * FROM admins WHERE id=?", (session["admin_id"],)).fetchone()
    conn.close()
    return render_template("admin_settings.html", admin=admin)


# ---------------- STAFF ----------------

@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        conn = db()
        staff = conn.execute("SELECT * FROM staff WHERE email=? AND active=1", (email,)).fetchone()
        conn.close()
        if staff and check_password_hash(staff["password_hash"], password):
            session.clear()
            session["staff_id"] = staff["id"]
            session["staff_name"] = staff["name"]
            return redirect(url_for("staff_scanner"))
        flash("Incorrect staff email or password.", "error")
    return render_template("staff_login.html")


@app.get("/staff/logout")
def staff_logout():
    session.clear()
    return redirect(url_for("staff_login"))


@app.get("/staff")
@staff_required
def staff_scanner():
    return render_template("staff_scanner.html")


@app.get("/staff/qr/<token>")
@staff_required
def staff_customer_by_qr(token):
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE qr_token=?", (token,)).fetchone()
    conn.close()
    if not customer:
        abort(404)
    return redirect(url_for("staff_customer", customer_id=customer["id"]))


@app.route("/staff/customer/<int:customer_id>", methods=["GET", "POST"])
@staff_required
def staff_customer(customer_id):
    conn = db()
    customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        conn.close()
        abort(404)

    if request.method == "POST":
        action = request.form.get("action")
        note = request.form.get("note","").strip()

        if action == "purchase":
            try:
                amount = float(request.form.get("amount","0"))
            except ValueError:
                amount = 0
            if amount <= 0:
                flash("Enter a valid purchase amount.", "error")
            else:
                rate = float(get_setting("points_per_currency","1") or "1")
                pts = int(round(amount * rate))
                conn.execute("UPDATE customers SET points=points+? WHERE id=?", (pts,customer_id))
                conn.execute(
                    """INSERT INTO transactions(customer_id,kind,points,amount,note,performed_by,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (customer_id,"purchase",pts,amount,note or "Purchase",current_operator(),now_iso()),
                )
                conn.commit()
                balance = conn.execute("SELECT points FROM customers WHERE id=?", (customer_id,)).fetchone()["points"]
                subject = get_setting("email_points_subject")
                body = render_email_template(get_setting("email_points_body"), customer, points=pts, balance=balance)
                send_email(customer["email"], subject, body)
                flash(f"Purchase recorded: +{pts} points.", "success")

        elif action == "redeem":
            reward_id = int(request.form.get("reward_id","0"))
            reward = conn.execute("SELECT * FROM rewards WHERE id=? AND active=1", (reward_id,)).fetchone()
            if not reward:
                flash("Reward not found.", "error")
            else:
                current = conn.execute("SELECT points FROM customers WHERE id=?", (customer_id,)).fetchone()["points"]
                if current < reward["points_cost"]:
                    flash("Customer does not have enough points.", "error")
                else:
                    conn.execute("UPDATE customers SET points=points-? WHERE id=?", (reward["points_cost"],customer_id))
                    conn.execute(
                        """INSERT INTO transactions(customer_id,kind,points,note,performed_by,created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (customer_id,"redemption",-reward["points_cost"],f"Redeemed: {reward['title']}",current_operator(),now_iso()),
                    )
                    conn.execute(
                        """INSERT INTO redemptions(customer_id,reward_id,points_spent,performed_by,created_at)
                           VALUES (?,?,?,?,?)""",
                        (customer_id,reward["id"],reward["points_cost"],current_operator(),now_iso()),
                    )
                    conn.commit()
                    balance = current - reward["points_cost"]
                    subject = get_setting("email_redeem_subject")
                    body = render_email_template(get_setting("email_redeem_body"), customer, reward=reward["title"], balance=balance)
                    send_email(customer["email"], subject, body)
                    flash("Reward redeemed.", "success")

        # No adjust action is exposed or accepted for staff.
        conn.close()
        return redirect(url_for("staff_customer", customer_id=customer_id))

    txns = conn.execute("SELECT * FROM transactions WHERE customer_id=? ORDER BY id DESC LIMIT 30", (customer_id,)).fetchall()
    rewards = conn.execute("SELECT * FROM rewards WHERE active=1 ORDER BY points_cost").fetchall()
    conn.close()
    return render_template("staff_customer.html", customer=customer, txns=txns, rewards=rewards)


init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
