import csv
import io
import json
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import bcrypt
import psycopg
from psycopg.rows import dict_row
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SESSION_DAYS = 30
PACKAGES = {"Bronze": 125.0, "Silber": 150.0, "Gold": 300.0}
PACKAGE_BENEFITS = {
    "Bronze": "Festflyer",
    "Silber": "Monitorwerbung\nFeuerwehrwebsite\nBauzaunbanner Dorfzentrum",
    "Gold": "Festflyer\nMonitorwerbung\nBauzaunbanner Dorfzentrum\nBauzaunbanner Festeingang\nSocial Media",
}
STATUSES = ["Noch nicht kontaktiert", "Angefragt", "Zugesagt", "Abgesagt", "Nachfassen"]


def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL fehlt")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def json_env(name, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def init_db():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    expires_at TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sponsors (
                    id BIGSERIAL PRIMARY KEY,
                    year INTEGER NOT NULL,
                    company TEXT NOT NULL,
                    contact TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    address TEXT NOT NULL DEFAULT '',
                    package TEXT NOT NULL DEFAULT 'Bronze',
                    amount NUMERIC(10,2) NOT NULL DEFAULT 125,
                    status TEXT NOT NULL DEFAULT 'Noch nicht kontaktiert',
                    paid BOOLEAN NOT NULL DEFAULT FALSE,
                    payment_date DATE,
                    invoice_number TEXT NOT NULL DEFAULT '',
                    invoice_date DATE,
                    due_date DATE,
                    benefits TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_by TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS sponsors_year_idx ON sponsors(year);
                CREATE INDEX IF NOT EXISTS sponsors_company_idx ON sponsors(company);
                CREATE TABLE IF NOT EXISTS app_settings (
                    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    organization TEXT NOT NULL DEFAULT 'Feuerwehr Niederndorf',
                    event_name TEXT NOT NULL DEFAULT 'Jährliches Feuerwehrfest',
                    address TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    iban TEXT NOT NULL DEFAULT '',
                    bic TEXT NOT NULL DEFAULT '',
                    bank TEXT NOT NULL DEFAULT '',
                    invoice_prefix TEXT NOT NULL DEFAULT '',
                    tax_note TEXT NOT NULL DEFAULT '',
                    default_due_days INTEGER NOT NULL DEFAULT 14
                );
            """)
            cur.execute("SELECT COUNT(*) AS c FROM app_settings")
            if cur.fetchone()["c"] == 0:
                cur.execute(
                    "INSERT INTO app_settings (id, invoice_prefix) VALUES (1, %s)",
                    (str(date.today().year),),
                )

            # One-time migration from the previous app, provided securely as env vars.
            cur.execute("SELECT COUNT(*) AS c FROM users")
            if cur.fetchone()["c"] == 0:
                seed_user = json_env("MIGRATION_USER_JSON", None)
                if seed_user:
                    cur.execute(
                        """INSERT INTO users(display_name,email,password_hash,role,created_at)
                           VALUES (%s,%s,%s,%s,%s)""",
                        (
                            seed_user.get("display_name", "Admin"),
                            seed_user["email"].lower().strip(),
                            seed_user["password_hash"],
                            seed_user.get("role", "admin"),
                            seed_user.get("created_at") or datetime.now(timezone.utc),
                        ),
                    )

            cur.execute("SELECT COUNT(*) AS c FROM sponsors")
            if cur.fetchone()["c"] == 0:
                for s in json_env("MIGRATION_SPONSORS_JSON", []):
                    benefits = s.get("benefits") or PACKAGE_BENEFITS.get(s.get("package", "Bronze"), "")
                    cur.execute(
                        """INSERT INTO sponsors(
                            year,company,contact,phone,email,address,package,amount,status,paid,
                            payment_date,invoice_number,invoice_date,due_date,benefits,notes,updated_at,updated_by
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            s.get("year", date.today().year), s.get("company", ""), s.get("contact", ""),
                            s.get("phone", ""), s.get("email", ""), s.get("address", ""),
                            s.get("package", "Bronze"), s.get("amount", 125), s.get("status", STATUSES[0]),
                            bool(s.get("paid")), s.get("payment_date"), s.get("invoice_number", ""),
                            s.get("invoice_date"), s.get("due_date"), benefits, s.get("notes", ""),
                            s.get("updated_at") or datetime.now(timezone.utc), s.get("updated_by", ""),
                        ),
                    )

            seed_settings = json_env("MIGRATION_SETTINGS_JSON", None)
            if seed_settings:
                cur.execute(
                    """UPDATE app_settings SET organization=%s,event_name=%s,address=%s,email=%s,phone=%s,
                       iban=%s,bic=%s,bank=%s,invoice_prefix=%s,tax_note=%s,default_due_days=%s WHERE id=1""",
                    (
                        seed_settings.get("organization", "Feuerwehr Niederndorf"),
                        seed_settings.get("event_name", "Jährliches Feuerwehrfest"),
                        seed_settings.get("address", ""), seed_settings.get("email", ""),
                        seed_settings.get("phone", ""), seed_settings.get("iban", ""),
                        seed_settings.get("bic", ""), seed_settings.get("bank", ""),
                        seed_settings.get("invoice_prefix", str(date.today().year)),
                        seed_settings.get("tax_note", ""), int(seed_settings.get("default_due_days", 14)),
                    ),
                )
        conn.commit()


def current_user():
    token = request.cookies.get("session")
    if not token:
        return None
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT u.id,u.display_name,u.email,u.role
                   FROM sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.id=%s AND s.expires_at > now()""",
                (token,),
            )
            return cur.fetchone()


def require_user(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify(error="Nicht angemeldet"), 401
        request.user = user
        return fn(*args, **kwargs)
    return wrapped


def sponsor_dict(row):
    d = dict(row)
    for key in ("payment_date", "invoice_date", "due_date"):
        if d.get(key):
            d[key] = d[key].isoformat()
    if d.get("updated_at"):
        d["updated_at"] = d["updated_at"].isoformat()
    d["amount"] = float(d.get("amount") or 0)
    return d


def settings_dict(row):
    return dict(row)


@app.get("/health")
def health():
    return jsonify(ok=True, service="Feuerwehr Niederndorf Sponsoring")


@app.get("/api/me")
def api_me():
    return jsonify(user=current_user())


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).lower().strip()
    password = str(data.get("password", ""))
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,display_name,email,role,password_hash FROM users WHERE lower(email)=%s", (email,))
            user = cur.fetchone()
            if not user or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
                return jsonify(error="E-Mail oder Passwort ist falsch"), 401
            token = secrets.token_urlsafe(32)
            expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
            cur.execute("DELETE FROM sessions WHERE expires_at <= now()")
            cur.execute("INSERT INTO sessions(id,user_id,expires_at) VALUES (%s,%s,%s)", (token, user["id"], expires))
        conn.commit()
    resp = jsonify(user={k: user[k] for k in ("id", "display_name", "email", "role")})
    resp.set_cookie("session", token, httponly=True, secure=True, samesite="Lax", max_age=SESSION_DAYS*86400)
    return resp


@app.post("/api/register")
def api_register():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).lower().strip()
    display_name = str(data.get("display_name", "")).strip()
    password = str(data.get("password", ""))
    if not email or "@" not in email or not display_name or len(password) < 8:
        return jsonify(error="Bitte Name, gültige E-Mail und Passwort mit mindestens 8 Zeichen angeben"), 400
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users")
            count = cur.fetchone()["c"]
            if count >= 2:
                return jsonify(error="Es sind bereits zwei Benutzer angelegt"), 403
            cur.execute("SELECT id FROM users WHERE lower(email)=%s", (email,))
            if cur.fetchone():
                return jsonify(error="Diese E-Mail ist bereits registriert"), 409
            role = "admin" if count == 0 else "user"
            pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            cur.execute(
                "INSERT INTO users(display_name,email,password_hash,role) VALUES (%s,%s,%s,%s) RETURNING id,display_name,email,role",
                (display_name, email, pw_hash, role),
            )
            user = cur.fetchone()
            token = secrets.token_urlsafe(32)
            expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
            cur.execute("INSERT INTO sessions(id,user_id,expires_at) VALUES (%s,%s,%s)", (token, user["id"], expires))
        conn.commit()
    resp = jsonify(user=user)
    resp.set_cookie("session", token, httponly=True, secure=True, samesite="Lax", max_age=SESSION_DAYS*86400)
    return resp


@app.post("/api/logout")
def api_logout():
    token = request.cookies.get("session")
    if token:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE id=%s", (token,))
            conn.commit()
    resp = jsonify(ok=True)
    resp.delete_cookie("session")
    return resp


@app.post("/api/change-password")
@require_user
def api_change_password():
    data = request.get_json(silent=True) or {}
    current = str(data.get("current_password", ""))
    new = str(data.get("new_password", ""))
    if len(new) < 8:
        return jsonify(error="Das neue Passwort muss mindestens 8 Zeichen haben"), 400
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM users WHERE id=%s", (request.user["id"],))
            row = cur.fetchone()
            if not row or not bcrypt.checkpw(current.encode(), row["password_hash"].encode()):
                return jsonify(error="Aktuelles Passwort ist falsch"), 400
            new_hash = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
            cur.execute("UPDATE users SET password_hash=%s,updated_at=now() WHERE id=%s", (new_hash, request.user["id"]))
        conn.commit()
    return jsonify(ok=True)


@app.get("/api/packages")
@require_user
def api_packages():
    return jsonify(packages=PACKAGES, benefits=PACKAGE_BENEFITS, statuses=STATUSES)


@app.get("/api/years")
@require_user
def api_years():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT year FROM sponsors ORDER BY year DESC")
            years = [r["year"] for r in cur.fetchall()]
    current = date.today().year
    if current not in years:
        years.insert(0, current)
    return jsonify(years=years)


@app.get("/api/sponsors")
@require_user
def api_sponsors():
    year = int(request.args.get("year", date.today().year))
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sponsors WHERE year=%s ORDER BY lower(company)", (year,))
            rows = cur.fetchall()
    return jsonify(sponsors=[sponsor_dict(r) for r in rows])


@app.post("/api/sponsors")
@require_user
def api_sponsor_create():
    data = request.get_json(silent=True) or {}
    company = str(data.get("company", "")).strip()
    if not company:
        return jsonify(error="Firma ist erforderlich"), 400
    package = data.get("package", "Bronze")
    if package not in PACKAGES:
        return jsonify(error="Ungültiges Paket"), 400
    benefits = str(data.get("benefits") or PACKAGE_BENEFITS[package])
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sponsors(year,company,contact,phone,email,address,package,amount,status,paid,payment_date,due_date,benefits,notes,updated_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    int(data.get("year", date.today().year)), company, str(data.get("contact", "")),
                    str(data.get("phone", "")), str(data.get("email", "")), str(data.get("address", "")),
                    package, float(data.get("amount", PACKAGES[package])), data.get("status", STATUSES[0]),
                    bool(data.get("paid")), data.get("payment_date") or None, data.get("due_date") or None,
                    benefits, str(data.get("notes", "")), request.user["display_name"],
                ),
            )
            sid = cur.fetchone()["id"]
        conn.commit()
    return jsonify(ok=True, id=sid)


@app.put("/api/sponsors/<int:sponsor_id>")
@require_user
def api_sponsor_update(sponsor_id):
    data = request.get_json(silent=True) or {}
    company = str(data.get("company", "")).strip()
    if not company:
        return jsonify(error="Firma ist erforderlich"), 400
    package = data.get("package", "Bronze")
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE sponsors SET year=%s,company=%s,contact=%s,phone=%s,email=%s,address=%s,package=%s,amount=%s,
                   status=%s,paid=%s,payment_date=%s,due_date=%s,benefits=%s,notes=%s,updated_at=now(),updated_by=%s WHERE id=%s""",
                (
                    int(data.get("year", date.today().year)), company, str(data.get("contact", "")),
                    str(data.get("phone", "")), str(data.get("email", "")), str(data.get("address", "")),
                    package, float(data.get("amount", PACKAGES.get(package, 0))), data.get("status", STATUSES[0]),
                    bool(data.get("paid")), data.get("payment_date") or None, data.get("due_date") or None,
                    str(data.get("benefits", "")), str(data.get("notes", "")), request.user["display_name"], sponsor_id,
                ),
            )
        conn.commit()
    return jsonify(ok=True)


@app.delete("/api/sponsors/<int:sponsor_id>")
@require_user
def api_sponsor_delete(sponsor_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sponsors WHERE id=%s", (sponsor_id,))
        conn.commit()
    return jsonify(ok=True)


@app.post("/api/sponsors/<int:sponsor_id>/invoice-number")
@require_user
def api_invoice_number(sponsor_id):
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sponsors WHERE id=%s FOR UPDATE", (sponsor_id,))
            sponsor = cur.fetchone()
            if not sponsor:
                return jsonify(error="Sponsor nicht gefunden"), 404
            if sponsor["invoice_number"]:
                return jsonify(invoice_number=sponsor["invoice_number"], invoice_date=sponsor["invoice_date"].isoformat() if sponsor["invoice_date"] else "")
            cur.execute("SELECT invoice_prefix FROM app_settings WHERE id=1")
            prefix = cur.fetchone()["invoice_prefix"] or str(sponsor["year"])
            cur.execute("SELECT invoice_number FROM sponsors WHERE year=%s AND invoice_number<>''", (sponsor["year"],))
            max_no = 0
            for row in cur.fetchall():
                try:
                    max_no = max(max_no, int(str(row["invoice_number"]).split("-")[-1]))
                except Exception:
                    pass
            number = f"{prefix}-{max_no+1:03d}"
            invoice_date = date.today()
            cur.execute(
                "UPDATE sponsors SET invoice_number=%s,invoice_date=%s,updated_at=now(),updated_by=%s WHERE id=%s",
                (number, invoice_date, request.user["display_name"], sponsor_id),
            )
        conn.commit()
    return jsonify(invoice_number=number, invoice_date=invoice_date.isoformat())


@app.get("/api/dashboard")
@require_user
def api_dashboard():
    year = int(request.args.get("year", date.today().year))
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS sponsors,
                   COALESCE(SUM(CASE WHEN status='Zugesagt' THEN amount ELSE 0 END),0) AS pledged,
                   COALESCE(SUM(CASE WHEN status='Zugesagt' AND paid THEN amount ELSE 0 END),0) AS paid,
                   COALESCE(SUM(CASE WHEN status='Zugesagt' AND NOT paid THEN amount ELSE 0 END),0) AS open
                   FROM sponsors WHERE year=%s""",
                (year,),
            )
            d = cur.fetchone()
            cur.execute("SELECT status,COUNT(*) AS c FROM sponsors WHERE year=%s GROUP BY status", (year,))
            by_status = {r["status"]: r["c"] for r in cur.fetchall()}
    return jsonify(sponsors=d["sponsors"], pledged=float(d["pledged"]), paid=float(d["paid"]), open=float(d["open"]), by_status=by_status)


@app.get("/api/settings")
@require_user
def api_settings_get():
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM app_settings WHERE id=1")
            row = cur.fetchone()
    return jsonify(settings=settings_dict(row))


@app.put("/api/settings")
@require_user
def api_settings_put():
    data = (request.get_json(silent=True) or {}).get("settings", {})
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE app_settings SET organization=%s,event_name=%s,address=%s,email=%s,phone=%s,iban=%s,bic=%s,bank=%s,
                   invoice_prefix=%s,tax_note=%s,default_due_days=%s WHERE id=1""",
                (
                    str(data.get("organization", "Feuerwehr Niederndorf")), str(data.get("event_name", "Jährliches Feuerwehrfest")),
                    str(data.get("address", "")), str(data.get("email", "")), str(data.get("phone", "")),
                    str(data.get("iban", "")), str(data.get("bic", "")), str(data.get("bank", "")),
                    str(data.get("invoice_prefix", str(date.today().year))), str(data.get("tax_note", "")),
                    int(data.get("default_due_days", 14)),
                ),
            )
        conn.commit()
    return jsonify(ok=True)


@app.post("/api/rollover")
@require_user
def api_rollover():
    data = request.get_json(silent=True) or {}
    from_year = int(data.get("from_year", date.today().year))
    to_year = int(data.get("to_year", from_year + 1))
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sponsors WHERE year=%s ORDER BY id", (from_year,))
            source = cur.fetchall()
            count = 0
            for s in source:
                cur.execute("SELECT 1 FROM sponsors WHERE year=%s AND lower(company)=lower(%s)", (to_year, s["company"]))
                if cur.fetchone():
                    continue
                cur.execute(
                    """INSERT INTO sponsors(year,company,contact,phone,email,address,package,amount,status,paid,benefits,notes,updated_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,%s,%s)""",
                    (to_year,s["company"],s["contact"],s["phone"],s["email"],s["address"],s["package"],s["amount"],STATUSES[0],s["benefits"],s["notes"],request.user["display_name"]),
                )
                count += 1
        conn.commit()
    return jsonify(ok=True, count=count)


@app.get("/api/export.csv")
@require_user
def api_export():
    year = int(request.args.get("year", date.today().year))
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sponsors WHERE year=%s ORDER BY lower(company)", (year,))
            rows = cur.fetchall()
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Firma","Ansprechpartner","Telefon","E-Mail","Adresse","Paket","Betrag","Status","Bezahlt","Zahlungsdatum","Rechnungsnummer","Rechnungsdatum","Fällig am","Gegenleistungen","Notizen","Bearbeitet von"])
    for r in rows:
        writer.writerow([r["company"],r["contact"],r["phone"],r["email"],r["address"],r["package"],r["amount"],r["status"],"Ja" if r["paid"] else "Nein",r["payment_date"] or "",r["invoice_number"],r["invoice_date"] or "",r["due_date"] or "",r["benefits"],r["notes"],r["updated_by"]])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="sponsoren-{year}.csv"'})


INDEX_HTML = '<!doctype html>\n<html lang="de">\n<head>\n  <meta charset="utf-8" />\n  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />\n  <meta name="theme-color" content="#a71d2a" />\n  <meta name="apple-mobile-web-app-capable" content="yes" />\n  <meta name="apple-mobile-web-app-status-bar-style" content="default" />\n  <meta name="apple-mobile-web-app-title" content="FF Sponsoring" />\n  <title>FF Niederndorf · Sponsoring</title>\n  <link rel="manifest" href="/manifest.json" />\n  <link rel="stylesheet" href="/styles.css" />\n</head>\n<body>\n<div id="toast" class="toast"></div>\n<section id="loginView" class="login-shell">\n  <div class="login-card card">\n    <div class="brand-mark">FF</div>\n    <p class="eyebrow">Feuerwehr Niederndorf</p>\n    <h1>Sponsoring Feuerwehrfest</h1>\n    <p class="muted intro">Gemeinsame Verwaltung für Sponsoren, Zahlungen, Rechnungen und Jahresübersicht.</p>\n    <div class="login-tabs"><button id="showLogin" class="primary">Anmelden</button><button id="showRegister" class="secondary">Konto anlegen</button></div>\n    <form id="loginForm">\n      <label>E-Mail<input id="loginEmail" type="email" autocomplete="email" required /></label>\n      <label>Passwort<input id="loginPassword" type="password" autocomplete="current-password" required /></label>\n      <button class="primary full" type="submit">Anmelden</button>\n    </form>\n    <form id="registerForm" class="hidden">\n      <label>Name<input id="regName" required /></label>\n      <label>E-Mail<input id="regEmail" type="email" required /></label>\n      <label>Passwort<input id="regPassword" type="password" minlength="8" required /></label>\n      <p class="hint">Es können maximal zwei Benutzer angelegt werden.</p>\n      <button class="primary full" type="submit">Konto anlegen</button>\n    </form>\n  </div>\n</section>\n\n<div id="appView" class="app hidden">\n  <aside class="sidebar">\n    <div class="sidebar-brand"><span class="brand-mark small">FF</span><div><b>Niederndorf</b><small>Sponsoring</small></div></div>\n    <nav>\n      <button data-view="dashboard" class="nav-btn active">Übersicht</button>\n      <button data-view="sponsors" class="nav-btn">Sponsoren</button>\n      <button data-view="settings" class="nav-btn">Einstellungen</button>\n    </nav>\n    <div class="sidebar-bottom"><small id="userName"></small><button id="logoutBtn" class="ghost">Abmelden</button></div>\n  </aside>\n\n  <main class="main">\n    <header class="topbar">\n      <div><p id="pageSub" class="eyebrow">Jährliches Feuerwehrfest</p><h1 id="pageTitle">Sponsoring im Überblick</h1></div>\n      <div class="top-actions"><select id="yearSelect"></select><button id="addSponsorTop" class="primary">+ Sponsor</button></div>\n    </header>\n\n    <div class="mobile-nav">\n      <button data-view="dashboard" class="nav-btn active">Übersicht</button>\n      <button data-view="sponsors" class="nav-btn">Sponsoren</button>\n      <button data-view="settings" class="nav-btn">Einstellungen</button>\n    </div>\n\n    <section id="dashboardView" class="view">\n      <div class="stats">\n        <article class="stat card"><span>Sponsoren</span><strong id="statSponsors">0</strong><small>im aktuellen Jahr</small></article>\n        <article class="stat card"><span>Zugesagt</span><strong id="statPledged">€ 0</strong><small>bestätigte Sponsorings</small></article>\n        <article class="stat card"><span>Bezahlt</span><strong id="statPaid">€ 0</strong><small>bereits eingegangen</small></article>\n        <article class="stat card"><span>Offen</span><strong id="statOpen">€ 0</strong><small>noch ausständig</small></article>\n      </div>\n      <div class="grid2">\n        <article class="card pad"><div class="section-head"><div><p class="eyebrow">Fortschritt</p><h2>Status</h2></div></div><div id="statusBars"></div></article>\n        <article class="card pad"><div class="section-head"><div><p class="eyebrow">Pakete</p><h2>Sponsoring-Pakete</h2></div></div>\n          <div class="packages"><div><b>Bronze</b><span>125 €</span></div><div><b>Silber</b><span>150 €</span></div><div><b>Gold</b><span>300 €</span></div></div>\n        </article>\n      </div>\n      <article class="card pad"><div class="section-head"><div><p class="eyebrow">Zuletzt bearbeitet</p><h2>Aktuelle Sponsoren</h2></div><button id="goSponsors" class="link-btn">Alle</button></div><div id="recentSponsors" class="recent"></div></article>\n    </section>\n\n    <section id="sponsorsView" class="view hidden">\n      <div class="toolbar card">\n        <input id="searchInput" placeholder="Firma oder Ansprechpartner suchen…" />\n        <select id="statusFilter"><option value="">Alle Status</option></select>\n        <button id="exportBtn" class="secondary">CSV</button>\n        <button id="rolloverBtn" class="secondary">Jahr übernehmen</button>\n        <button id="addSponsorBtn" class="primary">+ Sponsor</button>\n      </div>\n      <div class="card table-wrap"><table><thead><tr><th>Firma</th><th>Paket</th><th>Betrag</th><th>Status</th><th>Zahlung</th><th>Rechnung</th><th></th></tr></thead><tbody id="sponsorsBody"></tbody></table></div>\n    </section>\n\n    <section id="settingsView" class="view hidden">\n      <div class="settings-grid">\n        <form id="settingsForm" class="card pad">\n          <p class="eyebrow">Organisation & Rechnungen</p><h2>Feuerwehr Niederndorf</h2>\n          <div class="row2"><label>Organisation<input id="setOrganization" /></label><label>Veranstaltung<input id="setEventName" /></label></div>\n          <label>Adresse<textarea id="setAddress"></textarea></label>\n          <div class="row2"><label>E-Mail<input id="setEmail" type="email" /></label><label>Telefon<input id="setPhone" /></label></div>\n          <div class="row2"><label>IBAN<input id="setIban" /></label><label>BIC<input id="setBic" /></label></div>\n          <div class="row2"><label>Bank<input id="setBank" /></label><label>Rechnungs-Präfix<input id="setPrefix" /></label></div>\n          <label>Zahlungsziel (Tage)<input id="setDueDays" type="number" min="0" /></label>\n          <label>Rechnungshinweis<textarea id="setTaxNote"></textarea></label>\n          <button class="primary" type="submit">Einstellungen speichern</button>\n        </form>\n        <form id="passwordForm" class="card pad compact-form">\n          <p class="eyebrow">Sicherheit</p><h2>Passwort ändern</h2>\n          <label>Aktuelles Passwort<input id="pwCurrent" type="password" /></label>\n          <label>Neues Passwort<input id="pwNew" type="password" minlength="8" /></label>\n          <button class="secondary" type="submit">Passwort ändern</button>\n        </form>\n      </div>\n    </section>\n  </main>\n</div>\n\n<dialog id="sponsorDialog">\n  <form id="sponsorForm" class="dialog-card">\n    <div class="dialog-head"><div><h2 id="dialogTitle">Sponsor hinzufügen</h2><p class="muted">Kontaktdaten, Paket und Zahlungsstatus</p></div><button type="button" id="closeDialog" class="icon-btn">×</button></div>\n    <input type="hidden" id="spId" />\n    <div class="form-grid">\n      <label class="span2">Firma *<input id="spCompany" required /></label>\n      <label>Ansprechpartner<input id="spContact" /></label><label>Telefon<input id="spPhone" /></label>\n      <label>E-Mail<input id="spEmail" type="email" /></label><label>Adresse<input id="spAddress" /></label>\n      <label>Paket<select id="spPackage"><option>Bronze</option><option>Silber</option><option>Gold</option></select></label><label>Betrag (€)<input id="spAmount" type="number" step="0.01" min="0" /></label>\n      <label>Status<select id="spStatus"></select></label><label class="checkbox-line"><input id="spPaid" type="checkbox" /> <span id="paidLabel">Nein</span></label>\n      <label>Zahlungsdatum<input id="spPaymentDate" type="date" /></label><label>Fällig am<input id="spDueDate" type="date" /></label>\n      <label class="span2">Gegenleistungen · individuell anpassbar<textarea id="spBenefits" rows="5"></textarea></label>\n      <label class="span2">Notizen<textarea id="spNotes" rows="4"></textarea></label>\n    </div>\n    <div id="documentActions" class="document-actions hidden"><button type="button" id="printInvoice" class="secondary">Rechnung</button><button type="button" id="printReceipt" class="secondary">Zahlungsbestätigung</button></div>\n    <div class="dialog-actions"><button type="button" id="deleteSponsor" class="danger hidden">Löschen</button><span class="spacer"></span><button type="button" id="closeDialog2" class="secondary">Abbrechen</button><button type="submit" class="primary">Speichern</button></div>\n  </form>\n</dialog>\n\n<script src="/app.js"></script>\n</body>\n</html>\n'
STYLES_CSS = ':root{--bg:#f5f1eb;--surface:#fffdf9;--surface2:#f0ebe4;--text:#211d1b;--muted:#726a65;--line:#ddd4cb;--accent:#a71d2a;--accent2:#861620;--ok:#2e6b4f;--warn:#9a5b17;--danger:#a71d2a;--shadow:0 16px 42px rgba(45,34,28,.08);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text);background:var(--bg)}*{box-sizing:border-box}html,body{margin:0;min-width:320px;min-height:100%;background:var(--bg);color:var(--text)}button,input,select,textarea{font:inherit}.hidden{display:none!important}.card{background:var(--surface);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow)}.pad{padding:22px}.eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:11px;font-weight:800;color:var(--accent);margin:0}.muted{color:var(--muted)}.hint{font-size:.84rem;color:var(--muted);line-height:1.45}.brand-mark{width:52px;height:52px;border-radius:16px;background:var(--accent);color:white;display:grid;place-items:center;font-weight:800;margin-bottom:18px}.brand-mark.small{width:42px;height:42px;border-radius:13px;margin:0}.login-shell{min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at top left,#f7e7e8 0,transparent 34%),var(--bg)}.login-card{width:min(470px,100%);padding:30px}.login-card h1{font-size:30px;margin:5px 0 8px}.intro{line-height:1.55;margin-bottom:20px}.login-tabs{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:18px}label{display:flex;flex-direction:column;gap:7px;font-size:.9rem;font-weight:700;margin-bottom:14px}input,select,textarea{border:1px solid var(--line);border-radius:12px;padding:11px 12px;background:#fff;color:var(--text);outline:none;min-height:42px}textarea{resize:vertical;line-height:1.4}input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(167,29,42,.1)}button{border:0;border-radius:11px;padding:10px 14px;font-weight:750;cursor:pointer}.primary{background:var(--accent);color:#fff}.primary:hover{background:var(--accent2)}.secondary{background:#fff;color:var(--accent);border:1px solid var(--accent)}.ghost{background:#332d29;color:#fff}.danger{background:var(--accent);color:#fff}.link-btn{background:transparent;color:var(--accent);padding:6px}.full{width:100%}.compact{padding:7px 10px}.app{min-height:100vh;display:grid;grid-template-columns:250px 1fr}.sidebar{background:#211d1b;color:#fff;padding:28px 18px;display:flex;flex-direction:column;gap:30px;position:sticky;top:0;height:100vh}.sidebar-brand{display:flex;gap:12px;align-items:center;padding:0 8px}.sidebar-brand small{display:block;color:#bdb5b0;margin-top:2px}.sidebar nav{display:flex;flex-direction:column;gap:6px}.nav-btn{text-align:left;background:transparent;color:#cfc7c2;padding:12px 14px;border-radius:12px}.nav-btn.active,.nav-btn:hover{background:var(--accent);color:#fff}.sidebar-bottom{margin-top:auto;border-top:1px solid #3a3430;padding:18px 8px 2px;display:grid;gap:8px}.sidebar-bottom small{color:#cfc7c2}.main{min-width:0}.topbar{padding:30px 34px 20px;display:flex;align-items:flex-end;justify-content:space-between;gap:20px}.topbar h1{font-size:30px;margin:2px 0 0}.top-actions{display:flex;gap:10px;align-items:center}.top-actions select{width:126px}.mobile-nav{display:none}.view{padding:12px 34px 42px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:8px 0 18px}.stat{padding:19px}.stat span,.stat small{display:block;color:var(--muted);font-size:12px}.stat strong{display:block;font-size:23px;margin:3px 0}.grid2{display:grid;grid-template-columns:1fr 1.15fr;gap:18px;margin-bottom:18px}.section-head{display:flex;justify-content:space-between;align-items:center}.section-head h2{font-size:21px;margin:3px 0 18px}.packages{display:grid;gap:14px}.packages div{display:flex;justify-content:space-between;padding:13px 14px;border:1px solid var(--line);border-radius:12px;background:#fff}.status-row{display:grid;grid-template-columns:155px 1fr 34px;gap:10px;align-items:center;margin:12px 0;font-size:.9rem}.bar{height:10px;background:var(--surface2);border-radius:99px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--accent);border-radius:99px}.recent{display:flex;flex-direction:column}.recent-item{border:0;border-bottom:1px solid var(--line);background:transparent;display:grid;grid-template-columns:40px 1fr auto;gap:12px;align-items:center;padding:12px 0;text-align:left;color:var(--text)}.recent-item:last-child{border-bottom:0}.recent-item small{display:block;color:var(--muted);margin-top:2px}.company-bubble{width:40px;height:40px;border-radius:12px;background:var(--surface2);display:grid;place-items:center;font-weight:800}.badge{display:inline-flex;align-items:center;border-radius:99px;padding:5px 9px;background:var(--surface2);font-size:.78rem;font-weight:750}.badge.ok{background:#e6f1eb;color:var(--ok)}.badge.warn{background:#fff0d7;color:var(--warn)}.toolbar{display:flex;gap:10px;padding:12px;margin-bottom:14px;box-shadow:none}.toolbar input{flex:1;min-width:180px}.table-wrap{overflow:auto;box-shadow:none}table{border-collapse:collapse;width:100%;min-width:900px}th,td{text-align:left;padding:13px 14px;border-bottom:1px solid var(--line);font-size:.9rem}th{font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);background:var(--surface2)}td small{display:block;color:var(--muted);margin-top:3px}.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.compact-form{align-self:start}.toast{position:fixed;right:18px;top:18px;z-index:2000;background:#211d1b;color:#fff;padding:12px 16px;border-radius:10px;opacity:0;transform:translateY(-8px);pointer-events:none;transition:.2s}.toast.show{opacity:1;transform:translateY(0)}\n/* Sponsor dialog: opaque, readable and iPhone friendly */\ndialog{border:1px solid var(--line);padding:0;border-radius:20px;max-width:700px;width:calc(100% - 24px);max-height:88dvh;background:var(--surface);color:var(--text);box-shadow:0 28px 80px rgba(20,14,12,.32);overflow:auto}dialog::backdrop{background:rgba(18,12,10,.72);backdrop-filter:blur(2px)}.dialog-card{padding:24px;background:var(--surface)}.dialog-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:18px}.dialog-head h2{margin:0}.icon-btn{background:var(--surface2);font-size:26px;line-height:1;padding:5px 10px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}.span2{grid-column:span 2}.checkbox-line{flex-direction:row;align-items:center;gap:10px;padding-top:28px}.checkbox-line input{width:22px;height:22px}.document-actions{display:flex;gap:8px;padding:16px 0;border-top:1px solid var(--line)}.dialog-actions{display:flex;gap:10px;align-items:center;padding-top:14px;border-top:1px solid var(--line);position:sticky;bottom:0;background:var(--surface);padding-bottom:max(10px,env(safe-area-inset-bottom))}.spacer{flex:1}\n@media(max-width:980px){.app{grid-template-columns:1fr}.sidebar{display:none}.mobile-nav{display:flex;padding:0 20px 14px;gap:6px;overflow:auto}.mobile-nav .nav-btn{white-space:nowrap;color:var(--text);border:1px solid transparent}.mobile-nav .nav-btn.active{background:#f6e3e5;color:var(--accent);border-color:#ecc8cc}.topbar{padding:22px 20px 16px;align-items:flex-start}.view{padding:6px 20px 32px}.stats{grid-template-columns:1fr 1fr}.grid2,.settings-grid{grid-template-columns:1fr}}\n@media(max-width:620px){.topbar{display:grid;padding-left:14px;padding-right:14px}.topbar h1{font-size:25px}.top-actions{width:100%}.top-actions button{flex:1}.mobile-nav{padding-left:14px;padding-right:14px}.view{padding-left:14px;padding-right:14px}.stats{grid-template-columns:1fr}.toolbar{display:grid}.form-grid,.row2{grid-template-columns:1fr}.span2{grid-column:auto}.document-actions{display:grid}.status-row{grid-template-columns:125px 1fr 26px;font-size:.82rem}dialog{position:fixed;inset:auto 0 0 0;margin:0;width:100vw;max-width:none;max-height:88dvh;border-radius:22px 22px 0 0}.dialog-card{padding:20px 16px calc(16px + env(safe-area-inset-bottom))}.dialog-actions{flex-wrap:wrap}.dialog-actions .spacer{display:none}.dialog-actions button{flex:1;min-width:105px}.recent-item{grid-template-columns:38px 1fr}.recent-item .badge{grid-column:2;justify-self:start}}\n'
APP_JS = 'const $ = s => document.querySelector(s);\nconst $$ = s => [...document.querySelectorAll(s)];\nconst state = { user:null, sponsors:[], settings:{}, packages:{Bronze:125,Silber:150,Gold:300}, benefits:{}, statuses:[], year:new Date().getFullYear(), view:\'dashboard\' };\nconst euro = n => new Intl.NumberFormat(\'de-AT\',{style:\'currency\',currency:\'EUR\'}).format(Number(n||0));\nconst esc = s => String(s??\'\').replace(/[&<>\'"]/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',"\'":\'&#39;\',\'"\':\'&quot;\'}[c]));\nfunction toast(msg){const el=$(\'#toast\');el.textContent=msg;el.classList.add(\'show\');setTimeout(()=>el.classList.remove(\'show\'),2800)}\nasync function api(url,opts={}){opts.credentials=\'same-origin\';if(opts.body && !(opts.body instanceof FormData))opts.headers={...(opts.headers||{}),\'Content-Type\':\'application/json\'};const r=await fetch(url,opts);let data={};try{data=await r.json()}catch{}if(!r.ok)throw new Error(data.error||`Fehler ${r.status}`);return data}\nfunction showLogin(){ $(\'#loginView\').classList.remove(\'hidden\'); $(\'#appView\').classList.add(\'hidden\'); }\nfunction showApp(){ $(\'#loginView\').classList.add(\'hidden\'); $(\'#appView\').classList.remove(\'hidden\'); $(\'#userName\').textContent=state.user.display_name; }\nfunction setAuthMode(mode){$(\'#loginForm\').classList.toggle(\'hidden\',mode!==\'login\');$(\'#registerForm\').classList.toggle(\'hidden\',mode!==\'register\');$(\'#showLogin\').className=mode===\'login\'?\'primary\':\'secondary\';$(\'#showRegister\').className=mode===\'register\'?\'primary\':\'secondary\'}\nasync function boot(){try{const me=await api(\'/api/me\');if(!me.user)return showLogin();state.user=me.user;showApp();await loadBase()}catch{showLogin()}}\nasync function loadBase(){const [p,s,y]=await Promise.all([api(\'/api/packages\'),api(\'/api/settings\'),api(\'/api/years\')]);state.packages=p.packages;state.benefits=p.benefits;state.statuses=p.statuses;state.settings=s.settings;const ys=new Set([state.year,...y.years]);$(\'#yearSelect\').innerHTML=[...ys].sort((a,b)=>b-a).map(v=>`<option ${v==state.year?\'selected\':\'\'}>${v}</option>`).join(\'\');$(\'#statusFilter\').innerHTML=\'<option value="">Alle Status</option>\'+state.statuses.map(v=>`<option>${esc(v)}</option>`).join(\'\');$(\'#spStatus\').innerHTML=state.statuses.map(v=>`<option>${esc(v)}</option>`).join(\'\');fillSettings();await refresh()}\nasync function refresh(){const [sp,db]=await Promise.all([api(`/api/sponsors?year=${state.year}`),api(`/api/dashboard?year=${state.year}`)]);state.sponsors=sp.sponsors;renderSponsors();renderDashboard(db)}\nfunction renderDashboard(d){$(\'#statSponsors\').textContent=d.sponsors;$(\'#statPledged\').textContent=euro(d.pledged);$(\'#statPaid\').textContent=euro(d.paid);$(\'#statOpen\').textContent=euro(d.open);const total=Math.max(1,d.sponsors);$(\'#statusBars\').innerHTML=state.statuses.map(st=>{const n=d.by_status[st]||0;return `<div class="status-row"><span>${esc(st)}</span><div class="bar"><i style="width:${(n/total)*100}%"></i></div><b>${n}</b></div>`}).join(\'\');const rec=[...state.sponsors].sort((a,b)=>String(b.updated_at).localeCompare(String(a.updated_at))).slice(0,6);$(\'#recentSponsors\').innerHTML=rec.length?rec.map(s=>`<button class="recent-item" onclick="editSponsor(${s.id})"><span class="company-bubble">${esc(s.company.slice(0,1).toUpperCase())}</span><span><b>${esc(s.company)}</b><small>${esc(s.updated_by||s.contact||\'—\')}</small></span><span class="badge ${s.paid?\'ok\':s.status===\'Nachfassen\'?\'warn\':\'\'}">${s.paid?\'Bezahlt\':esc(s.status)}</span></button>`).join(\'\'):\'<p class="muted">Noch keine Sponsoren eingetragen.</p>\'}\nfunction renderSponsors(){const q=$(\'#searchInput\').value.trim().toLowerCase();const f=$(\'#statusFilter\').value;const rows=state.sponsors.filter(s=>!q||[s.company,s.contact,s.email,s.phone].some(x=>String(x||\'\').toLowerCase().includes(q))).filter(s=>!f||s.status===f);$(\'#sponsorsBody\').innerHTML=rows.length?rows.map(s=>`<tr><td><b>${esc(s.company)}</b><small>${esc(s.contact||\'\')}</small></td><td><span class="badge">${esc(s.package)}</span></td><td>${euro(s.amount)}</td><td>${esc(s.status)}</td><td><span class="badge ${s.paid?\'ok\':\'warn\'}">${s.paid?\'Bezahlt\':\'Offen\'}</span></td><td>${esc(s.invoice_number||\'—\')}</td><td><button class="secondary compact" onclick="editSponsor(${s.id})">Öffnen</button></td></tr>`).join(\'\'):\'<tr><td colspan="7" class="muted">Keine Einträge gefunden.</td></tr>\'}\nfunction setView(v){state.view=v;$$(\'.view\').forEach(x=>x.classList.add(\'hidden\'));$(`#${v}View`).classList.remove(\'hidden\');$$(\'.nav-btn\').forEach(x=>x.classList.toggle(\'active\',x.dataset.view===v));$(\'#pageTitle\').textContent={dashboard:\'Sponsoring im Überblick\',sponsors:\'Sponsoren verwalten\',settings:\'Einstellungen\'}[v];$(\'#pageSub\').textContent=state.settings.event_name||\'Jährliches Feuerwehrfest\'}\nfunction fillSettings(){const s=state.settings;$(\'#setOrganization\').value=s.organization||\'\';$(\'#setEventName\').value=s.event_name||\'\';$(\'#setAddress\').value=s.address||\'\';$(\'#setEmail\').value=s.email||\'\';$(\'#setPhone\').value=s.phone||\'\';$(\'#setIban\').value=s.iban||\'\';$(\'#setBic\').value=s.bic||\'\';$(\'#setBank\').value=s.bank||\'\';$(\'#setPrefix\').value=s.invoice_prefix||\'\';$(\'#setDueDays\').value=s.default_due_days||14;$(\'#setTaxNote\').value=s.tax_note||\'\'}\nfunction openSponsor(s=null){$(\'#sponsorForm\').reset();$(\'#spId\').value=s?.id||\'\';$(\'#dialogTitle\').textContent=s?\'Sponsor bearbeiten\':\'Sponsor hinzufügen\';$(\'#spCompany\').value=s?.company||\'\';$(\'#spContact\').value=s?.contact||\'\';$(\'#spPhone\').value=s?.phone||\'\';$(\'#spEmail\').value=s?.email||\'\';$(\'#spAddress\').value=s?.address||\'\';const pack=s?.package||\'Bronze\';$(\'#spPackage\').value=pack;$(\'#spAmount\').value=s?.amount??state.packages[pack];$(\'#spStatus\').value=s?.status||\'Noch nicht kontaktiert\';$(\'#spPaid\').checked=!!s?.paid;$(\'#paidLabel\').textContent=$(\'#spPaid\').checked?\'Ja\':\'Nein\';$(\'#spPaymentDate\').value=s?.payment_date||\'\';$(\'#spDueDate\').value=s?.due_date||\'\';$(\'#spBenefits\').value=s?.benefits||state.benefits[pack]||\'\';$(\'#spNotes\').value=s?.notes||\'\';$(\'#deleteSponsor\').classList.toggle(\'hidden\',!s);$(\'#documentActions\').classList.toggle(\'hidden\',!s);$(\'#sponsorDialog\').showModal()}\nwindow.editSponsor=id=>openSponsor(state.sponsors.find(x=>x.id===id));\nfunction formSponsor(){return {year:state.year,company:$(\'#spCompany\').value,contact:$(\'#spContact\').value,phone:$(\'#spPhone\').value,email:$(\'#spEmail\').value,address:$(\'#spAddress\').value,package:$(\'#spPackage\').value,amount:Number($(\'#spAmount\').value||0),status:$(\'#spStatus\').value,paid:$(\'#spPaid\').checked,payment_date:$(\'#spPaymentDate\').value,due_date:$(\'#spDueDate\').value,benefits:$(\'#spBenefits\').value,notes:$(\'#spNotes\').value}}\nasync function saveSponsor(ev){ev.preventDefault();const id=$(\'#spId\').value;const payload=formSponsor();try{id?await api(`/api/sponsors/${id}`,{method:\'PUT\',body:JSON.stringify(payload)}):await api(\'/api/sponsors\',{method:\'POST\',body:JSON.stringify(payload)});$(\'#sponsorDialog\').close();await refresh();toast(\'Sponsor gespeichert\')}catch(e){toast(e.message)}}\nasync function ensureInvoice(s){if(s.invoice_number)return s;const d=await api(`/api/sponsors/${s.id}/invoice-number`,{method:\'POST\',body:\'{}\'});await refresh();return state.sponsors.find(x=>x.id===s.id)||{...s,...d}}\nfunction printDoc(s,isReceipt=false){const st=state.settings;if(!st.address||!st.iban){toast(\'Bitte zuerst Adresse und IBAN in den Einstellungen ergänzen.\');return}const w=window.open(\'\',\'_blank\');if(!w){toast(\'Pop-up wurde blockiert.\');return}const dateText=(isReceipt?s.payment_date:s.invoice_date)||new Date().toISOString().slice(0,10);const heading=isReceipt?\'Zahlungsbestätigung\':\'Rechnung\';const number=isReceipt?`Bestätigung zu ${s.invoice_number||\'Sponsoring\'}`:(s.invoice_number||\'\');const tax=st.tax_note?`<p style="margin-top:24px">${esc(st.tax_note).replace(/\\n/g,\'<br>\')}</p>`:\'\';const paidText=isReceipt?`Hiermit bestätigen wir den Zahlungseingang über <b>${euro(s.amount)}</b> für das Sponsoring-Paket <b>${esc(s.package)}</b>.`:`Bitte überweisen Sie <b>${euro(s.amount)}</b> unter Angabe der Rechnungsnummer auf das untenstehende Konto.`;const benefits=s.benefits?`<p><b>Gegenleistungen:</b><br>${esc(s.benefits).replace(/\\n/g,\'<br>\')}</p>`:\'\';w.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${heading} ${esc(number)}</title><style>body{font-family:Arial,sans-serif;color:#222;margin:52px;line-height:1.45}.head{display:flex;justify-content:space-between;border-bottom:3px solid #a71d2a;padding-bottom:20px;margin-bottom:34px}.org{font-size:22px;font-weight:700}.muted{color:#666}h1{font-size:30px;margin:30px 0 6px}table{width:100%;border-collapse:collapse;margin:25px 0}td{padding:12px;border-bottom:1px solid #ddd}td:last-child{text-align:right}.total td{font-weight:700;border-top:2px solid #222}.foot{margin-top:50px;font-size:13px;color:#555}</style></head><body><div class="head"><div><div class="org">${esc(st.organization)}</div><div class="muted">${esc(st.event_name)}</div></div><div style="text-align:right">${esc(st.address).replace(/\\n/g,\'<br>\')}<br>${esc(st.email)} ${esc(st.phone)}</div></div><div>${esc(s.company)}<br>${esc(s.contact||\'\')}<br>${esc(s.address||\'\')}</div><h1>${heading}</h1><div class="muted">${esc(number)} · Datum: ${esc(dateText)}</div><p>Vielen Dank für die Unterstützung unseres Feuerwehrfestes.</p><table><tr><td>Sponsoring-Paket ${esc(s.package)}</td><td>${euro(s.amount)}</td></tr><tr class="total"><td>Gesamt</td><td>${euro(s.amount)}</td></tr></table><p>${paidText}</p>${benefits}${!isReceipt&&s.due_date?`<p>Fällig am: <b>${esc(s.due_date)}</b></p>`:\'\'}${tax}<div class="foot"><b>${esc(st.organization)}</b><br>${esc(st.bank)} · IBAN ${esc(st.iban)}${st.bic?\' · BIC \'+esc(st.bic):\'\'}</div><script>window.onload=()=>window.print()<\\/script></body></html>`);w.document.close()}\nasync function invoiceCurrent(){let s=state.sponsors.find(x=>x.id==$(\'#spId\').value);if(!s)return;try{s=await ensureInvoice(s);printDoc(s,false)}catch(e){toast(e.message)}}\nasync function receiptCurrent(){let s=state.sponsors.find(x=>x.id==$(\'#spId\').value);if(!s)return;if(!s.paid){toast(\'Sponsor zuerst als bezahlt markieren und speichern.\');return}try{s=await ensureInvoice(s);printDoc(s,true)}catch(e){toast(e.message)}}\n$(\'#showLogin\').onclick=()=>setAuthMode(\'login\');$(\'#showRegister\').onclick=()=>setAuthMode(\'register\');\n$(\'#loginForm\').addEventListener(\'submit\',async e=>{e.preventDefault();try{const d=await api(\'/api/login\',{method:\'POST\',body:JSON.stringify({email:$(\'#loginEmail\').value,password:$(\'#loginPassword\').value})});state.user=d.user;showApp();await loadBase()}catch(err){toast(err.message)}});\n$(\'#registerForm\').addEventListener(\'submit\',async e=>{e.preventDefault();try{const d=await api(\'/api/register\',{method:\'POST\',body:JSON.stringify({display_name:$(\'#regName\').value,email:$(\'#regEmail\').value,password:$(\'#regPassword\').value})});state.user=d.user;showApp();await loadBase()}catch(err){toast(err.message)}});\n$(\'#logoutBtn\').onclick=async()=>{await api(\'/api/logout\',{method:\'POST\',body:\'{}\'});state.user=null;showLogin()};\n$$(\'.nav-btn\').forEach(b=>b.addEventListener(\'click\',()=>setView(b.dataset.view)));$(\'#goSponsors\').onclick=()=>setView(\'sponsors\');$(\'#addSponsorTop\').onclick=()=>openSponsor();$(\'#addSponsorBtn\').onclick=()=>openSponsor();$(\'#closeDialog\').onclick=()=>$(\'#sponsorDialog\').close();$(\'#closeDialog2\').onclick=()=>$(\'#sponsorDialog\').close();$(\'#sponsorForm\').addEventListener(\'submit\',saveSponsor);\n$(\'#spPackage\').addEventListener(\'change\',()=>{const p=$(\'#spPackage\').value;$(\'#spAmount\').value=state.packages[p];$(\'#spBenefits\').value=state.benefits[p]||\'\'});\n$(\'#spPaid\').addEventListener(\'change\',()=>{$(\'#paidLabel\').textContent=$(\'#spPaid\').checked?\'Ja\':\'Nein\';if($(\'#spPaid\').checked&&!$(\'#spPaymentDate\').value)$(\'#spPaymentDate\').value=new Date().toISOString().slice(0,10)});\n$(\'#searchInput\').addEventListener(\'input\',renderSponsors);$(\'#statusFilter\').addEventListener(\'change\',renderSponsors);$(\'#yearSelect\').addEventListener(\'change\',async()=>{state.year=Number($(\'#yearSelect\').value);await refresh()});$(\'#exportBtn\').onclick=()=>location.href=`/api/export.csv?year=${state.year}`;\n$(\'#deleteSponsor\').onclick=async()=>{const id=$(\'#spId\').value;if(!id||!confirm(\'Sponsor wirklich löschen?\'))return;try{await api(`/api/sponsors/${id}`,{method:\'DELETE\'});$(\'#sponsorDialog\').close();await refresh();toast(\'Sponsor gelöscht\')}catch(e){toast(e.message)}};\n$(\'#printInvoice\').onclick=invoiceCurrent;$(\'#printReceipt\').onclick=receiptCurrent;\n$(\'#rolloverBtn\').onclick=async()=>{const to=Number(prompt(`Sponsoren von ${state.year} in welches Jahr übernehmen?`,state.year+1));if(!to)return;try{const d=await api(\'/api/rollover\',{method:\'POST\',body:JSON.stringify({from_year:state.year,to_year:to})});toast(`${d.count} Sponsoren nach ${to} übernommen`);await loadBase()}catch(e){toast(e.message)}};\n$(\'#settingsForm\').addEventListener(\'submit\',async e=>{e.preventDefault();const settings={organization:$(\'#setOrganization\').value,event_name:$(\'#setEventName\').value,address:$(\'#setAddress\').value,email:$(\'#setEmail\').value,phone:$(\'#setPhone\').value,iban:$(\'#setIban\').value,bic:$(\'#setBic\').value,bank:$(\'#setBank\').value,invoice_prefix:$(\'#setPrefix\').value,default_due_days:Number($(\'#setDueDays\').value||14),tax_note:$(\'#setTaxNote\').value};try{await api(\'/api/settings\',{method:\'PUT\',body:JSON.stringify({settings})});state.settings=settings;setView(state.view);toast(\'Einstellungen gespeichert\')}catch(e){toast(e.message)}});\n$(\'#passwordForm\').addEventListener(\'submit\',async e=>{e.preventDefault();try{await api(\'/api/change-password\',{method:\'POST\',body:JSON.stringify({current_password:$(\'#pwCurrent\').value,new_password:$(\'#pwNew\').value})});e.target.reset();toast(\'Passwort geändert\')}catch(err){toast(err.message)}});\nboot();\n'
MANIFEST_JSON = '{\n  "name": "FF Niederndorf Sponsoring",\n  "short_name": "FF Sponsoring",\n  "start_url": "/",\n  "display": "standalone",\n  "background_color": "#f5f1eb",\n  "theme_color": "#a71d2a",\n  "description": "Sponsorenverwaltung für das jährliche Feuerwehrfest der Feuerwehr Niederndorf"\n}\n'

@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")

@app.get("/styles.css")
def styles_css():
    return Response(STYLES_CSS, mimetype="text/css")

@app.get("/app.js")
def app_js():
    return Response(APP_JS, mimetype="application/javascript")

@app.get("/manifest.json")
def manifest_json():
    return Response(MANIFEST_JSON, mimetype="application/manifest+json")


# Render/Gunicorn imports this module. Initialize the schema at startup.
if DATABASE_URL:
    init_db()

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
