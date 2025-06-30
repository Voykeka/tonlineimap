from flask import Flask, request, jsonify
import imaplib
import email
import uuid
import time
import logging
import re

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

imap_sessions = {}

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
        logger.info(f"Connecting to {imap_server}:{imap_port} for {email_address}")
        mail = imaplib.IMAP4_SSL(imap_server, imap_port)
        mail.login(email_address, password)
        logger.info(f"Login successful for {email_address}")

        session_id = str(uuid.uuid4())
        imap_sessions[session_id] = mail

        return jsonify({
            "session_id": session_id,
            "status": "ready",
            "message": "Login successful"
        })

    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP login failed for {email_address}: {str(e)}")
        return jsonify({"error": "Login failed", "detail": "Invalid credentials or IMAP access not enabled"}), 403
    except Exception as e:
        logger.error(f"Unexpected error during login for {email_address}: {str(e)}")
        return jsonify({"error": "Login failed", "detail": str(e)}), 500

@app.route("/inbox/latest", methods=["GET"])
def get_latest_email():
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing session_id parameter"}), 400

    mail = imap_sessions.get(session_id)
    if not mail:
        return jsonify({"error": "Invalid or expired session"}), 401

    refresh_count = 3
    wait_seconds = 2

    def extract_verification_code(html):
        match = re.search(r'<span style="color: #FB6100;">\s*([A-Z0-9]{6})\s*</span>', html)
        return match.group(1).strip() if match else None

    try:
        for attempt in range(1, refresh_count + 1):
            logger.info(f"Attempt {attempt} to fetch email")
            try:
                mail.select("INBOX", readonly=True)
                status, messages = mail.search(None, '(FROM "no-reply@lieferando.de")')

                if status == "OK" and messages[0]:
                    latest_id = messages[0].split()[-1]
                    status, data = mail.fetch(latest_id, "(RFC822)")

                    if status == "OK":
                        msg = email.message_from_bytes(data[0][1])
                        html = None
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/html":
                                    html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                    break
                        elif msg.get_content_type() == "text/html":
                            html = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                        if html:
                            code = extract_verification_code(html)
                            if code:
                                return code, 200, {"Content-Type": "text/plain"}

                if attempt < refresh_count:
                    time.sleep(wait_seconds)

            except Exception as e:
                logger.error(f"Error during attempt {attempt}: {str(e)}")
                if attempt == refresh_count:
                    raise
                time.sleep(wait_seconds)

        return jsonify({"error": "No verification code found after 3 attempts", "attempts": refresh_count}), 404

    except Exception as e:
        logger.error(f"Failed to process email: {str(e)}")
        return jsonify({"error": "Email processing failed", "detail": str(e)}), 500
