"""Emails from no-reply@belcentra.com through Resend. Nothing is sent unless MAIL_ENABLED=1 and an API key is set."""
import json
import urllib.request

from flask import current_app


def send(to, subject, text):
    key = current_app.config.get("RESEND_API_KEY")
    if not (key and current_app.config.get("MAIL_ENABLED")):
        current_app.logger.info("Email not sent (email is off): subject=%s", subject)
        current_app.extensions.setdefault("sent_mail", []).append({"to": to, "subject": subject, "text": text})
        return False
    body = json.dumps({"from": current_app.config["MAIL_FROM"], "to": [to], "subject": subject, "text": text}).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:  # never let an email failure break sign-in
        current_app.logger.exception("Email to %s failed", to)
        return False
