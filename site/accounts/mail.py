"""Emails from no-reply@belcentra.com through Resend; without an API key they are only logged."""
import json
import urllib.request

from flask import current_app


def send(to, subject, text):
    key = current_app.config.get("RESEND_API_KEY")
    if not key:
        current_app.logger.info("Email not sent (no RESEND_API_KEY): to=%s subject=%s", to, subject)
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
