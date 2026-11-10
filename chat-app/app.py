import base64, os, re, secrets, sqlite3, time, urllib.parse, urllib.request
from flask import Flask, jsonify, redirect, render_template, request, session
from flask_socketio import SocketIO, emit, join_room

DATABASE_URL = os.environ.get("DATABASE_URL")  # set on the host -> PostgreSQL; empty -> local SQLite
if DATABASE_URL:
    import psycopg2, psycopg2.extras

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("COOKIE_SECURE")))
socketio = SocketIO(app, async_mode="threading")


class DB:
    """Tiny wrapper so the same queries work on SQLite and PostgreSQL."""
    def __enter__(self):
        if DATABASE_URL:
            self.c = psycopg2.connect(DATABASE_URL)
            self.cur = self.c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            self.c = sqlite3.connect("chat.db")
            self.c.row_factory = sqlite3.Row
            self.cur = self.c.cursor()
        return self

    def execute(self, sql, params=()):
        self.cur.execute(sql.replace("?", "%s") if DATABASE_URL else sql, params)
        return self.cur

    def __exit__(self, et, ev, tb):
        self.c.rollback() if et else self.c.commit()
        self.c.close()


ID = "SERIAL PRIMARY KEY" if DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"
NUM = "DOUBLE PRECISION" if DATABASE_URL else "REAL"
with DB() as c:
    c.execute(f"CREATE TABLE IF NOT EXISTS users(phone TEXT PRIMARY KEY, created {NUM})")
    c.execute(f"CREATE TABLE IF NOT EXISTS otps(phone TEXT PRIMARY KEY, code TEXT, expires {NUM}, tries INTEGER DEFAULT 0)")
    c.execute(f"CREATE TABLE IF NOT EXISTS messages(id {ID}, sender TEXT, receiver TEXT, body TEXT, ts {NUM})")


def clean(p):
    d = re.sub(r"\D", "", p or "")
    return d if 10 <= len(d) <= 15 else None


def me():
    return session.get("phone")


def send_sms(phone, code):
    sid, token, sender = (os.environ.get(k) for k in ("TWILIO_SID", "TWILIO_TOKEN", "TWILIO_FROM"))
    if not (sid and token and sender):
        # No SMS provider set: OTP is printed in the server logs (demo only).
        print(f"[DEV] OTP for {phone}: {code}", flush=True)
        return
    cc = os.environ.get("DEFAULT_COUNTRY", "91")
    to = "+" + (cc + phone if len(phone) == 10 else phone)
    data = urllib.parse.urlencode({"To": to, "From": sender, "Body": f"Your login OTP is {code}"}).encode()
    req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print("SMS failed:", e, flush=True)


@app.get("/")
def home():
    return redirect("/chat" if me() else "/login")


@app.get("/login")
def login_page():
    return render_template("login.html")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.post("/api/send-otp")
def send_otp():
    phone = clean((request.json or {}).get("phone"))
    if not phone:
        return jsonify(error="Enter a valid phone number"), 400
    with DB() as c:
        row = c.execute("SELECT expires FROM otps WHERE phone=?", (phone,)).fetchone()
        if row and row["expires"] - 240 > time.time():
            return jsonify(error="Wait a minute before asking for a new OTP"), 429
        code = f"{secrets.randbelow(10**6):06d}"
        c.execute("""INSERT INTO otps(phone,code,expires,tries) VALUES(?,?,?,0)
                     ON CONFLICT(phone) DO UPDATE SET code=excluded.code, expires=excluded.expires, tries=0""",
                  (phone, code, time.time() + 300))
    send_sms(phone, code)
    return jsonify(ok=True)


@app.post("/api/verify-otp")
def verify_otp():
    d = request.json or {}
    phone, code = clean(d.get("phone")), str(d.get("code", "")).strip()
    with DB() as c:
        row = c.execute("SELECT * FROM otps WHERE phone=?", (phone,)).fetchone()
        if not row or row["expires"] < time.time() or row["tries"] >= 5:
            return jsonify(error="OTP expired. Ask for a new one."), 400
        if code != row["code"]:
            c.execute("UPDATE otps SET tries=tries+1 WHERE phone=?", (phone,))
            return jsonify(error="Wrong OTP"), 400
        c.execute("DELETE FROM otps WHERE phone=?", (phone,))
        c.execute("INSERT INTO users(phone,created) VALUES(?,?) ON CONFLICT DO NOTHING", (phone, time.time()))
    session["phone"] = phone
    return jsonify(ok=True)


@app.get("/chat")
def chat_page():
    if not me():
        return redirect("/login")
    return render_template("chat.html", me=me())


@app.get("/api/chats")
def chats():
    if not me():
        return jsonify([]), 401
    with DB() as c:
        rows = c.execute(
            """SELECT CASE WHEN sender=? THEN receiver ELSE sender END AS other, MAX(ts) AS t
               FROM messages WHERE sender=? OR receiver=? GROUP BY other ORDER BY t DESC""",
            (me(), me(), me())).fetchall()
    return jsonify([r["other"] for r in rows])


@app.get("/api/user/<phone>")
def find_user(phone):
    p = clean(phone)
    with DB() as c:
        ok = p and c.execute("SELECT 1 FROM users WHERE phone=?", (p,)).fetchone()
    if not ok:
        return jsonify(error="This number hasn't joined yet"), 404
    return jsonify(phone=p)


@app.get("/api/messages/<other>")
def history(other):
    if not me():
        return jsonify([]), 401
    with DB() as c:
        rows = c.execute(
            """SELECT sender, receiver, body, ts FROM messages
               WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?) ORDER BY id""",
            (me(), other, other, me())).fetchall()
    return jsonify([dict(r) for r in rows])


@socketio.on("connect")
def on_connect():
    if not me():
        return False
    join_room(me())


@socketio.on("send")
def on_send(d):
    to, body = clean(d.get("to")), (d.get("body") or "").strip()[:2000]
    if not me() or not to or to == me() or not body:
        return
    with DB() as c:
        if not c.execute("SELECT 1 FROM users WHERE phone=?", (to,)).fetchone():
            return
        ts = time.time()
        c.execute("INSERT INTO messages(sender,receiver,body,ts) VALUES(?,?,?,?)", (me(), to, body, ts))
    m = dict(sender=me(), receiver=to, body=body, ts=ts)
    emit("message", m, to=to)
    emit("message", m, to=me())


if __name__ == "__main__":
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)
