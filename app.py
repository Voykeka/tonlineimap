from flask import Flask, request, jsonify
import imaplib
import email
import uuid
import time
import re
from threading import Lock
import threading

app = Flask(__name__)

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
        mail.noop()
        mail.socket().settimeout(5)

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
        return jsonify({"error": "Missing session_id"}), 400

    with session_lock:
        session = imap_sessions.get(session_id)
        if not session:
            return jsonify({"error": "Invalid or expired session"}), 401

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
            mail.noop()
        except:
            mail = reconnect()

        mail.select("INBOX", readonly=True)
        status, messages = mail.search(None, '(FROM "no-reply@lieferando.de")')

        if status != "OK" or not messages[0]:
            return jsonify({"error": "No emails found"}), 404

        latest_id = messages[0].split()[-1]
        status, data = mail.fetch(latest_id, "(RFC822)")

        if status != "OK":
            return jsonify({"error": "Failed to fetch email"}), 500

        msg = email.message_from_bytes(data[0][1])

        # Extract HTML body
        html = None
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    break
        elif msg.get_content_type() == "text/html":
            html = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

        if not html:
            return jsonify({"error": "No HTML content found"}), 404

        # Lieferando regex for <td class="code-text">CODE</td>
        match = re.search(
            r'<td[^>]*class=["\']?code-text["\']?[^>]*>\s*([A-Z0-9]{6})\s*</td>',
            html,
            re.IGNORECASE
        )
        if not match:
            return jsonify({"error": "Verification code not found"}), 404

        code = match.group(1).strip()
        return code, 200, {"Content-Type": "text/plain"}

    except imaplib.IMAP4.abort:
        return jsonify({"error": "Connection lost"}), 503
    except Exception:
        return jsonify({"error": "Server error"}), 500


# Background session cleanup
def cleanup_sessions():
    while True:
        time.sleep(60)
        now = time.time()
        expired = []
        with session_lock:
            for session_id, session in list(imap_sessions.items()):
                if now - session["last_activity"] > 60:  # 1 min expiry
                    try:
                        session["mail"].logout()
                    except:
                        pass
                    expired.append(session_id)
            for session_id in expired:
                del imap_sessions[session_id]

threading.Thread(target=cleanup_sessions, daemon=True).start()
