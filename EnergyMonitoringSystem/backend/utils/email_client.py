import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List
from backend.dal.database import db_helper

# Unified SMTP configuration from root .env
SMTP_HOST = os.getenv("SMTP_SERVER") or os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", os.getenv("SMTP_SERVER_PORT", "587")))
SMTP_USE_SSL = (os.getenv("SMTP_USE_SSL", "false").lower() in ("1", "true", "yes")) or (str(SMTP_PORT) == "465")
SMTP_USER = os.getenv("SMTP_USERNAME") or os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_FROM") or (SMTP_USER or "noreply@example.com")
ALERTS_ENABLED = os.getenv("ALERTS_ENABLED", "false").lower() in ("1", "true", "yes")


def send_email(subject: str, body: str, to_emails: List[str], html: bool = False) -> bool:
    if not ALERTS_ENABLED:
        return False
    if not SMTP_HOST or not SMTP_PORT or not SMTP_FROM or not to_emails:
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(to_emails)

    if html:
        msg.attach(MIMEText(body, "html"))
    else:
        msg.attach(MIMEText(body, "plain"))

    # Build metadata safely (no f-string brace explosion)
    metadata_success = '{"to": "%s", "success": true}' % ",".join(to_emails)
    metadata_fail = '{"to": "%s", "success": false}' % ",".join(to_emails)

    try:
        # SSL or TLS handling
        if SMTP_USE_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                if SMTP_USER and SMTP_PASSWORD:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, to_emails, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                server.ehlo()
                try:
                    server.starttls()
                except Exception:
                    pass
                if SMTP_USER and SMTP_PASSWORD:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_FROM, to_emails, msg.as_string())

        # Log success
        try:
            db_helper.execute_query(
                """
                INSERT INTO ops.Events (Level, EventType, Message, Source, MetaData, Timestamp)
                VALUES ('INFO', 'email_send', ?, 'email', ?, GETUTCDATE())
                """,
                (subject, metadata_success),
            )
        except Exception:
            pass

        return True

    except Exception:
        # Log failure
        try:
            db_helper.execute_query(
                """
                INSERT INTO ops.Events (Level, EventType, Message, Source, MetaData, Timestamp)
                VALUES ('ERROR', 'email_send', ?, 'email', ?, GETUTCDATE())
                """,
                (subject, metadata_fail),
            )
        except Exception:
            pass

        return False
