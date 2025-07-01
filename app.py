from flask import Flask, request, jsonify
import imaplib
import email
import uuid
import time
import logging
import re
from threading import Lock

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Thread-safe session storage
imap_sessions = {}
session_lock = Lock()

IMAP_CONFIG = {
    "t-online.de": ("secureimap.t-online.de", 993),
    "freenet.de": ("imap.freenet.de", 993)
}

# Keep connections alive by sending NOOP every ~2 mins
def keep_alive(mail):
    try:
        mail.noop()
    except Exception as e:
        logger.warning(f"IMAP keep-alive failed: {e}")
        raise

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
        logger.info(f"Connecting to {imap_server}:{imap_port}")
        mail = imaplib.IMAP4_SSL(imap_server, imap_port)
        mail.login(email_address, password)
        
        # Set timeout to prevent hanging
        mail.socket().settimeout(30)  # 30 sec timeout

        session_id = str(uuid.uuid4())
        with session_lock:
            imap_sessions[session_id] = {
                "mail": mail,
                "email": email_address,
                "password": password,  # Stored for reconnection
                "last_activity": time.time()
            }

        return jsonify({
            "session_id": session_id,
            "status": "ready"
        })

    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP login failed: {e}")
        return jsonify({"error": "Login failed"}), 403
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
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
        logger.info("Attempting to reconnect...")
        domain = email_address.split('@')[-1].lower()
        imap_server, imap_port = IMAP_CONFIG[domain]
        new_mail = imaplib.IMAP4_SSL(imap_server, imap_port)
        new_mail.login(email_address, password)
        new_mail.socket().settimeout(30)
        with session_lock:
            imap_sessions[session_id]["mail"] = new_mail
        return new_mail

    try:
        # Check if connection is alive
        try:
            keep_alive(mail)
        except:
            mail = reconnect()  # Auto-reconnect if dead

        mail.select("INBOX", readonly=True)
        status, messages = mail.search(None, '(FROM "no-reply@lieferando.de")')

        if status != "OK" or not messages[0]:
            return jsonify({"error": "No emails found"}), 404

        latest_id = messages[0].split()[-1]
        status, data = mail.fetch(latest_id, "(RFC822)")

        if status != "OK":
            return jsonify({"error": "Failed to fetch email"}), 500

        msg = email.message_from_bytes(data[0][1])
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

        # Extract verification code
        match = re.search(r'<span style="color: #FB6100;">\s*([A-Z0-9]{6})\s*</span>', html)
        if not match:
            return jsonify({"error": "Verification code not found"}), 404

        code = match.group(1).strip()
        return code, 200, {"Content-Type": "text/plain"}

    except imaplib.IMAP4.abort:
        logger.error("IMAP connection aborted")
        return jsonify({"error": "Connection lost"}), 503
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return jsonify({"error": "Server error"}), 500

# Cleanup old sessions (run in background)
def cleanup_sessions():
    while True:
        time.sleep(60)  # Run every minute
        now = time.time()
        expired = []
        with session_lock:
            for session_id, session in list(imap_sessions.items()):
                if now - session["last_activity"] > 1800:  # 30 min expiry
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
