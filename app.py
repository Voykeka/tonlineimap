from flask import Flask, request, jsonify, Response
import imaplib
import email
import uuid
import time
import re
from threading import Lock, Thread

app = Flask(__name__)

imap_sessions = {}
session_lock = Lock()

IMAP_CONFIG = {
    "t-online.de": ("secureimap.t-online.de", 993),
    "freenet.de": ("imap.freenet.de", 993)
}

CODE_CACHE_SECONDS = 60  # cache code for 1 minute

def fetch_latest_code(session_id):
    """Background thread: fetch latest email and extract code"""
    with session_lock:
        session = imap_sessions.get(session_id)
        if not session:
            return
        mail = session["mail"]
        email_address = session["email"]
        password = session["password"]

    try:
        try:
            mail.noop()
        except:
            # reconnect if IMAP died
            domain = email_address.split('@')[-1].lower()
            imap_server, imap_port = IMAP_CONFIG[domain]
            mail = imaplib.IMAP4_SSL(imap_server, imap_port)
            mail.login(email_address, password)
            mail.socket().settimeout(5)
            with session_lock:
                imap_sessions[session_id]["mail"] = mail

        mail.select("INBOX", readonly=True)
        status, messages = mail.search(None, "ALL")  # search all emails

        if status != "OK" or not messages[0]:
            return

        # find the latest matching email
        latest_id = None
        for num in reversed(messages[0].split()):
            status, data = mail.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            # match emails from sender
            if "no-reply" in (msg["From"] or "").lower() and "burgerking" in (msg["From"] or "").lower():
                latest_id = num
                break

        if not latest_id:
            return

        status, data = mail.fetch(latest_id, "(RFC822)")
        msg = email.message_from_bytes(data[0][1])

        # extract HTML
        html = None
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    break
        elif msg.get_content_type() == "text/html":
            html = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

        code = None
        if html:
            match = re.search(
                r'<td[^>]*class=["\']?code-text["\']?[^>]*>\s*([A-Z0-9]{6})\s*<',
                html,
                re.IGNORECASE
            )
            if match:
                code = match.group(1).strip()

        if code:
            with session_lock:
                imap_sessions[session_id]["latest_code"] = code
                imap_sessions[session_id]["latest_code_time"] = time.time()

    except Exception:
        pass  # ignore background fetch errors

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
                "last_activity": time.time(),
                "latest_code": None,
                "latest_code_time": 0
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

        # update last activity
        session["last_activity"] = time.time()
        code = session.get("latest_code")
        code_time = session.get("latest_code_time", 0)

    # If cached code is fresh, return immediately
    if code and (time.time() - code_time) < CODE_CACHE_SECONDS:
        return Response(code + "\n", status=200, mimetype="text/plain")

    # Otherwise, fetch in background
    Thread(target=fetch_latest_code, args=(session_id,), daemon=True).start()
    # immediately respond
    return Response("processing…\n", status=202, mimetype="text/plain")

# Cleanup thread
def cleanup_sessions():
    while True:
        time.sleep(60)
        now = time.time()
        expired = []
        with session_lock:
            for session_id, session in list(imap_sessions.items()):
                if now - session["last_activity"] > 300:  # 5 min expiry
                    try:
                        session["mail"].logout()
                    except:
                        pass
                    expired.append(session_id)
            for session_id in expired:
                del imap_sessions[session_id]

import threading
threading.Thread(target=cleanup_sessions, daemon=True).start()
