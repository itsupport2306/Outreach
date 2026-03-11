# app/sendgrid_webhook.py
import os
import logging
import re
import json
import asyncio
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, Depends, Header
from fastapi.responses import JSONResponse
from typing import Dict, Optional

from app.database import SessionLocal
from app.models import OutreachTracker

# Configure logging
logger = logging.getLogger(__name__)

# Create router
router = APIRouter()

# Environment variables
SENDGRID_WEBHOOK_SECRET = os.getenv('SENDGRID_WEBHOOK_SECRET', '')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SENDGRID_API_KEY = os.getenv('SENDGRID_API_KEY')
SENDGRID_FROM_EMAIL = os.getenv('SENDGRID_FROM_EMAIL', 'noreply@example.com')

_alembic_running = (os.getenv("ALEMBIC_RUNNING") or "").strip() in {"1", "true", "True"}

# Lazily initialize EmailProcessor to avoid import-time side effects (especially during Alembic).
email_processor = None

def _get_email_processor():
    global email_processor
    if email_processor is not None:
        return email_processor
    if _alembic_running:
        return None
    try:
        from app.email_processor import EmailProcessor
        google_api_key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
        email_processor = EmailProcessor(google_api_key=google_api_key) if google_api_key else None
    except Exception as e:
        logger.error(f"Failed to initialize EmailProcessor in sendgrid_webhook: {e}")
        email_processor = None
    return email_processor


async def verify_webhook_signature(
        x_twilio_email_event_webhook_signature: Optional[str] = Header(None,
                                                                       alias='X-Twilio-Email-Event-Webhook-Signature')
) -> bool:
    """Verify the SendGrid webhook signature if configured."""
    # If no secret is configured, skip verification entirely
    if not SENDGRID_WEBHOOK_SECRET:
        return True

    # If a secret is configured but the header is missing, log a warning and allow.
    # SendGrid Inbound Parse does not send this header by default.
    if not x_twilio_email_event_webhook_signature:
        logger.warning(
            "Webhook signature header missing; skipping signature verification despite configured secret"
        )
        return True

    # Only reject when a signature is present and does not match the expected value.
    if x_twilio_email_event_webhook_signature != SENDGRID_WEBHOOK_SECRET:
        logger.error(
            f"Invalid webhook signature. Expected: {SENDGRID_WEBHOOK_SECRET}, Got: {x_twilio_email_event_webhook_signature}")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    return True


def clean_email_body(body: str) -> str:
    """Clean the email body by removing quoted text and signatures."""
    if not body:
        return ""

    lines = []
    for line in body.split('\n'):
        clean_line = line.strip()
        if not (clean_line.startswith('>') or
                clean_line.startswith('On ') and 'wrote:' in clean_line or
                clean_line.lower() in ['sent from my iphone', 'sent from my mobile device']):
            lines.append(line)

    cleaned = '\n'.join(lines).strip()
    return re.sub(r'\n{3,}', '\n\n', cleaned)


@router.post("/email-webhook")
async def handle_inbound_email(
        request: Request,
        verified: bool = Depends(verify_webhook_signature)
):
    """Handle incoming email webhook from SendGrid and drive availability + scheduling flow.

    This is meant to mirror the behaviour of the existing messages feature:
    - focus on understanding the candidate's availability
    - schedule an interview using Google Calendar
    - send a confirmation email with the meeting link
    """
    try:
        ep = _get_email_processor()
        if not ep:
            logger.error("EmailProcessor is not initialized; cannot process email replies")
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": "EmailProcessor not available on server"},
            )

        # Parse SendGrid Inbound Parse payload (form-encoded)
        form_data = await request.form()
        email_data = dict(form_data)

        # Log received data (excluding large fields)
        log_data = {k: v for k, v in email_data.items() if k not in ["text", "html", "attachments"]}
        log_data["has_attachments"] = bool(email_data.get("attachments"))
        log_data["text_length"] = len(email_data.get("text", ""))
        log_data["html_length"] = len(email_data.get("html", ""))

        logger.info("=== RECEIVED SENDGRID INBOUND EMAIL ===")
        logger.info(json.dumps(log_data, indent=2))
        logger.info("======================================")

        # Extract basic fields from SendGrid Inbound Parse
        raw_from = email_data.get("from", "")
        to_email = email_data.get("to", "")
        subject = email_data.get("subject", "")
        text_body = email_data.get("text", "") or ""
        html_body = email_data.get("html", "") or ""

        if not raw_from or not to_email:
            logger.error("Missing required email fields (from/to)")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Missing required email fields"},
            )

        # Skip auto-replies
        subject_lower = subject.lower()
        if any(x in subject_lower for x in ["auto", "out of office", "automatic reply", "autoreply"]):
            logger.info("Skipping auto-reply/out of office email")
            return JSONResponse(content={"status": "skipped", "reason": "auto-reply"})

        # Derive candidate email and name from the From header
        match = re.search(r"<([^>]+)>", raw_from)
        candidate_email = match.group(1).strip().lower() if match else raw_from.strip().lower()

        # Mark reply for follow-up tracker
        try:
            db = SessionLocal()
            try:
                tracker = (
                    db.query(OutreachTracker)
                    .filter(
                        OutreachTracker.channel == "email",
                        OutreachTracker.contact == candidate_email,
                        OutreachTracker.replied_at.is_(None),
                    )
                    .order_by(OutreachTracker.id.desc())
                    .first()
                )
                if tracker:
                    candidate_id = tracker.candidate_id
                    tracker.replied_at = datetime.utcnow()
                    tracker.last_inbound_at = datetime.utcnow()
                    tracker.status = "replied"
                    tracker.next_followup_at = None
                    if candidate_id:
                        others = (
                            db.query(OutreachTracker)
                            .filter(
                                OutreachTracker.candidate_id == candidate_id,
                                OutreachTracker.replied_at.is_(None),
                            )
                            .all()
                        )
                        for other in others:
                            other.replied_at = datetime.utcnow()
                            other.last_inbound_at = datetime.utcnow()
                            other.status = "replied"
                            other.next_followup_at = None
                    db.commit()
            finally:
                db.close()
        except Exception as mark_ex:
            logger.error(f"Failed to mark email reply for {candidate_email}: {mark_ex}")

        # Basic name extraction from 'Name <email@x>' or local-part of email
        candidate_name = raw_from
        if "<" in raw_from:
            candidate_name = raw_from.split("<")[0].strip() or "Candidate"
        elif "@" in candidate_email:
            local_part = candidate_email.split("@")[0]
            candidate_name = " ".join(
                p.capitalize() for p in re.split(r"[._]+", local_part) if p
            ) or "Candidate"

        # Prefer plain text, fall back to cleaned HTML
        email_content = text_body.strip()
        if not email_content and html_body:
            email_content = clean_email_body(html_body)

        # Some SendGrid Inbound Parse deliveries may not populate text/html,
        # but include the full raw message in the `email` field.
        # As a final fallback, derive content from that raw message.
        if not email_content:
            raw_email_full = email_data.get("email", "") or ""
            if raw_email_full:
                email_content = clean_email_body(raw_email_full)

        logger.info(f"Processing reply from {candidate_email} (name guess: {candidate_name})")
        logger.info(f"Email content preview: {email_content[:200]}...")

        # Off-topic detection: if candidate asks something not in JD and no availability provided,
        # forward to human and acknowledge to candidate, skipping scheduling.
        def _looks_like_availability(text: str) -> bool:
            if not isinstance(text, str):
                return False
            t = text.lower()
            availability_keywords = [
                'am','pm','morning','afternoon','evening','night',"o'clock",
                'today','tomorrow','tonight','available','availability','schedule','scheduling','reschedule',
                'monday','tuesday','wednesday','thursday','friday','saturday','sunday','timezone','time zone'
            ]
            return any(w in t for w in availability_keywords)

        def _extract_jd_keywords(desc: str) -> set:
            if not isinstance(desc, str):
                return set()
            import re as _re
            tokens = [x for x in _re.split(r"[^a-zA-Z]+", desc.lower()) if len(x) > 3]
            common = {"about","with","that","this","from","your","have","will","been","which","their","there"}
            return set([x for x in tokens if x not in common])

        def _is_offtopic(text: str, jd: str) -> bool:
            if not isinstance(text, str):
                return False
            t = text.lower()
            if _looks_like_availability(t):
                return False
            question_like = ('?' in t) or any(t.startswith(w) or f" {w} " in t for w in [
                'what','how','where','when','who','which','why','pay','rate','salary','benefit','housing','stipend','overtime'
            ])
            if not question_like:
                return False
            jd_keys = _extract_jd_keywords(os.getenv('CURRENT_JOB_DESCRIPTION',''))
            words = set([w for w in t.replace('?',' ').split() if len(w) > 3])
            overlap = len(words & jd_keys)
            return overlap < 2

        job_desc_for_cls = os.getenv('CURRENT_JOB_DESCRIPTION','')
        if _is_offtopic(email_content or "", job_desc_for_cls):
            try:
                forward_email = os.getenv('OFFTOPIC_FORWARD_EMAIL', 'sagar@radixsol.com').strip()
                if SENDGRID_API_KEY:
                    from sendgrid import SendGridAPIClient
                    from sendgrid.helpers.mail import Mail, HtmlContent, Email
                    sg = SendGridAPIClient(SENDGRID_API_KEY)

                    # Forward to recruiter
                    fwd_subject = f"[Escalation][Email] From {candidate_email}: {subject[:60]}"
                    fwd_body = f"""
<p>Off-topic candidate reply detected.</p>
<p><b>From:</b> {candidate_name} &lt;{candidate_email}&gt;</p>
<p><b>To:</b> {to_email}</p>
<p><b>Subject:</b> {subject}</p>
<hr/>
<pre style='white-space:pre-wrap'>{(email_content or '')[:4000]}</pre>
"""
                    fwd_msg = Mail(
                        from_email=SENDGRID_FROM_EMAIL,
                        to_emails=forward_email,
                        subject=fwd_subject,
                        html_content=HtmlContent(fwd_body),
                    )
                    sg.send(fwd_msg)

                    # Acknowledge to candidate
                    ack_msg = Mail(
                        from_email=SENDGRID_FROM_EMAIL,
                        to_emails=candidate_email,
                        subject="Thanks – connecting you with a recruiter",
                        html_content=HtmlContent(
                            "<p>Thanks for your question. I'm looping in a recruiter to help and you'll hear from us shortly.</p>"
                        ),
                    )
                    try:
                        ack_msg.reply_to = Email(SENDGRID_FROM_EMAIL)
                    except Exception:
                        pass
                    sg.send(ack_msg)

                logger.info(f"Forwarded off-topic email from {candidate_email} to {forward_email}")
                return JSONResponse(content={"status": "forwarded", "reason": "off_topic"})
            except Exception as e:
                logger.error(f"Failed to forward off-topic email: {e}", exc_info=True)
                # continue to scheduling flow if forwarding fails

        # Drive availability extraction + scheduling via EmailProcessor
        # schedule_from_plain_email is async, so we must await it
        result = await ep.schedule_from_plain_email(
            candidate_email=candidate_email,
            candidate_name=candidate_name,
            email_content=email_content,
        )

        logger.info(f"Email scheduling result: {json.dumps(result, indent=2, default=str)}")

        if result.get("status") == "success":
            return JSONResponse(
                content={
                    "status": "success",
                    "message": "Interview scheduled and confirmation email sent",
                    "meeting_link": result.get("meeting_link"),
                    "start_time": result.get("start_time"),
                    "event_id": result.get("event_id"),
                }
            )

        # Non-successful scheduling: still return 200 to avoid endless retries,
        # but include details so you can monitor logs and fix prompts.
        return JSONResponse(
            status_code=200,
            content={
                "status": "error",
                "message": result.get("message", "Failed to schedule interview from email"),
                "details": result.get("details"),
            },
        )

    except Exception as e:
        logger.error(f"Error processing SendGrid inbound email webhook: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"Internal server error while handling inbound email: {str(e)}",
            },
        )


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "services": {
            "sendgrid": bool(SENDGRID_API_KEY),
            "gemini": bool(GEMINI_API_KEY)
        }
    }