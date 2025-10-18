from flask import Flask, request, jsonify, Response
import imaplib
import email
import uuid
import time
import re
from threading import Lock

app = Flask(__name__)

@app.after_request
def disable_compression(response):
    response.headers["Content-Encoding"] = "identity"
    return response
# Thread-safe session storage
imap_sessions = {}
session_lock = Lock()

IMAP_CONFIG = {
    "t-online.de": ("secureimap.t-online.de", 993),
    "freenet.de": ("imap.freenet.de", 993)
}

@app.route("/login", methods=["GET"])
def login():
    email_address = request.args.get("email")
    password = request.args.get("password")

    if not email_address or not password:
        return jsonify({"error": "Missing email or password"}), 400

    domain = email_address.split('@')[-1].lower()
    if domain not in IMAP_CONFIG:
        return jsonify({"error": "Unsupported email domain"}), 400

    imap_server, imap_port = IMAP_CONFIG[domain]

    try:
        mail = imaplib.IMAP4_SSL(imap_server, imap_port)
        mail.login(email_address, password)
        mail.noop()  # Force connection alive
        mail.socket().settimeout(5)  # 5-second timeout

        session_id = str(uuid.uuid4())
        with session_lock:
            imap_sessions[session_id] = {
                "mail": mail,
                "email": email_address,
                "password": password,
                "last_activity": time.time()
            }

        return jsonify({"session_id": session_id, "status": "ready"})

    except imaplib.IMAP4.error:
        return jsonify({"error": "Login failed"}), 403
    except Exception:
        return jsonify({"error": "Server error"}), 500

@app.route("/inbox/latest", methods=["GET"])
def get_latest_email():
    session_id = request.args.get("session_id")
    if not session_id:
        return Response("error: missing session_id\n", status=400, mimetype="text/plain")

    with session_lock:
        session = imap_sessions.get(session_id)
        if not session:
            return Response("error: invalid or expired session\n", status=401, mimetype="text/plain")

        mail = session["mail"]
        email_address = session["email"]
        password = session["password"]

    def reconnect():
        domain = email_address.split('@')[-1].lower()
        imap_server, imap_port = IMAP_CONFIG[domain]
        new_mail = imaplib.IMAP4_SSL(imap_server, imap_port)
        new_mail.login(email_address, password)
        new_mail.socket().settimeout(5)
        with session_lock:
            imap_sessions[session_id]["mail"] = new_mail
        return new_mail

    try:
        try:
            mail.noop()  # Check if alive
        except:
            mail = reconnect()

        mail.select("INBOX", readonly=True)
        status, messages = mail.search(None, '(FROM "no-reply@lieferando.de")')

        if status != "OK" or not messages[0]:
            print("⚠️ No emails found.")
            return Response("no emails found\n", status=404, mimetype="text/plain")

        latest_id = messages[0].split()[-1]
        status, data = mail.fetch(latest_id, "(RFC822)")

        if status != "OK":
            print("⚠️ Failed to fetch email.")
            return Response("failed to fetch email\n", status=500, mimetype="text/plain")

        msg = email.message_from_bytes(data[0][1])
        html = None

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    html = part.get_payload(decode=True)
                    if html:
                        html = html.decode(part.get_content_charset() or "utf-8", errors="ignore")
                    break
        elif msg.get_content_type() == "text/html":
            html = msg.get_payload(decode=True)
            if html:
                html = html.decode(msg.get_content_charset() or "utf-8", errors="ignore")

        if not html:
            print("❌ No HTML content found in email.")
            return Response("no html content found\n", status=404, mimetype="text/plain")

        match = re.search(
            r'<td[^>]*class=["\']?code-text["\']?[^>]*>\s*([A-Z0-9]{6})\s*<',
            html,
            re.IGNORECASE
        )

        if match:
            code = match.group(1).strip()
            print(f"✅ Code found: {code}")
            return Response(f"code found: {code}\n", status=200, mimetype="text/plain")
        else:
            print("⚠️ Verification code not found.")
            return Response("no code found, reload page\n", status=200, mimetype="text/plain")

    except imaplib.IMAP4.abort:
        print("❌ IMAP connection lost.")
        return Response("connection lost\n", status=503, mimetype="text/plain")
    except Exception as e:
        import traceback
        print("🔥 Server error:", e)
        traceback.print_exc()
        return Response("server error\n", status=500, mimetype="text/plain")

# Background session cleanup
def cleanup_sessions():
    while True:
        time.sleep(60)
        now = time.time()
        expired = []
        with session_lock:
            for session_id, session in list(imap_sessions.items()):
                if now - session["last_activity"] > 60:  # 30 min expiry
                    try:
                        session["mail"].logout()
                    except:
                        pass
                    expired.append(session_id)
            for session_id in expired:
                del imap_sessions[session_id]

# Start cleanup thread
import threading
threading.Thread(target=cleanup_sessions, daemon=True).start()
