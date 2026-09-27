import os
import json
import re
import logging
import traceback
import difflib
import hashlib
import json
import logging
import math
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import asyncio
import sys
import time
import uuid
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# SQLAlchemy imports
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, func, text
from sqlalchemy.orm import Session, load_only

# FastAPI imports
from fastapi import FastAPI, HTTPException, Body, Request, Form, Depends, status, Response, Query, BackgroundTasks, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from twilio.twiml.voice_response import VoiceResponse

# Configure logging
log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'interview_scheduler.log')

# Create a custom formatter
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Get logger instance
logger = logging.getLogger(__name__)

_manual_context_lock = threading.Lock()
_manual_phone_context: Dict[str, Dict[str, Any]] = {}

def _set_manual_phone_context(phone_number: str, ctx: Dict[str, Any]) -> None:
    try:
        phone = _norm_e164(phone_number)
        if not phone:
            return
        payload = dict(ctx or {})
        payload["updated_at"] = datetime.utcnow().isoformat()
        with _manual_context_lock:
            _manual_phone_context[phone] = payload
    except Exception:
        return

def _get_manual_phone_context(phone_number: str) -> Optional[Dict[str, Any]]:
    try:
        phone = _norm_e164(phone_number)
        if not phone:
            return None
        with _manual_context_lock:
            return dict(_manual_phone_context.get(phone) or {}) or None
    except Exception:
        return None

# Twilio imports
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.request_validator import RequestValidator

# Shared database (MySQL when DATABASE_URL is configured)
from app.database import Base, SessionLocal, engine, get_db

# Helper to get messaging service
def get_messaging_service(db: Session):
    from app.messaging import get_messaging_service as _get_messaging_service
    return _get_messaging_service(db=db)

# Shared SQLAlchemy models in app/models.py
from app.models import InterviewSchedule, OutreachTracker

from .interview_service import InterviewService

# Initialize interview service (kept as singleton for call state)
interview_service = None

# Create FastAPI app (must exist before decorators like @app.middleware)
app = FastAPI(title="Candidate Interview Scheduler")

@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    """Attach a request id to every request and response for easier debugging in production."""
    req_id = request.headers.get("X-Request-ID") or request.headers.get("X-Correlation-ID")
    if not req_id:
        req_id = str(uuid.uuid4())
    request.state.request_id = req_id

    try:
        response = await call_next(request)
    except Exception:
        # Let exception handlers format the response, but ensure request_id exists on state.
        raise
    response.headers["X-Request-ID"] = req_id
    return response

@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    req_id = getattr(request.state, "request_id", None)
    logger.warning(f"Request validation error request_id={req_id}: {exc}")
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": "validation_error", "detail": exc.errors(), "request_id": req_id},
    )

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, "request_id", None)
    logger.error(f"Unhandled exception request_id={req_id}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "internal_server_error", "request_id": req_id},
    )

_sms_handoff_routes = {}
_sheet_sms_campaigns: Dict[str, Dict[str, Any]] = {}
_sheet_sms_campaigns_lock = threading.Lock()

def _norm_e164(phone: str) -> str:
    """Best-effort E.164 normalization.

    - Preserves leading '+' when present.
    - Strips spaces/dashes/parentheses and other formatting.
    - Converts '00' prefix to '+' (international format).
    - For US-heavy datasets: converts 10-digit numbers to +1XXXXXXXXXX.
    - Converts 11-digit numbers starting with '1' to +1XXXXXXXXXX.
    - Returns '' for clearly invalid/too-short numbers to avoid Twilio failures.
    """
    raw = (phone or "").strip()
    if not raw:
        return ""

    # Normalize common international prefix
    if raw.startswith("00"):
        raw = "+" + raw[2:]

    # Keep '+' but strip all other non-digits
    if raw.startswith("+"):
        digits = re.sub(r"\D+", "", raw)
        if not digits:
            return ""
        # E.164 max is 15 digits; we accept 10..15 digits for sanity
        if len(digits) < 10 or len(digits) > 15:
            return ""
        return "+" + digits

    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return ""

    # US default behavior (CEIPAL data is mostly US)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits

    # If it's plausible international length but missing '+', treat as invalid to avoid mis-dialing.
    return ""

def _twiml_sms(body_text: str) -> Response:
    import html
    escaped = html.escape(body_text or "")
    payload = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Response>
    <Message>
        <Body>{escaped}</Body>
    </Message>
</Response>"""
    return Response(content=payload, media_type="application/xml", headers={"Content-Type": "application/xml"})


def _parse_candidate_xlsx(
    content: bytes,
    first_name_column: Optional[int] = None,
    phone_column: Optional[int] = None,
    include_columns: bool = False,
) -> tuple[Any, int]:
    """Read the first worksheet, optionally returning its columns or mapping selected fields."""
    import io
    import posixpath
    import zipfile
    import xml.etree.ElementTree as ET

    namespace = {
        "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "p": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        members = archive.infolist()
        if len(members) > 500 or sum(item.file_size for item in members) > 100 * 1024 * 1024:
            raise ValueError("Workbook contents exceed the import size limit.")
        names = {item.filename for item in members}
        if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
            raise ValueError("This file is not a valid Excel workbook.")
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships.findall("p:Relationship", namespace)}
        sheet = workbook.find("m:sheets/m:sheet", namespace)
        if sheet is None:
            raise ValueError("The workbook does not contain a worksheet.")
        target = rel_targets.get(sheet.attrib.get("{" + namespace["r"] + "}id"), "")
        sheet_path = (
            posixpath.normpath(target.lstrip("/"))
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
        if not sheet_path.startswith("xl/") or sheet_path not in names:
            raise ValueError("The first worksheet could not be read.")

        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in names:
            strings_xml = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(text.text or "" for text in item.findall(".//m:t", namespace))
                for item in strings_xml.findall("m:si", namespace)
            ]
        def cell_value(cell) -> str:
            if cell.attrib.get("t") == "inlineStr":
                inline = cell.find("m:is", namespace)
                return "".join(text.text or "" for text in inline.findall(".//m:t", namespace)) if inline is not None else ""
            value = cell.find("m:v", namespace)
            raw = value.text if value is not None and value.text is not None else ""
            if cell.attrib.get("t") == "s" and raw:
                return shared_strings[int(raw)]
            return raw

        def col_index(cell_ref: str) -> int:
            letters = re.match(r"[A-Z]+", cell_ref or "")
            if not letters:
                return -1
            result = 0
            for char in letters.group(0):
                result = result * 26 + ord(char) - ord("A") + 1
            return result - 1

        candidates: List[Dict[str, str]] = []
        seen_phones = set()
        skipped = 0
        header = None
        header_columns: Dict[int, str] = {}
        first_idx = phone_idx = None
        populated_rows = 0
        element_stack = []
        with archive.open(sheet_path) as sheet_stream:
            for event, row in ET.iterparse(sheet_stream, events=("start", "end")):
                if event == "start":
                    element_stack.append(row)
                    continue
                if row.tag != "{" + namespace["m"] + "}row":
                    element_stack.pop()
                    continue
                values = {
                    col_index(cell.attrib.get("r", "")): cell_value(cell).strip()
                    for cell in row.findall("m:c", namespace)
                }
                if header is None:
                    header = {}
                    for col, value in values.items():
                        if col < 0:
                            continue
                        label = value or f"Column {col + 1}"
                        header_columns[col] = label
                        key = re.sub(r"[^a-z0-9]", "", label.lower())
                        if key and key not in header:
                            header[key] = col
                    def suggested_column(aliases: tuple[str, ...], suffixes: tuple[str, ...] = ()) -> Optional[int]:
                        for alias in aliases:
                            if alias in header:
                                return header[alias]
                        for label_key, col in header.items():
                            if any(label_key.endswith(suffix) for suffix in suffixes):
                                return col
                        return None

                    first_idx = suggested_column(
                        ("firstname", "first", "givenname", "given", "candidatename", "name"),
                        ("firstname", "givenname", "name"),
                    )
                    phone_idx = suggested_column(
                        ("phone", "prefphone", "phonenumber", "mobile", "mobilephone", "cell", "cellphone", "telephone"),
                        ("phonenumber", "mobilephone", "cellphone", "telephone"),
                    )
                    if include_columns:
                        return {
                            "columns": [
                                {"index": col, "label": label}
                                for col, label in sorted(header_columns.items())
                            ],
                            "suggested_first_name_column": first_idx,
                            "suggested_phone_column": phone_idx,
                        }, 0
                    if first_name_column is not None:
                        first_idx = first_name_column
                    if phone_column is not None:
                        phone_idx = phone_column
                    if first_idx is None or first_idx not in header_columns:
                        raise ValueError("Choose a valid First Name column or add a recognizable first-name header.")
                    if phone_idx is None or phone_idx not in header_columns:
                        raise ValueError("Choose a valid Phone column or add a recognizable phone header.")
                    if first_idx == phone_idx:
                        raise ValueError("First Name and Phone must use different columns.")
                else:
                    if any(values.values()):
                        populated_rows += 1
                        if populated_rows > 10000:
                            raise ValueError("The worksheet exceeds the 10,000-populated-row import limit.")
                        # Outreach copy addresses candidates by first name only.
                        name = _extract_first_name(values.get(first_idx, ""))
                        phone = _norm_e164(values.get(phone_idx, ""))
                        if not name or not phone or phone in seen_phones:
                            skipped += 1
                        else:
                            seen_phones.add(phone)
                            candidates.append({"name": name, "phone": phone})
                row.clear()
                if len(element_stack) > 1:
                    element_stack[-2].remove(row)
                element_stack.pop()
        if header is None:
            raise ValueError("The worksheet is empty or exceeds the 10,000-populated-row import limit.")
        if not candidates:
            raise ValueError("No rows with both a candidate name and a valid phone number were found.")
        return candidates, skipped
    except zipfile.BadZipFile as exc:
        raise ValueError("This file is not a valid .xlsx workbook.") from exc


def _render_sheet_outreach_message(template: str, first_name: str) -> str:
    """Render supported candidate-name tokens in a spreadsheet outreach template."""
    return (
        template.replace("{{first_name}}", first_name)
        .replace("{{name}}", first_name)
        .replace("{name}", first_name)
    )


def _extract_first_name(candidate_name: str) -> str:
    """Get a given name from either a first-name field or a full candidate name."""
    value = re.sub(r"\s+", " ", str(candidate_name or "")).strip()
    if not value:
        return ""

    suffixes = {"jr", "sr", "ii", "iii", "iv", "v", "phd", "md", "rn"}
    if "," in value:
        before_comma, after_comma = value.split(",", 1)
        after_tokens = after_comma.strip().split()
        if after_tokens and after_tokens[0].lower().strip(".,") not in suffixes:
            value = " ".join(after_tokens)
        else:
            value = before_comma.strip()

    tokens = value.split()
    if tokens and tokens[0].lower().strip(".,") in {"mr", "mrs", "ms", "miss", "dr"}:
        tokens = tokens[1:]
    return tokens[0].strip(".,;:") if tokens else ""


_SHEET_OUTREACH_MESSAGE = (
    "Hi {name}, this is Brian from Radixsol. We have Travel RN openings in KY, IN & OH "
    "across multiple specialties with competitive weekly pay. Interested?"
)


@app.post("/api/sms/outreach/columns")
async def inspect_sheet_outreach_columns(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Upload an .xlsx workbook.")
    content = await file.read(15 * 1024 * 1024 + 1)
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Workbook exceeds the 15 MB upload limit.")
    try:
        columns, _ = _parse_candidate_xlsx(content, include_columns=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return columns


@app.post("/api/sms/outreach/preview")
async def preview_sheet_outreach(
    file: UploadFile = File(...),
    first_name_column: Optional[int] = Form(None),
    phone_column: Optional[int] = Form(None),
    message_template: str = Form(_SHEET_OUTREACH_MESSAGE),
):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Upload an .xlsx workbook.")
    content = await file.read(15 * 1024 * 1024 + 1)
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Workbook exceeds the 15 MB upload limit.")
    message_template = (message_template or "").strip()
    if not message_template:
        raise HTTPException(status_code=400, detail="Enter the SMS message to send.")
    if len(message_template) > 1500:
        raise HTTPException(status_code=400, detail="SMS message templates must be 1,500 characters or fewer.")
    try:
        candidates, skipped = _parse_candidate_xlsx(
            content,
            first_name_column=first_name_column,
            phone_column=phone_column,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Do not re-contact candidates already messaged through a spreadsheet campaign.
    db = SessionLocal()
    try:
        contacted_phones = {
            row[0]
            for row in db.query(OutreachTracker.contact)
            .filter(OutreachTracker.candidate_id.like("sheet-email:%"))
            .distinct()
            .all()
        }
    finally:
        db.close()
    remaining_candidates = [candidate for candidate in candidates if candidate["phone"] not in contacted_phones]
    skipped += len(candidates) - len(remaining_candidates)
    candidates = remaining_candidates
    if not candidates:
        raise HTTPException(status_code=409, detail="All valid candidates in this workbook were already contacted.")

    token = str(uuid.uuid4())
    with _sheet_sms_campaigns_lock:
        now = datetime.utcnow()
        expired = [key for key, value in _sheet_sms_campaigns.items() if value.get("expires_at", now) <= now]
        for key in expired:
            _sheet_sms_campaigns.pop(key, None)
        _sheet_sms_campaigns[token] = {
            "candidates": candidates,
            "skipped": skipped,
            "message_template": message_template,
            "state": "preview",
            "created_at": now,
            "expires_at": now + timedelta(minutes=30),
        }
    return {
        "campaign_id": token,
        "recipient_count": len(candidates),
        "skipped_count": skipped,
        "message_preview": _render_sheet_outreach_message(message_template, candidates[0]["name"]),
        "sample_recipients": [
            {"name": candidate["name"], "phone_last4": candidate["phone"][-4:]}
            for candidate in candidates[:10]
        ],
        "outreach_enabled": (os.getenv("SHEET_OUTREACH_ENABLED") or "").strip().lower() in {"1", "true", "yes", "y"},
    }


def _run_sheet_outreach(campaign_id: str) -> None:
    from app.messaging_service import messaging_service as sms_service

    with _sheet_sms_campaigns_lock:
        campaign = _sheet_sms_campaigns.get(campaign_id)
        if not campaign:
            return
        recipients = list(campaign.get("candidates_to_send", campaign["candidates"]))
    db = SessionLocal()
    try:
        for candidate in recipients:
            message = _render_sheet_outreach_message(
                campaign.get("message_template", _SHEET_OUTREACH_MESSAGE),
                candidate["name"],
            )
            success = False
            message_sid = None
            error = None
            try:
                message_sid = sms_service.send_sms_with_sid(candidate["phone"], message)
                success = bool(message_sid)
                if success:
                    now = datetime.utcnow()
                    tracker = OutreachTracker(
                        candidate_id=f"sheet-email:{campaign_id}:{candidate['phone']}",
                        contact=candidate["phone"], channel="sms", first_contacted_at=now,
                        last_outreach_at=now, followup_count=0, status="active",
                    )
                    db.add(tracker)
                db.add(SmsLog(
                    candidate_id=None, phone=candidate["phone"], direction="outgoing",
                    message=message, status="sent" if success else "failed", provider_sid=message_sid,
                ))
                db.commit()
                if not success:
                    error = "Twilio could not send the message; check Twilio configuration and application logs."
            except Exception as exc:
                db.rollback()
                error = str(exc)
                logger.error("Sheet outreach failed for %s: %s", candidate["phone"], exc, exc_info=True)
            with _sheet_sms_campaigns_lock:
                current = _sheet_sms_campaigns.get(campaign_id)
                if current:
                    current["sent_count"] += int(success)
                    current["failed_count"] += int(not success)
                    if message_sid:
                        current.setdefault("message_sids", []).append(message_sid)
                    if error:
                        current["last_error"] = error[:500]
                    current["processed_count"] += 1
            time.sleep(max(0.0, min(float(os.getenv("SHEET_OUTREACH_DELAY_SECONDS", "0.2")), 5.0)))
    finally:
        db.close()
        with _sheet_sms_campaigns_lock:
            current = _sheet_sms_campaigns.get(campaign_id)
            if current:
                current["state"] = "complete"


@app.post("/api/sms/outreach/{campaign_id}/send")
async def send_sheet_outreach(
    campaign_id: str,
    background_tasks: BackgroundTasks,
    limit: int = Query(5, ge=1, le=10000),
):
    outreach_enabled = (os.getenv("SHEET_OUTREACH_ENABLED") or "").strip().lower() in {"1", "true", "yes", "y"}
    if not outreach_enabled:
        raise HTTPException(status_code=409, detail="Spreadsheet SMS outreach is disabled. Set SHEET_OUTREACH_ENABLED=1 to send SMS.")
    with _sheet_sms_campaigns_lock:
        campaign = _sheet_sms_campaigns.get(campaign_id)
        if not campaign or campaign.get("expires_at", datetime.utcnow()) <= datetime.utcnow():
            _sheet_sms_campaigns.pop(campaign_id, None)
            raise HTTPException(status_code=404, detail="Preview expired. Upload the workbook again.")
        if campaign["state"] != "preview":
            raise HTTPException(status_code=409, detail="This campaign has already been started.")
        candidates_to_send = list(campaign["candidates"][:limit])
        campaign.update({
            "state": "sending", "candidates_to_send": candidates_to_send,
            "processed_count": 0, "sent_count": 0, "failed_count": 0, "message_sids": [],
        })
    background_tasks.add_task(_run_sheet_outreach, campaign_id)
    return {"state": "sending", "recipient_count": len(candidates_to_send)}


@app.get("/api/sms/outreach/{campaign_id}")
async def get_sheet_outreach_status(campaign_id: str):
    with _sheet_sms_campaigns_lock:
        campaign = _sheet_sms_campaigns.get(campaign_id)
        if not campaign or campaign.get("expires_at", datetime.utcnow()) <= datetime.utcnow():
            _sheet_sms_campaigns.pop(campaign_id, None)
            raise HTTPException(status_code=404, detail="Campaign status is no longer available.")
        return {
            "state": campaign["state"],
            "recipient_count": len(campaign.get("candidates_to_send", campaign["candidates"])),
            "processed_count": campaign.get("processed_count", 0),
            "sent_count": campaign.get("sent_count", 0),
            "failed_count": campaign.get("failed_count", 0),
            "skipped_count": campaign["skipped"],
            "last_error": campaign.get("last_error"),
            "message_sids": list(campaign.get("message_sids", [])),
        }

def get_interview_service(db: Session = Depends(get_db)) -> InterviewService:
    global interview_service
    if interview_service is None:
        interview_service = InterviewService(db)
    else:
        # refresh DB session per request (avoid holding stale/closed session)
        interview_service.db = db
    return interview_service

# Log startup information
logger.info("\n" + "="*80)
logger.info("INTERVIEW SCHEDULER STARTING")
logger.info(f"Log file: {log_file}")
from app.database import DATABASE_BACKEND
logger.info("Database backend configured: %s", DATABASE_BACKEND)
logger.info("="*80 + "\n")

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from app.template_loader import template_manager

# Import messaging service
from app.messaging_service import MessagingService
from . import messaging_service

# Import email processor
from .email_processor import (
    EmailProcessor, 
    AvailabilityRequest, 
    ScheduleRequest, 
    InterviewSlot,
    GoogleCalendarScheduler
)
import google.generativeai as genai

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from sentence_transformers import SentenceTransformer

from app.ceipal_client import build_ceipal_client_from_env

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(APP_ROOT)
DATA_FILE = os.path.join(PROJECT_ROOT, "Applicant Data.json")
DB_FILE = os.path.join(PROJECT_ROOT, "app.db")
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")
TTS_DIR = os.path.join(PROJECT_ROOT, "tts")
REQUIRED_SKILLS_FILE = os.path.join(PROJECT_ROOT, "required_skills.json")
REQUIRED_QUESTIONS_FILE = os.path.join(PROJECT_ROOT, "required_questions.json")
STRUCTURED_JD_FILE = os.path.join(PROJECT_ROOT, "structured_jd_cache.json")

# Load environment variables from .env at project root if present
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# Routers
from app.sendgrid_webhook import router as sendgrid_router
from app.api.endpoints.auth import router as auth_router

@app.on_event("startup")
async def startup_event():
    """Keep startup limited to the active spreadsheet outreach workflow."""
    logger.info("Spreadsheet outreach mode active; interview/TTS startup is disabled")

@app.middleware("http")
async def _log_timing_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    path = request.url.path
    if path.startswith("/tts/") or path.startswith("/api/interview/"):
        logger.info(f"HTTP timing: {request.method} {path} -> {response.status_code} ({elapsed_ms}ms)")
    return response

# Ensure TTS cache directory exists for ElevenLabs audio
os.makedirs(TTS_DIR, exist_ok=True)

# Include the router
app.include_router(sendgrid_router, prefix="/api")
app.include_router(auth_router, prefix="/api/auth")
# Define API routes before adding CORS middleware
@app.post("/api/sms/webhook")
async def sms_webhook(From: str = Form(...), Body: str = Form(...), db: Session = Depends(get_db)):
    """Log candidate replies and email them to recruiters; never auto-reply to candidates."""
    try:
        logger.info(f"Received SMS from {From}: {Body}")

        recruiter_number = _norm_e164(os.getenv("OFFTOPIC_FORWARD_SMS_NUMBER", "+16234736809"))

        # Ignore internal recruiter messages in spreadsheet-only mode. The legacy
        # relay sent SMS back to candidates and is intentionally disabled here.
        normalized_from = _norm_e164(From)
        if recruiter_number and normalized_from == recruiter_number:
            logger.info("Ignoring inbound recruiter SMS; recruiter-to-candidate relay is disabled")
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response/>',
                media_type="application/xml",
                headers={"Content-Type": "application/xml"},
            )

        normalized_phone = _norm_e164(From)
        reply_candidate_name = ""
        original_message = ""

        # Mark this candidate as replied and stop any pending follow-ups.
        try:
            tracker = (
                db.query(OutreachTracker)
                .filter(
                    OutreachTracker.channel == "sms",
                    OutreachTracker.contact == normalized_phone,
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
        except Exception as mark_ex:
            logger.error(f"Failed to mark SMS reply for {normalized_phone}: {mark_ex}")

        # Log every inbound candidate SMS and email its details to the configured recruiters.
        try:
            sms_log = SmsLog(
                candidate_id=None,
                phone=normalized_phone,
                direction="incoming",
                message=Body,
                status="received",
                provider_sid=None,
            )
            db.add(sms_log)
            db.commit()
        except Exception as log_ex:
            logger.error(f"Failed to log incoming SMS from {normalized_phone}: {log_ex}", exc_info=True)
        try:
            import html
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail, HtmlContent

            recipient_emails = [
                address.strip()
                for address in re.split(r"[,;]+", os.getenv("OFFTOPIC_FORWARD_EMAIL") or "")
                if address.strip()
            ]
            sendgrid_api_key = (os.getenv("SENDGRID_API_KEY") or "").strip()
            sender_email = (os.getenv("SENDGRID_FROM_EMAIL") or "").strip()

            try:
                original_row = (
                    db.query(SmsLog.message)
                    .filter(SmsLog.phone == normalized_phone, SmsLog.direction == "outgoing")
                    .order_by(SmsLog.id.desc())
                    .first()
                )
                original_message = str(original_row[0] or "") if original_row else ""
            except Exception as original_ex:
                logger.warning(f"Could not load original outreach for reply from {normalized_phone}: {original_ex}")

            name_match = re.match(
                r"(?is)^\s*Hi\s+(.+?),\s+this is Brian from Radixsol\.", original_message
            )
            if name_match:
                reply_candidate_name = name_match.group(1).strip()

            if recipient_emails and sendgrid_api_key and sender_email:
                details = [
                    ("Candidate", reply_candidate_name or "Name unavailable"),
                    ("Phone", normalized_phone or From),
                    ("Received (UTC)", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
                    ("Original outreach", original_message or "Unavailable"),
                    ("Candidate reply", (Body or "").strip() or "[empty message]"),
                ]
                rows_html = "".join(
                    f"<tr><th align='left' style='padding:6px 12px 6px 0'>{html.escape(label)}</th>"
                    f"<td style='padding:6px 0;white-space:pre-wrap'>{html.escape(value)}</td></tr>"
                    for label, value in details
                )
                email_message = Mail(
                    from_email=sender_email,
                    to_emails=recipient_emails,
                    subject=f"[Candidate reply] {reply_candidate_name or normalized_phone}",
                    html_content=HtmlContent(f"<p>A candidate replied to SMS outreach.</p><table>{rows_html}</table>"),
                )
                SendGridAPIClient(sendgrid_api_key).send(email_message)
                logger.info(f"Candidate reply email sent to {', '.join(recipient_emails)} for {normalized_phone}")
            else:
                logger.error(
                    "Candidate reply email not sent: configure OFFTOPIC_FORWARD_EMAIL, "
                    "SENDGRID_API_KEY, and SENDGRID_FROM_EMAIL."
                )
        except Exception as notify_ex:
            logger.error(f"Failed to email recruiter about reply from {normalized_phone}: {notify_ex}", exc_info=True)

        # Empty TwiML tells Twilio to end the webhook without sending an SMS reply.
        logger.info(f"Candidate reply recorded for {normalized_phone}; no automated SMS response sent")
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response/>',
            media_type="application/xml",
            headers={"Content-Type": "application/xml"},
        )
        
    except Exception as e:
        logger.error(f"Error processing SMS: {str(e)}", exc_info=True)
        error_response = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
        return Response(
            content=error_response,
            media_type="application/xml",
            status_code=200
        )

"""SMS utilities"""

# Endpoint to send SMS (for testing and internal use)
@app.post("/api/sms/send")
async def send_sms(request: Request, data: dict = Body(...), db: Session = Depends(get_db)):
    """Send SMS messages.
    
    Supports both a single phone number (to_number) for backwards
    compatibility and a list of phone numbers (to_numbers) for sending
    to multiple candidates at once.
    """
    try:
        # Get the messaging service with the provided db session
        messaging_service = get_messaging_service(db=db)
        
        # Handle both single number and list of numbers
        to_numbers = []
        if 'to_number' in data:
            to_numbers = [str(data['to_number']).strip()]
        elif 'to_numbers' in data and isinstance(data['to_numbers'], list):
            to_numbers = [str(n).strip() for n in data['to_numbers'] if n]
        else:
            raise HTTPException(
                status_code=400,
                detail="Either 'to_number' or 'to_numbers' must be provided"
            )
            
        message = data.get('message', '')
        candidate_id = data.get('candidate_id')
        role_from_frontend = (
            data.get('role')
            or data.get('role_title')
            or data.get('job_title')
            or data.get('title')
        )
        manual_location = (
            data.get('location')
            or data.get('job_location')
            or data.get('jobLocation')
            or data.get('loc')
        )
        manual_job_description = data.get('job_description') or data.get('jd')
        manual_required_skills = data.get('required_skills')
        manual_required_questions = data.get('required_questions')
        manual_certifications = data.get('certifications')

        # If the UI did not include job context in /api/sms/send, fall back to the latest
        # manual /rank request context (still frontend-provided).
        try:
            with _latest_manual_rank_lock:
                cached_manual = dict(_latest_manual_rank_context or {})
        except Exception:
            cached_manual = {}

        if not (role_from_frontend or "").strip():
            role_from_frontend = (cached_manual.get("role_title") or "").strip()
        if not (manual_job_description or "").strip():
            manual_job_description = (cached_manual.get("job_description") or "").strip()
        if not (manual_location or "").strip():
            manual_location = (cached_manual.get("location") or cached_manual.get("job_location") or "").strip()
        if not isinstance(manual_required_skills, list):
            manual_required_skills = cached_manual.get("required_skills")
        if not isinstance(manual_certifications, list):
            manual_certifications = cached_manual.get("certifications")
        if not isinstance(manual_required_questions, list):
            manual_required_questions = cached_manual.get("required_questions")
        use_template = bool(data.get('use_template', False))
        template_name = (data.get('template') or data.get('template_name') or '').strip()

        # Normalize manual job location for placeholder rendering + call context.
        # Prefer explicit request fields, then try to extract from the JD text.
        try:
            location_for_context = str(manual_location or "").strip()
        except Exception:
            location_for_context = ""
        if not location_for_context:
            try:
                jd_text = str(manual_job_description or "")
                m = re.search(r"(?im)^\s*location\s*[:\-]\s*(.+?)\s*$", jd_text)
                if m:
                    location_for_context = (m.group(1) or "").strip()
            except Exception:
                pass
        if location_for_context.startswith("[") and location_for_context.endswith("]"):
            location_for_context = location_for_context[1:-1].strip()
        
        # If explicit template key provided, use it
        if template_name:
            tpl_msg = template_manager.get_sms_template(template_name)
            if tpl_msg:
                message = tpl_msg
                logger.info(f"/api/sms/send: using templates.json -> sms.{template_name}")
            else:
                logger.warning(f"/api/sms/send: template '{template_name}' not found in templates.json; falling back")
                # fall through to generic handling

        # Heuristic: if UI sent its own default (contains 'Reply STOP'), override with our template unless explicitly disabled
        ui_default_detected = False
        if isinstance(message, str):
            mlow = message.lower()
            ui_default_detected = (
                ("reply stop" in mlow)
                or ("we'd like to discuss the" in mlow)
                or ("wed like to discuss the" in mlow)
                or ("{{name}}" in message)
                or ("{{role}}" in message)
            )

        # If message not provided OR explicitly requested OR UI default detected, prefer templates.json then fall back to FIRST_OUTREACH_SMS_TEXT
        if template_name or use_template or not message or ui_default_detected:
            # If we didn't already resolve a specific template, use first_outreach
            if not template_name or (template_name and message and message == data.get('message', '')):
                tpl_msg = template_manager.get_sms_template("first_outreach")
            if tpl_msg:
                message = tpl_msg
                logger.info("/api/sms/send: using templates.json -> sms.first_outreach")
            else:
                message = os.getenv(
                    "FIRST_OUTREACH_SMS_TEXT",
                    "Hi {name}, I'd love to connect about {Role}. When are you available? Please include your timezone (e.g., EST).",
                )
                logger.info("/api/sms/send: using env FIRST_OUTREACH_SMS_TEXT fallback")
        elif message:
            logger.info("/api/sms/send: using provided 'message' from request body")
            
        if not to_numbers:
            raise HTTPException(
                status_code=400,
                detail="At least one valid phone number is required"
            )
            
        # Log the request
        logger.info(f"Sending SMS to {', '.join(to_numbers)}")
        
        # Deduplicate while preserving order
        seen = set()
        unique_numbers = []
        for n in to_numbers:
            if n and n not in seen:
                seen.add(n)
                unique_numbers.append(n)

        # Prepare candidate context for placeholder replacement
        candidate = None
        try:
            id_to_candidate = {str(c.get("_id")): c for c in _candidates}
            if candidate_id:
                candidate = id_to_candidate.get(str(candidate_id))
        except Exception:
            candidate = None

        def render_placeholders(text: str) -> str:
            if not isinstance(text, str):
                return text
            name = ""
            job_title = ""
            location = ""
            if candidate:
                name = f"{candidate.get('FirstName','')} {candidate.get('LastName','')}".strip()
                job_title = str(candidate.get('JobTitle') or '')

            # Manual flow: location should come from the job context (frontend/JD),
            # NOT from the candidate's home city/state.
            location = location_for_context

            # Manual flow should not fall back to candidate profile JobTitle (can be a different role).
            role_val = (role_from_frontend or os.getenv("DEFAULT_ROLE", "the role") or "").strip()
            # Support both {name}/{Role} and {{name}}/{{job_title}}
            out = text

            # If no location is available, remove the literal " in {{location}}" fragment
            # so templates don't produce awkward text.
            if not location:
                out = out.replace(" in {{location}}", "")
                out = out.replace(" in {location}", "")
                out = out.replace(" in  {{location}}", "")
                out = out.replace(" in  {location}", "")

            out = out.replace("{name}", name)
            out = out.replace("{Role}", role_val)
            out = out.replace("{{name}}", name)
            out = out.replace("{{job_title}}", role_val)
            out = out.replace("{job_title}", role_val)
            out = out.replace("{{location}}", location)
            out = out.replace("{location}", location)
            return out

        results = []
        for number in unique_numbers:
            # Ensure number has proper format
            to_number = str(number).strip()
            if not to_number.startswith("+"):
                to_number = f"+{to_number}"  # Ensure country code is included

            # Manual flow: rely ONLY on frontend-provided job context.
            try:
                _set_manual_phone_context(
                    to_number,
                    {
                        "job_title": str(role_from_frontend or "").strip(),
                        "location": str(location_for_context or "").strip(),
                        "job_description": str(manual_job_description or "").strip(),
                        "required_skills": manual_required_skills if isinstance(manual_required_skills, list) else [],
                        "required_questions": manual_required_questions if isinstance(manual_required_questions, list) else [],
                        "certifications": manual_certifications if isinstance(manual_certifications, list) else [],
                    },
                )
            except Exception:
                pass

            try:
                logger.info(f"Sending SMS to {to_number}")
                rendered = render_placeholders(message)
                success = messaging_service.send_sms(to_number, rendered)

                # Follow-up tracker upsert for outbound SMS
                try:
                    now = datetime.utcnow()
                    tracker = (
                        db.query(OutreachTracker)
                        .filter(OutreachTracker.channel == "sms", OutreachTracker.contact == to_number)
                        .order_by(OutreachTracker.id.desc())
                        .first()
                    )
                    if not tracker:
                        tracker = OutreachTracker(
                            candidate_id=str(candidate_id) if candidate_id else None,
                            contact=to_number,
                            channel="sms",
                            first_contacted_at=now,
                            last_outreach_at=now,
                            followup_count=0,
                            next_followup_at=now + timedelta(hours=int(os.getenv("FOLLOWUP_HOURS", "12"))),
                            status="active",
                        )
                        db.add(tracker)
                    else:
                        # If candidate_id becomes available later, keep it
                        if candidate_id and not tracker.candidate_id:
                            tracker.candidate_id = str(candidate_id)
                        tracker.last_outreach_at = now
                        if tracker.replied_at is None and tracker.status == "active":
                            tracker.next_followup_at = now + timedelta(hours=int(os.getenv("FOLLOWUP_HOURS", "12")))
                    db.commit()
                except Exception as t_ex:
                    logger.error(f"Failed to upsert outreach tracker for {to_number}: {t_ex}", exc_info=True)

                # Log SMS in database for dashboard
                try:
                    db_log = db
                    sms_log = SmsLog(
                        candidate_id=str(candidate_id) if candidate_id else None,
                        phone=to_number,
                        direction="outgoing",
                        message=rendered,
                        status="sent" if success else "failed",
                        provider_sid=None,
                    )
                    db_log.add(sms_log)
                    db_log.commit()
                except Exception as log_ex:
                    logger.error(f"Failed to log SMS to {to_number}: {log_ex}", exc_info=True)

                results.append({
                    "to": to_number,
                    "success": success,
                    "error": None if success else "Failed to send SMS"
                })
                
                if success:
                    logger.info(f"Successfully sent SMS to {to_number}")
                else:
                    logger.warning(f"Failed to send SMS to {to_number}")
                    
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error sending SMS to {to_number}: {error_msg}", exc_info=True)
                results.append({
                    "to": to_number,
                    "success": False,
                    "error": error_msg
                })

        # Check if any messages were sent successfully
        any_success = any(r["success"] for r in results)
        if not any_success:
            raise HTTPException(
                status_code=500,
                detail="Failed to send SMS to any recipients",
                headers={"X-Error": "Sending failed"}
            )

        return {
            "status": "success" if all(r["success"] for r in results) else "partial_success",
            "message": "SMS sent successfully" if all(r["success"] for r in results) 
                      else "Some messages failed to send",
            "results": results
        }

    except HTTPException as he:
        logger.error(f"HTTP error in send_sms: {he.detail}")
        raise
    except Exception as e:
        logger.error(f"Error in send_sms: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/availability/recent")
async def get_recent_availability(limit: int = 50):
    """Return the most recent parsed availability records.

    This is a simple JSON API so you can see which candidates have
    provided availability over SMS without reading the logs.
    """
    safe_limit = max(1, min(limit, 200))

    db = SessionLocal()
    try:
        rows = (
            db.query(InterviewAvailability)
            .order_by(InterviewAvailability.created_at.desc(), InterviewAvailability.id.desc())
            .limit(safe_limit)
            .all()
        )

        results = []
        for r in rows:
            try:
                availability = json.loads(r.availability_json) if r.availability_json else {}
            except Exception:
                availability = {"parse_error": True}

            results.append(
                {
                    "id": r.id,
                    "candidate_id": r.candidate_id,
                    "candidate_name": r.candidate_name,
                    "role": r.role,
                    "phone": r.phone,
                    "availability": availability,
                    "raw_message": r.raw_message,
                    "created_at": r.created_at,
                }
            )

        return {"items": results, "count": len(results)}
    finally:
        db.close()

@app.post("/api/templates/reload")
def reload_templates():
    """Reload templates.json at runtime without restarting the server."""
    try:
        template_manager.reload()
        return {"status": "ok", "message": "templates.json reloaded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/templates/preview")
def preview_template(
    channel: str = Query(..., regex="^(sms|email)$"),
    name: str = Query(..., description="Template key e.g. first_outreach, followup1"),
    candidate_id: Optional[str] = None,
    role: Optional[str] = None,
):
    """Preview a rendered template for a candidate and optional role."""
    # Load candidate context
    candidate = None
    try:
        id_to_candidate = {str(c.get("_id")): c for c in _candidates}
        if candidate_id:
            candidate = id_to_candidate.get(str(candidate_id))
    except Exception:
        candidate = None

    def ctx():
        full_name = ""
        job_title = ""
        if candidate:
            full_name = f"{candidate.get('FirstName','')} {candidate.get('LastName','')}".strip()
            job_title = str(candidate.get('JobTitle') or '')
        role_val = role or job_title or os.getenv("DEFAULT_ROLE", "the role")
        return full_name, job_title, role_val

    if channel == "sms":
        raw = template_manager.get_sms_template(name)
        if not raw:
            raise HTTPException(status_code=404, detail="SMS template not found")
        full_name, job_title, role_val = ctx()
        rendered = (
            raw.replace("{name}", full_name)
               .replace("{Role}", role_val)
               .replace("{{name}}", full_name)
               .replace("{{job_title}}", job_title)
        )
        return {"channel": "sms", "name": name, "raw": raw, "rendered": rendered}
    else:
        tpl = template_manager.get_email_template(name)
        if not tpl.get("subject") and not tpl.get("body"):
            raise HTTPException(status_code=404, detail="Email template not found")
        full_name, job_title, role_val = ctx()
        subj = (
            (tpl.get("subject") or "")
            .replace("{name}", full_name)
            .replace("{Role}", role_val)
            .replace("{{name}}", full_name)
            .replace("{{job_title}}", job_title)
        )
        body = (
            (tpl.get("body") or "")
            .replace("{name}", full_name)
            .replace("{Role}", role_val)
            .replace("{{name}}", full_name)
            .replace("{{job_title}}", job_title)
        )
        return {"channel": "email", "name": name, "raw": tpl, "rendered": {"subject": subj, "body": body}}

@app.get("/api/dashboard/sms/logs")
async def get_sms_logs(limit: int = 100, db: Session = Depends(get_db)):
    """Return recent SMS logs for dashboard (outgoing and incoming)."""
    safe_limit = max(1, min(limit, 500))

    rows = (
        db.query(SmsLog)
        .order_by(SmsLog.created_at.desc(), SmsLog.id.desc())
        .limit(safe_limit)
        .all()
    )
    items = []
    for r in rows:
        items.append(
            {
                "id": r.id,
                "candidate_id": r.candidate_id,
                "phone": r.phone,
                "direction": r.direction,
                "message": r.message,
                "status": r.status,
                "provider_sid": r.provider_sid,
                "created_at": r.created_at,
            }
        )
    return {"items": items, "count": len(items)}

@app.get("/api/dashboard/interviews/schedules")
async def get_interview_schedules(limit: int = 100):
    """Return recent interview schedules from the interview scheduler DB."""
    safe_limit = max(1, min(limit, 500))

    db = SessionLocal()
    try:
        rows = (
            db.query(InterviewSchedule)
            .order_by(InterviewSchedule.created_at.desc(), InterviewSchedule.id.desc())
            .limit(safe_limit)
            .all()
        )
        items = []
        for r in rows:
            items.append(
                {
                    "id": r.id,
                    "candidate_phone": r.candidate_phone,
                    # These fields may not exist on the scheduler model in this repo.
                    # Keep them for UI compatibility but populate safely.
                    "candidate_name": getattr(r, "candidate_name", None),
                    "candidate_email": getattr(r, "candidate_email", None),
                    "scheduled_datetime": r.scheduled_datetime,
                    "timezone": r.timezone,
                    "status": r.status,
                    "call_sid": r.call_sid,
                    "recording_url": r.recording_url,
                    "call_pickup": getattr(r, "call_pickup", None),
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                }
            )
        return {"items": items, "count": len(items)}
    finally:
        db.close()

@app.get("/api/dashboard/interviews/results")
async def get_interview_results(limit: int = 100, db: Session = Depends(get_db)):
    """Return recent interview results and AI feedback for dashboard."""
    safe_limit = max(1, min(limit, 500))

    rows = (
        db.query(InterviewResult)
        .order_by(InterviewResult.created_at.desc(), InterviewResult.id.desc())
        .limit(safe_limit)
        .all()
    )
    items = []
    for r in rows:
        items.append(
            {
                "id": r.id,
                "phone_number": r.phone_number,
                "job_description": r.job_description,
                "answers_json": r.answers_json,
                "feedback_text": r.feedback_text,
                "created_at": r.created_at,
            }
        )
    return {"items": items, "count": len(items)}

# Interview endpoints

@app.post("/api/voice")
async def voice_entrypoint(request: Request):
    """Disable voice calls; this app now supports spreadsheet outreach only."""
    vr = VoiceResponse()
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")

    # Kept below temporarily for reference; the unconditional Hangup above
    # ensures Twilio cannot start an interview or send a missed-call SMS.
    try:
        form = await request.form()
        call_sid = form.get("CallSid")
        from_number = form.get("From")
        to_number = form.get("To")  # candidate phone number
        direction = (form.get("Direction") or "").lower()
        answered_by = (form.get("AnsweredBy") or "").lower()

        # For outbound calls that we initiate via the Twilio API:
        # - From = our Twilio number
        # - To = candidate number
        # For inbound calls:
        # - From = caller/candidate number
        # - To = our Twilio number
        candidate_phone = to_number if direction.startswith("outbound") else from_number

        logger.info(
            f"/api/voice webhook form data: CallSid={call_sid}, From={from_number}, To={to_number}, "
            f"Direction={direction}, AnsweredBy={answered_by}, CandidatePhone={candidate_phone}"
        )

        if not call_sid or not from_number:
            logger.error(f"/api/voice missing CallSid or From in form data: {dict(form)}")
            vr = VoiceResponse()
            vr.say("Sorry, we could not identify your call. Please try again later.", voice="woman")
            vr.hangup()
            return Response(content=str(vr), media_type="application/xml")

        if not candidate_phone:
            logger.error(f"/api/voice missing candidate phone in form data: {dict(form)}")
            vr = VoiceResponse()
            vr.say("Sorry, we could not identify your phone number. Please try again later.", voice="woman")
            vr.hangup()
            return Response(content=str(vr), media_type="application/xml")

        # Determine whether this call is a scheduled interview call.
        # NOTE: Twilio always includes CallSid, so we must check our DB schedule.
        is_scheduled_interview = False
        try:
            if to_number:
                from datetime import datetime, timedelta

                db = SessionLocal()
                try:
                    now_utc = datetime.utcnow()
                    # Allow a small window because the call may start slightly before/after the scheduled time.
                    window_start = now_utc - timedelta(hours=2)
                    existing = (
                        db.query(InterviewSchedule)
                        .filter(
                            InterviewSchedule.candidate_phone == to_number,
                            InterviewSchedule.status.in_(["scheduled", "in_progress"]),
                            InterviewSchedule.scheduled_datetime >= window_start,
                        )
                        .order_by(InterviewSchedule.scheduled_datetime.asc())
                        .first()
                    )
                    is_scheduled_interview = existing is not None
                finally:
                    db.close()
        except Exception as sched_ex:
            logger.error(f"Failed to determine scheduled interview status for {to_number}: {sched_ex}")

        # For scheduled interview calls, if Twilio detected an answering machine/voicemail,
        # leave a short voicemail and hang up instead of starting the full interview.
        # Twilio can send values like: machine_start, machine_end_beep, machine_end_other.
        is_machine_answer = bool(answered_by) and (
            answered_by.startswith("machine") or answered_by == "fax"
        )

        # Treat answering machine as voicemail even if schedule detection fails.
        if is_machine_answer and (is_scheduled_interview or direction.startswith("outbound")):
            logger.info(
                f"Scheduled interview call to {from_number} was answered by machine ({answered_by}); leaving voicemail instead of starting interview."
            )

            # Mark call_pickup = 'No' for this candidate in the schedule table
            try:
                if to_number:
                    from datetime import datetime

                    db = SessionLocal()
                    try:
                        now_utc = datetime.utcnow()
                        existing = (
                            db.query(InterviewSchedule)
                            .filter(
                                InterviewSchedule.candidate_phone == to_number,
                                InterviewSchedule.status.in_(["scheduled", "in_progress"]),
                                InterviewSchedule.scheduled_datetime >= now_utc,
                            )
                            .order_by(InterviewSchedule.scheduled_datetime.asc())
                            .first()
                        )

                        if existing:
                            existing.call_pickup = "No"
                            db.commit()
            
                    finally:
                        db.close()
            except Exception as e:
                logger.error(f"Failed to update call_pickup for missed call to {to_number}: {e}")

            # Send an SMS asking the candidate to provide a new time
            try:
                if to_number:
                    from app.messaging_service import messaging_service

                    messaging_service.send_sms(
                        to_number,
                        (
                            "We tried to reach you but you didn't respond to your interview call. "
                            "Please tell me another suitable time for the interview in date-time-timezone format "
                            "so that I can reschedule your interview again."
                        ),
                    )
            except Exception as sms_err:
                logger.error(f"Failed to send missed-call reschedule SMS to {to_number}: {sms_err}")

            vr = VoiceResponse()
            vr.say(
                "Hello, this is Radixsol calling about your scheduled interview. "
                "We tried to reach you but got your voicemail. "
                "Please reply to our message or email to reschedule a convenient time. Thank you.",
                voice="woman",
            )
            vr.hangup()
            return Response(content=str(vr), media_type="application/xml")

        # If this is a scheduled interview call with a human answer, proceed with the interview.
        if is_scheduled_interview:
            logger.info(f"Proceeding with scheduled interview call to {from_number}, answered_by={answered_by}")

            # Mark call_pickup = 'Yes' and send a short thank-you SMS to the candidate.
            try:
                if to_number:
                    from datetime import datetime

                    db = SessionLocal()
                    try:
                        now_utc = datetime.utcnow()
                        existing = (
                            db.query(InterviewSchedule)
                            .filter(
                                InterviewSchedule.candidate_phone == to_number,
                                InterviewSchedule.status.in_(["scheduled", "in_progress"]),
                                InterviewSchedule.scheduled_datetime >= now_utc,
                            )
                            .order_by(InterviewSchedule.scheduled_datetime.asc())
                            .first()
                        )

                        if existing:
                            existing.call_pickup = "Yes"
                            db.commit()
            
                    finally:
                        db.close()
            except Exception as e:
                logger.error(f"Failed to update call_pickup for answered call to {to_number}: {e}")
        # For non-scheduled calls, if Twilio detected an answering machine/voicemail,
        # leave a short voicemail and hang up.
        elif answered_by and (
            answered_by.startswith("machine")
            or answered_by.startswith("fax")
            or answered_by in {"machine_start", "machine_end", "fax", "fax_start", "fax_end"}
        ):
            vr = VoiceResponse()
            vr.say(
                "Hello, I am calling from Radixsol. We had an interview call scheduled for you. "
                "Please call back to start your interview. Thank you.",
                voice="woman",
            )
            vr.hangup()
            return Response(content=str(vr), media_type="application/xml")

        # Prefer manual-flow context provided by the frontend (stored in memory by /api/sms/send).
        manual_ctx = _get_manual_phone_context(candidate_phone)

        # Otherwise fall back to candidate-specific DB context (CEIPAL runs).
        candidate_job_context = None if manual_ctx else _get_candidate_job_context(candidate_phone)
        job_description = ""
        required_skills: List[str] = []
        required_questions: List[str] = []
        job_title = ""
        certifications: List[str] = []

        if manual_ctx:
            job_description = (manual_ctx.get("job_description") or "").strip()
            required_skills = list(manual_ctx.get("required_skills") or [])
            required_questions = list(manual_ctx.get("required_questions") or [])
            job_title = (manual_ctx.get("job_title") or "").strip()
            certifications = list(manual_ctx.get("certifications") or [])
            logger.info(
                f"/api/voice resolved context source=manual_ctx phone={candidate_phone} job_title={job_title!r} jd_len={len(job_description)}"
            )
        elif candidate_job_context:
            job_description = (candidate_job_context.get("job_description") or "").strip()
            required_skills = list(candidate_job_context.get("required_skills") or [])
            required_questions = list(candidate_job_context.get("required_questions") or [])
            job_title = (candidate_job_context.get("job_title") or "").strip()
            certifications = list(candidate_job_context.get("certifications") or [])
            logger.info(
                f"/api/voice resolved context source=ceipal_db phone={candidate_phone} job_title={job_title!r} jd_len={len(job_description)}"
            )
        else:
            # No context found (manual cache miss and no CEIPAL context). Keep job_description empty
            # and let the final generic fallback apply.
            logger.info(
                f"/api/voice resolved context source=none phone={candidate_phone} (falling back to defaults)"
            )
            pass

        if not job_description:
            # Final fallback to messaging service default or a generic title
            _db = SessionLocal()
            try:
                messaging_service_dep = get_messaging_service(_db)
                job_description = getattr(messaging_service_dep, "default_job_description", "Software Engineer")
            finally:
                _db.close()

        logger.info(
            f"Using job description for /api/voice (len={len(job_description)}): {(job_description[:120] + '...') if len(job_description) > 120 else job_description}"
        )
        twiml = await interview_service.start_interview(
            call_sid,
            candidate_phone,
            job_description,
            job_title=job_title or None,
            required_skills=required_skills,
            required_questions=required_questions,
            certifications=certifications,
        )
        return Response(content=str(twiml), media_type="application/xml")

    except Exception as e:
        logger.error(f"Error in /api/voice entrypoint: {e}", exc_info=True)
        vr = VoiceResponse()
        vr.say("Sorry, we encountered an error starting your interview. Please try again later.", voice="woman")
        vr.hangup()
        return Response(content=str(vr), media_type="application/xml")


def _get_candidate_job_context(phone_number: str) -> Optional[Dict[str, Any]]:
    """Look up candidate-specific job context from the most recent CEIPAL run."""
    try:
        if not phone_number:
            return None
        phone_normalized = _norm_e164(phone_number)
        if not phone_normalized:
            return None
        db = SessionLocal()
        try:
            c_run = (
                db.query(CeipalCandidateRun)
                .filter(
                    CeipalCandidateRun.candidate_phone == phone_normalized,
                    CeipalCandidateRun.job_description.isnot(None),
                )
                .order_by(CeipalCandidateRun.id.desc())
                .first()
            )
            if not c_run:
                return None
            required_skills = []
            required_questions = []
            certifications = []
            try:
                if c_run.required_skills_json:
                    skills = json.loads(c_run.required_skills_json)
                    if isinstance(skills, list):
                        required_skills = skills
            except Exception:
                pass
            try:
                if c_run.required_questions_json:
                    questions = json.loads(c_run.required_questions_json)
                    if isinstance(questions, list):
                        required_questions = questions
            except Exception:
                pass
            try:
                if getattr(c_run, "certifications_json", None):
                    certs = json.loads(c_run.certifications_json)
                    if isinstance(certs, list):
                        certifications = certs
            except Exception:
                pass
            return {
                "job_code": c_run.job_code,
                "job_title": c_run.job_title,
                "job_description": c_run.job_description or "",
                "required_skills": required_skills,
                "required_questions": required_questions,
                "certifications": certifications,
            }
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Failed to get candidate job context for {phone_number}: {e}")
        return None


def _upsert_manual_candidate_job_context(
    *,
    candidate_phone: str,
    candidate_id: Optional[str],
    job_title: str,
    job_description: str,
    required_skills: Optional[List[str]] = None,
    required_questions: Optional[List[str]] = None,
    certifications: Optional[List[str]] = None,
) -> None:
    """Persist manual-flow job context so concurrent calls always route to correct JD.

    We store it into ceipal_candidate_run because /api/voice already looks up candidate
    context from that table, keyed by candidate_phone.
    """
    candidate_phone = _norm_e164(candidate_phone)
    job_title = (job_title or "").strip()
    job_description = (job_description or "").strip()
    if not candidate_phone or not job_description:
        return

    skills = [s.strip() for s in (required_skills or []) if isinstance(s, str) and s.strip()]
    questions = [q.strip() for q in (required_questions or []) if isinstance(q, str) and q.strip()]
    certs = [c.strip() for c in (certifications or []) if isinstance(c, str) and c.strip()]

    db = SessionLocal()
    try:
        # Best effort update: if a manual context already exists for this phone + JD, reuse it.
        existing = (
            db.query(CeipalCandidateRun)
            .filter(
                CeipalCandidateRun.candidate_phone == candidate_phone,
                CeipalCandidateRun.job_description.isnot(None),
            )
            .order_by(CeipalCandidateRun.id.desc())
            .first()
        )

        row = existing or CeipalCandidateRun()
        row.job_run_id = getattr(row, "job_run_id", None)
        row.candidate_id = str(candidate_id) if candidate_id else getattr(row, "candidate_id", None)
        row.candidate_phone = candidate_phone
        row.job_title = job_title or getattr(row, "job_title", None)
        row.job_description = job_description
        row.required_skills_json = json.dumps(skills)
        row.required_questions_json = json.dumps(questions)
        row.certifications_json = json.dumps(certs)

        if not existing:
            db.add(row)
        db.commit()
    except Exception as e:
        logger.warning(f"Failed to persist manual candidate job context for {candidate_phone}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


@app.post("/api/interview/start")
async def start_interview(
    request: Request,
    call_sid: str = Form(...),
    from_number: str = Form(...)
):
    """Disabled legacy interview endpoint."""
    vr = VoiceResponse()
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")

    # Legacy flow retained below; unreachable while spreadsheet-only mode is active.
    try:
        # Prefer manual-flow context provided by the frontend (stored in memory by /api/sms/send).
        manual_ctx = _get_manual_phone_context(from_number)

        # Otherwise fall back to CEIPAL DB context.
        candidate_job_context = None if manual_ctx else _get_candidate_job_context(from_number)
        
        if manual_ctx:
            job_description = manual_ctx.get("job_description") or ""
            required_skills = manual_ctx.get("required_skills") or []
            required_questions = manual_ctx.get("required_questions") or []
            job_title = (manual_ctx.get("job_title") or "").strip() or None
            certifications = manual_ctx.get("certifications") or []
            important_questions = []
        elif candidate_job_context:
            # Use the candidate's specific job context (from CEIPAL batch)
            job_description = candidate_job_context.get("job_description") or ""
            required_skills = candidate_job_context.get("required_skills") or []
            required_questions = candidate_job_context.get("required_questions") or []
            job_title = (candidate_job_context.get("job_title") or "").strip() or None
            certifications = candidate_job_context.get("certifications") or []
            important_questions = []
        else:
            # No context found (manual cache miss and no CEIPAL context). Use a generic fallback.
            important_questions = []
            job_description = "Software Engineer"
            required_skills = []
            required_questions = []
            job_title = None
            certifications = []

        # Start the interview with candidate-specific or fallback context
        return await interview_service.start_interview(
            call_sid,
            from_number,
            job_description,
            job_title=job_title,
            important_questions=important_questions,
            required_skills=required_skills,
            required_questions=required_questions,
            certifications=certifications,
        )
    except Exception as e:
        logger.error(f"Error starting interview: {e}")
        response = VoiceResponse()
        response.say("Sorry, we encountered an error starting your interview. Please try again later.", voice='woman')
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

@app.post("/api/interview/question")
async def ask_question(
    request: Request,
    call_sid: str = Query(..., alias="call_sid")
):
    """Disabled legacy interview endpoint."""
    vr = VoiceResponse()
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")

    # Legacy flow retained below; unreachable while spreadsheet-only mode is active.
    try:
        form_data = await request.form()
        user_response = form_data.get('SpeechResult')
        confidence = form_data.get('Confidence')
        return await interview_service.handle_question(call_sid, user_response=user_response, confidence=confidence)
    except Exception as e:
        logger.error(f"Error in interview question flow: {e}")
        response = VoiceResponse()
        response.say("Sorry, we encountered an error. Please try again later.", voice='woman')
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

@app.post("/api/interview/answer")
async def handle_answer(
    request: Request,
    call_sid: str = Query(..., alias="call_sid")
):
    """Disabled legacy interview endpoint."""
    vr = VoiceResponse()
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")

    # Legacy flow retained below; unreachable while spreadsheet-only mode is active.
    try:
        form_data = await request.form()
        user_response = form_data.get('SpeechResult')
        confidence = form_data.get('Confidence')
        return await interview_service.handle_question(call_sid, user_response=user_response, confidence=confidence)
    except Exception as e:
        logger.error(f"Error handling interview answer: {e}")
        response = VoiceResponse()
        response.say("Sorry, we encountered an error. Please try again.", voice='woman')
        response.redirect(f"/api/interview/question?call_sid={call_sid}", method='POST')
        return Response(content=str(response), media_type="application/xml")

@app.post("/api/interview/timeout")
async def handle_timeout(
    request: Request,
    call_sid: str = Query(..., alias="call_sid")
):
    """Disabled legacy interview endpoint."""
    vr = VoiceResponse()
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")

    # Legacy flow retained below; unreachable while spreadsheet-only mode is active.
    try:
        # Treat as no-input and let InterviewService decide whether to repeat
        # the same question, move on, or end the call.
        form_data = await request.form()
        confidence = form_data.get('Confidence')
        return await interview_service.handle_timeout(call_sid, confidence=confidence)
    except Exception as e:
        logger.error(f"Error handling timeout: {e}")
        response = VoiceResponse()
        response.say("Sorry, we encountered an error. The interview will now end.", voice='woman')
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace "*" with your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Mount TTS audio files so Twilio can fetch generated MP3 prompts
app.mount("/tts", StaticFiles(directory=TTS_DIR), name="tts")

# Endpoint to generate AI response (for testing and internal use)
@app.post("/api/ai/chat")
async def chat_with_ai(
    message: str = Body(..., embed=True),
    context: str = Body("", embed=True)
):
    """
    Generate an AI response using Gemini.
    For testing and internal use.
    """
    response = messaging_service.generate_ai_response(message, context)
    return {"response": response}

# Mount static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Initialize email processor
_alembic_running = (os.getenv("ALEMBIC_RUNNING") or "").strip() in {"1", "true", "True"}
if not _alembic_running:
    google_api_key = os.getenv("GOOGLE_API_KEY")
    if not google_api_key:
        raise ValueError("GOOGLE_API_KEY environment variable not set")
        
    # Configure the Gemini client
    genai.configure(api_key=google_api_key)
    email_processor = EmailProcessor(google_api_key=google_api_key)
else:
    email_processor = None

# Global state for TF-IDF
_latest_job_description = ""  # Store the most recent job description
_latest_required_skills: List[str] = []
_latest_required_questions: List[str] = []
_latest_manual_rank_context: Dict[str, Any] = {}
_latest_manual_rank_lock = threading.Lock()
_candidates: List[Dict[str, Any]] = []
_vectorizer = None
_candidate_matrix = None
_st_model = None
_vectorizer: Optional[TfidfVectorizer] = None
_candidate_matrix = None
_candidate_source: str = ""
_candidate_source_file: str = ""
_ceipal_candidates_loaded: bool = False
_st_model: Optional[SentenceTransformer] = None
_candidate_sem: Optional[np.ndarray] = None

# Protects switching between local/CEIPAL candidates and rebuilding vector indices.
_candidates_lock = threading.RLock()

# Prevent CEIPAL scheduled batches from starting while a manual HTTP /rank is executing.
_manual_rank_lock = threading.Lock()


def _load_latest_required_skills_from_file() -> List[str]:
    try:
        if not os.path.exists(REQUIRED_SKILLS_FILE):
            return []
        with open(REQUIRED_SKILLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        cleaned = [str(s).strip() for s in data if s is not None and str(s).strip()]
        return cleaned
    except Exception:
        return []


def _save_latest_required_skills_to_file(skills: List[str]) -> None:
    try:
        cleaned = [str(s).strip() for s in (skills or []) if s is not None and str(s).strip()]
        with open(REQUIRED_SKILLS_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False)
    except Exception:
        pass


def _load_latest_required_questions_from_file() -> List[str]:
    try:
        if not os.path.exists(REQUIRED_QUESTIONS_FILE):
            return []
        with open(REQUIRED_QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        cleaned = [str(s).strip() for s in data if s is not None and str(s).strip()]
        return cleaned[:10]
    except Exception:
        return []


def _save_latest_required_questions_to_file(questions: List[str]) -> None:
    try:
        cleaned = [str(s).strip() for s in (questions or []) if s is not None and str(s).strip()]
        with open(REQUIRED_QUESTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned[:10], f, ensure_ascii=False)
    except Exception:
        pass


def _normalize_question_list(items: Any, limit: int = 6) -> List[str]:
    if items is None:
        return []
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return []

    out: List[str] = []
    for x in items:
        if x is None:
            continue
        s = str(x).strip()
        if not s:
            continue
        s = re.sub(r"\s+", " ", s)
        if len(s) > 160:
            continue
        # Avoid non-questions like boolean flags
        if re.fullmatch(r"(?i)(yes|no|true|false|n/?a)", s):
            continue
        if not s.endswith("?"):
            s = s.rstrip(".") + "?"
        out.append(s)
        if len(out) >= limit:
            break

    # de-dupe while preserving order
    seen = set()
    deduped: List[str] = []
    for s in out:
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(s)
    return deduped


def _validate_skills_against_jd(skills: List[str], job_description: str, *, limit: int = 20) -> List[str]:
    """Drop hallucinated skills by requiring they appear in the JD text.

    This is intentionally strict for CEIPAL ranking inputs because hallucinated skills
    can cause mass-rejection/0 matches.
    """
    if not skills:
        return []
    jd = (job_description or "")
    jd_lc = jd.lower()
    if not jd_lc.strip():
        return _normalize_skill_list(skills, limit=limit)

    cleaned = _normalize_skill_list(skills, limit=50)
    kept: List[str] = []
    for s in cleaned:
        s_lc = s.lower()
        # Only keep if the phrase or its key token appears in the JD.
        if s_lc in jd_lc:
            kept.append(s)
        else:
            # Allow token-level match for short skills/acronyms.
            toks = [t for t in re.findall(r"[a-z0-9]{2,}", s_lc) if t]
            if toks and any(t in jd_lc for t in toks):
                kept.append(s)
        if len(kept) >= limit:
            break
    return kept


def _validate_required_questions_against_jd(questions: List[str], job_description: str, *, limit: int = 6) -> List[str]:
    """Filter required screening questions to reduce off-JD hallucinations.

    We keep only short yes/no-style screening questions that have clear lexical overlap
    with the JD. This is intentionally conservative: if the JD is vague, it's better
    to ask fewer required questions than to ask irrelevant ones.
    """
    if not questions:
        return []
    jd = (job_description or "").lower()
    if not jd.strip():
        return _normalize_question_list(questions, limit=limit)

    # Tokenize JD into a lightweight keyword set.
    # Keep it simple (no external deps) and avoid common filler words.
    stop = {
        "the", "and", "or", "a", "an", "to", "of", "in", "for", "with", "on", "at", "by",
        "from", "as", "is", "are", "be", "will", "must", "required", "preferred", "experience",
        "years", "year", "work", "role", "position", "job", "candidate", "able", "ability",
        "including", "include", "etc",
    }

    jd_tokens = set()
    for tok in re.findall(r"[a-z0-9]{2,}", jd):
        if tok in stop:
            continue
        jd_tokens.add(tok)

    # Also seed with extracted entities that commonly matter.
    try:
        for s in (_extract_certifications_from_jd(job_description) or []):
            for tok in re.findall(r"[a-z0-9]{2,}", str(s).lower()):
                if tok not in stop:
                    jd_tokens.add(tok)
    except Exception:
        pass

    # If JD has extremely few tokens, don't aggressively filter.
    if len(jd_tokens) < 25:
        return _normalize_question_list(questions, limit=limit)

    cleaned = _normalize_question_list(questions, limit=50)
    kept: List[str] = []
    for q in cleaned:
        q_lc = q.lower()
        # Only keep question-like strings.
        if "?" not in q_lc:
            continue
        # Prefer explicit screening framing.
        screening_markers = ["are you", "do you", "can you", "have you", "will you", "would you", "able to"]
        if not any(m in q_lc for m in screening_markers):
            continue
        q_tokens = {t for t in re.findall(r"[a-z0-9]{2,}", q_lc) if t not in stop}
        # Require some overlap with JD tokens to ensure grounding.
        if len(q_tokens.intersection(jd_tokens)) == 0:
            continue
        kept.append(q)
        if len(kept) >= limit:
            break
    return kept


def _load_structured_jd_cache() -> Dict[str, Any]:
    try:
        if not os.path.exists(STRUCTURED_JD_FILE):
            return {}
        with open(STRUCTURED_JD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_structured_jd_cache(cache: Dict[str, Any]) -> None:
    try:
        if not isinstance(cache, dict):
            return
        with open(STRUCTURED_JD_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def _normalize_skill_list(items: Any, limit: int = 20) -> List[str]:
    out: List[str] = []
    banned_substrings = [
        "locals accepted",
        "rate difference",
        "traveler experience",
        "travel experience is required",
        "travel experience required",
        "travel experience",
        "1st time travelers",
        "first time travelers",
        "unit will accept",
        "unit will not accept",
        "not required",
        "required: yes",
        "required: no",
        "shift",
        "weekend requirement",
        "call requirement",
        "float requirement",
        "interview availability",
        "rto",
    ]
    # Generic soft skills or vague traits that do not help ranking and should
    # not be treated as concrete skills.
    generic_soft_skills = {
        "interpersonal skills",
        "organizational skills",
        "written communication skills",
        "verbal communication skills",
        "communication skills",
        "leadership skills",
        "prioritization skills",
        "delegation skills",
        "problem-solving skills",
        "problem solving skills",
        "time management",
        "teamwork",
    }
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return []
    for x in items:
        if x is None:
            continue
        s = str(x).strip()
        if not s:
            continue
        if len(s) > 80:
            continue
        if re.fullmatch(r"(?i)(yes|no|true|false|n/?a)", s):
            continue
        s_lc = s.lower()
        if any(b in s_lc for b in banned_substrings):
            continue
        # Drop explicit years-of-experience phrases from skills; those are
        # modeled separately as min_experience_years.
        if re.search(r"\b\d+\+?\s*years? of experience\b", s_lc):
            continue
        # Drop strings that are primarily license requirements rather than
        # skills (e.g. "RN license", "NY RN license").
        if "license" in s_lc:
            continue
        # Drop very generic soft skills that do not meaningfully impact
        # healthcare matching.
        if s_lc in generic_soft_skills:
            continue
        # Avoid sentence-like or policy-like strings in skills
        if len(s.split()) >= 9 and not re.search(r"\b(\w+\+|[A-Z]{2,}|\d)\b", s):
            continue
        out.append(re.sub(r"\s+", " ", s))
        if len(out) >= limit:
            break
    # de-dupe while preserving order
    seen = set()
    deduped: List[str] = []
    for s in out:
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(s)
    return deduped


def _gemini_extract_structured_jd(
    *,
    job_code: str,
    job_title: str,
    job_description: str,
    primary_skills: str = "",
) -> Optional[Dict[str, Any]]:
    try:
        if not (os.getenv("GOOGLE_API_KEY") or "").strip():
            return None
        if not isinstance(job_description, str) or not job_description.strip():
            return None

        prompt = (
            "You are an expert recruiter assistant. Extract ONLY what is explicitly stated in the job description. "
            "Do NOT invent requirements. Do NOT use general healthcare assumptions.\n\n"
            "Task:\n"
            "1) Extract required_skills and preferred_skills mentioned in the JD (tools/technologies/clinical skills/specialties).\n"
            "2) Extract certifications/licenses mentioned in the JD (e.g. ARRT, BLS, ACLS, RN).\n"
            "3) Extract min_experience_years ONLY if the JD explicitly states a minimum years requirement.\n"
            "4) Extract other REQUIRED non-skill conditions (travel, radius/distance, vaccination, transportation, shift constraints only if explicitly required, etc.) "
            "and convert ONLY those into required_questions.\n\n"
            "Required_questions rules (very important):\n"
            "- Each question MUST be directly grounded in the JD. If the JD does not mention it, do not ask it.\n"
            "- Each question must be yes/no style and reference a concrete JD term (a certification name, a skill, a location, 'travel', etc.).\n"
            "- Keep each question under 140 characters.\n"
            "- Do NOT ask generic questions like 'Are you a team player?' or 'Can you work under pressure?'\n"
            "- If there are no clear required conditions beyond skills/certs/years, return an empty required_questions list.\n\n"
            "Do NOT include job logistics (start date, bill rate, interview availability, etc.) as skills.\n\n"
            "Return ONLY valid JSON with this exact schema:\n"
            "{\n"
            "  \"required_skills\": [string],\n"
            "  \"preferred_skills\": [string],\n"
            "  \"certifications\": [string],\n"
            "  \"required_questions\": [string],\n"
            "  \"min_experience_years\": number|null\n"
            "}\n\n"
            f"JobTitle: {job_title}\n"
            f"PrimarySkills (may be empty): {primary_skills}\n\n"
            "JobDescription:\n"
            f"{job_description}\n"
        )

        try:
            model_name = (os.getenv("GEMINI_JD_MODEL") or "gemini-2.5-flash-lite").strip()
            model = genai.GenerativeModel(model_name)
        except Exception:
            model = genai.GenerativeModel("gemini-2.5-flash-lite")

        resp = model.generate_content(prompt)
        raw = (getattr(resp, "text", None) or "").strip()
        if not raw:
            return None

        # If wrapped in markdown code blocks, unwrap
        if "```" in raw:
            parts = raw.split("```")
            if len(parts) >= 3:
                raw = parts[1].strip()
                if raw.lower().startswith("json"):
                    raw = raw[4:].strip()

        data = json.loads(raw)
        if not isinstance(data, dict):
            return None

        required_skills = _normalize_skill_list(data.get("required_skills"), limit=20)
        preferred_skills = _normalize_skill_list(data.get("preferred_skills"), limit=20)
        certifications = _normalize_skill_list(data.get("certifications"), limit=20)
        required_questions = _normalize_question_list(data.get("required_questions"), limit=12)
        required_questions = _validate_required_questions_against_jd(required_questions, job_description, limit=6)

        min_exp = data.get("min_experience_years")
        if min_exp is None:
            min_exp_val = None
        else:
            try:
                min_exp_val = int(float(min_exp))
                if min_exp_val < 0 or min_exp_val > 50:
                    min_exp_val = None
            except Exception:
                min_exp_val = None

        # If Gemini missed these, fall back to regex for better coverage
        if min_exp_val is None:
            min_exp_val = _extract_min_experience_years_from_jd(job_description)
        if not certifications:
            certifications = _extract_certifications_from_jd(job_description) or []

        # Final grounding pass: for ranking inputs we only want skills explicitly present in the JD.
        required_skills = _validate_skills_against_jd(required_skills, job_description, limit=20)
        preferred_skills = _validate_skills_against_jd(preferred_skills, job_description, limit=20)

        # Keep min_exp conservative: do not exceed the regex-derived minimum when present.
        try:
            regex_min = _extract_min_experience_years_from_jd(job_description)
            if regex_min is not None and min_exp_val is not None and min_exp_val > regex_min:
                min_exp_val = regex_min
        except Exception:
            pass

        return {
            "job_code": job_code,
            "job_title": job_title,
            "required_skills": required_skills,
            "preferred_skills": preferred_skills,
            "certifications": certifications,
            "required_questions": required_questions,
            "min_experience_years": min_exp_val,
        }
    except Exception as ex:
        logger.warning(f"Gemini structured JD extraction failed for job_code={job_code}: {ex}")
        return None

# Define healthcare roles and their required skills
HEALTHCARE_ROLES = {
    'Registered Nurse': [
        'patient care', 'medication administration', 'vital signs', 'wound care', 'IV therapy',
        'patient assessment', 'care planning', 'clinical documentation', 'BLS', 'ACLS',
        'patient education', 'medication management', 'nursing process', 'care coordination'
    ],
    'Physician': [
        'diagnosis', 'treatment planning', 'patient consultation', 'medical history',
        'physical examination', 'medical diagnosis', 'treatment plans', 'prescription',
        'medical procedures', 'patient management', 'clinical research', 'medical records'
    ],
    'Medical Assistant': [
        'vital signs', 'patient intake', 'medical records', 'appointment scheduling',
        'specimen collection', 'EKG', 'phlebotomy', 'injections', 'medical terminology',
        'insurance verification', 'patient preparation', 'clinical procedures'
    ],
    'Physical Therapist': [
        'patient assessment', 'treatment planning', 'therapeutic exercises', 'manual therapy',
        'patient education', 'rehabilitation', 'mobility training', 'pain management',
        'exercise prescription', 'functional training', 'modalities', 'patient evaluation'
    ],
    'Radiologic Technologist': [
        'x-ray', 'radiography', 'patient positioning', 'radiation safety', 'imaging procedures',
        'contrast media', 'patient care', 'equipment operation', 'image quality', 'CT', 'MRI',
        'patient safety', 'medical imaging'
    ]
}

# Add healthcare roles to technical roles list
HEALTHCARE_TITLES = list(HEALTHCARE_ROLES.keys())

# Add healthcare skills to technical skills list
TECHNICAL_SKILLS = [
    'python', 'tensorflow', 'pytorch', 'ml', 'ai', 'nlp', 'computer vision',
    'deep learning', 'machine learning', 'data science', 'big data', 'spark',
    'hadoop', 'sql', 'nosql', 'aws', 'azure', 'gcp', 'docker', 'kubernetes'
] + [skill.lower() for skills in HEALTHCARE_ROLES.values() for skill in skills]


def _load_candidates() -> List[Dict[str, Any]]:
    global _candidate_source
    global _candidate_source_file
    data_file = (os.getenv("CANDIDATE_DATA_FILE") or "").strip() or None
    if not data_file:
        data_json = os.path.join(PROJECT_ROOT, "data.json")
        if os.path.exists(data_json):
            data_file = data_json
    data_file = data_file or DATA_FILE

    logger.info(f"Loading candidates from: {data_file}")
    if not os.path.exists(data_file):
        raise FileNotFoundError(f"Missing data file: {data_file}")
    
    try:
        with open(data_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        logger.info(f"Successfully loaded {len(data)} candidates from {data_file}")
        
        # Log healthcare professionals count
        healthcare_keywords = ['nurse', 'rn', 'lpn', 'lvn', 'cna', 'physician', 'doctor', 'healthcare', 'medical']
        healthcare_professionals = []
        
        # Ensure each candidate has a unique id and required fields
        for i, c in enumerate(data):
            try:
                # Ensure required fields exist with defaults
                c["_id"] = str(c.get("Sr No.", i + 1))
                
                # Handle Skills field
                if "Skills" not in c:
                    c["Skills"] = ""
                
                # Normalize skills into comma-separated string if it's a list
                if isinstance(c["Skills"], list):
                    c["Skills"] = ",".join(str(skill).strip() for skill in c["Skills"] if skill)
                
                # Ensure Skills is a string
                c["Skills"] = str(c["Skills"] or "").strip()
                
                # Check if candidate is a healthcare professional
                job_title = str(c.get("JobTitle", "")).lower()
                skills_lower = c["Skills"].lower()

                # Debug: log Sonali at load time (Sr No. == -2)
                try:
                    if str(c.get("Sr No.")) == "-2":
                        logger.info("DEBUG (_load_candidates) Sonali from JSON: %s", c)
                        logger.info(
                            "DEBUG (_load_candidates) Sonali phone keys at load: phone=%s, Phone=%s, PhoneNumber=%s, Phone_No=%s, ContactNumber=%s",
                            c.get("phone"),
                            c.get("Phone"),
                            c.get("PhoneNumber"),
                            c.get("Phone_No"),
                            c.get("ContactNumber"),
                        )
                except Exception as load_debug_ex:
                    logger.warning("DEBUG (_load_candidates) logging for Sonali failed: %s", load_debug_ex)
                
                if any(keyword in job_title or keyword in skills_lower for keyword in healthcare_keywords):
                    healthcare_professionals.append({
                        'id': c["_id"],
                        'name': f"{c.get('FirstName', '').strip()} {c.get('LastName', '').strip()}".strip(),
                        'title': c.get("JobTitle", "").strip(),
                        'skills': c["Skills"]
                    })
            except Exception as e:
                logger.warning(f"Error processing candidate {i+1}: {str(e)}")
                continue
        
        logger.info(f"Found {len(healthcare_professionals)} healthcare professionals in the dataset")
        if healthcare_professionals:
            logger.info("Sample healthcare professionals:")
            for i, hp in enumerate(healthcare_professionals[:3]):  # Log first 3 as sample
                logger.info(f"  {i+1}. {hp['name']} - {hp['title']}")
                logger.info(f"     Skills: {hp['skills']}")

        _candidate_source = "local"
        _candidate_source_file = str(data_file)
        
        return data

    except json.JSONDecodeError as e:
        logger.error(f"Error parsing JSON file: {e}")
        raise HTTPException(status_code=500, detail=f"Invalid JSON data in {DATA_FILE}")
    except Exception as e:
        logger.error(f"Error loading candidates: {str(e)}")
        raise


def _split_full_name(name: str) -> Dict[str, str]:
    name = (name or "").strip()
    if not name:
        return {"FirstName": "", "LastName": ""}
    parts = [p for p in name.split() if p]
    if len(parts) == 1:
        return {"FirstName": parts[0], "LastName": ""}
    return {"FirstName": " ".join(parts[:-1]), "LastName": parts[-1]}


def _normalize_phone(phone: Any) -> str:
    if phone is None:
        return ""
    return _norm_e164(str(phone))


def _load_candidates_from_ceipal() -> List[Dict[str, Any]]:
    global _candidate_source
    global _candidate_source_file
    client = build_ceipal_client_from_env()
    report_url = (os.getenv("CEIPAL_CANDIDATE_REPORT_URL") or "").strip()
    if not report_url:
        raise ValueError("CEIPAL_CANDIDATE_REPORT_URL must be set")

    max_pages = int(os.getenv("CEIPAL_CANDIDATE_MAX_PAGES", "50") or "50")
    throttle_seconds = float(os.getenv("CEIPAL_REPORT_THROTTLE_SECONDS", "0.25") or "0.25")
    throttle_seconds = max(0.0, min(throttle_seconds, 5.0))
    page = 1
    fetched_pages = 0
    candidates: List[Dict[str, Any]] = []

    # Prefer latest candidates by jumping to the last N pages, since CEIPAL BI reports
    # often show newest records at the bottom.
    fetch_order = (os.getenv("CEIPAL_CANDIDATES_FETCH_ORDER") or "latest").strip().lower() or "latest"

    def _with_page(url: str, page_num: int) -> str:
        parts = urlsplit(url)
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        q["page"] = str(max(1, int(page_num)))
        new_q = urlencode(q, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_q, parts.fragment))

    start_page: Optional[int] = None
    total_pages: Optional[int] = None

    next_url: Optional[str] = report_url
    while next_url and fetched_pages < max_pages:
        try:
            if fetched_pages == 0 or max_pages <= 10 or (fetched_pages % 10) == 0:
                logger.info(f"CEIPAL candidates: fetching page={page} ({fetched_pages + 1}/{max_pages})")
        except Exception:
            pass
        if throttle_seconds and page > 1:
            time.sleep(throttle_seconds)
        payload = client.get_report_data(next_url)
        data = payload.get("result") if isinstance(payload, dict) else None
        if data is None and isinstance(payload, dict):
            data = payload.get("data", {}).get("result")

        # Handle the common shape returned by our CEIPAL debug endpoint as well.
        if isinstance(payload, dict) and "data" in payload and isinstance(payload.get("data"), dict):
            ceipal_data = payload["data"]
        else:
            ceipal_data = payload if isinstance(payload, dict) else {}

        # One-time: compute total pages and jump to last pages if configured.
        if fetch_order in {"latest", "newest", "desc", "bottom"} and start_page is None:
            try:
                rc_raw = ceipal_data.get("record_count") if isinstance(ceipal_data, dict) else None
                limit_raw = ceipal_data.get("limit") if isinstance(ceipal_data, dict) else None
                page_count_raw = ceipal_data.get("page_count") if isinstance(ceipal_data, dict) else None

                rc = int(rc_raw) if rc_raw not in (None, "") else 0
                lim = int(limit_raw) if limit_raw not in (None, "") else 0
                pc = int(page_count_raw) if page_count_raw not in (None, "") else 0

                if rc > 0 and lim > 0:
                    total_pages = int(math.ceil(rc / float(lim)))
                elif pc > 0 and lim > 0 and rc == 0:
                    total_pages = int(math.ceil(pc / float(lim)))
                elif pc > 0 and lim == 0:
                    total_pages = pc

                if total_pages and total_pages > 1:
                    start_page = max(1, (total_pages - max(1, max_pages) + 1))
                    logger.info(
                        f"CEIPAL candidates: report appears to have total_pages={total_pages}; fetching newest pages {start_page}..{total_pages}"
                    )
                    page = start_page
                    next_url = _with_page(report_url, page)
                    continue
            except Exception:
                start_page = 1

        result_rows = ceipal_data.get("result") if isinstance(ceipal_data, dict) else None
        if not isinstance(result_rows, list):
            break

        for r in result_rows:
            if not isinstance(r, dict):
                continue
            name_parts = _split_full_name(str(r.get("ApplicantName") or ""))
            c = {
                "_id": str(r.get("ApplicantID") or "").strip() or None,
                "FirstName": name_parts["FirstName"],
                "LastName": name_parts["LastName"],
                "ApplicantName": str(r.get("ApplicantName") or "").strip(),
                "JobTitle": str(r.get("JobTitle") or "").strip(),
                "Skills": str(r.get("Skills") or "").strip(),
                "Experience": str(r.get("Experience") or "").strip(),
                "City": str(r.get("City") or "").strip(),
                "State": str(r.get("State") or "").strip(),
                "Country": str(r.get("Country") or "").strip(),
                "Email": str(r.get("EmailAddress") or "").strip(),
                "EmailAddress": str(r.get("EmailAddress") or "").strip(),
                "AlternateEmailAddress": str(r.get("AlternateEmailAddress") or "").strip(),
                "PhoneNumber": _normalize_phone(r.get("MobileNumber")),
                "MobileNumber": _normalize_phone(r.get("MobileNumber")),
                "LinkedInProfileURL": str(r.get("LinkedInProfileURL") or "").strip(),
                "WorkAuthorization": str(r.get("WorkAuthorization") or "").strip(),
                "Address": str(r.get("Address") or "").strip(),
            }

            # Fallback id
            if not c.get("_id"):
                c["_id"] = str(uuid.uuid4())

            candidates.append(c)

        fetched_pages += 1

        # Advance page.
        page += 1

        # If we are fetching newest pages by computing total_pages, use explicit page stepping
        # rather than depending on next_page.
        if total_pages is not None and start_page is not None:
            if page > total_pages:
                next_url = None
            else:
                next_url = _with_page(report_url, page)
        else:
            has_next = int(ceipal_data.get("has_next_page") or 0) if isinstance(ceipal_data, dict) else 0
            next_url = (ceipal_data.get("next_page") or "").strip() if has_next else ""
            next_url = next_url or None

    logger.info(f"Loaded {len(candidates)} candidates from CEIPAL report")
    # Deduplicate and clean
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for c in candidates:
        cid = str(c.get("_id") or "").strip()
        if not cid:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        unique.append(c)

    _candidate_source = "ceipal"
    _candidate_source_file = "CEIPAL"
    return unique


def _load_jobs_from_ceipal() -> List[Dict[str, Any]]:
    client = build_ceipal_client_from_env()
    report_url = (
        (os.getenv("CEIPAL_JOB_REPORT_URL") or "").strip()
        or (os.getenv("CEIPAL_JD_REPORT_URL") or "").strip()
    )
    if not report_url:
        raise ValueError("CEIPAL_JOB_REPORT_URL or CEIPAL_JD_REPORT_URL must be set")

    max_pages = int(os.getenv("CEIPAL_JD_MAX_PAGES", "50") or "50")
    throttle_seconds = float(os.getenv("CEIPAL_REPORT_THROTTLE_SECONDS", "0.25") or "0.25")
    throttle_seconds = max(0.0, min(throttle_seconds, 5.0))
    page = 1
    fetched_pages = 0
    jobs: List[Dict[str, Any]] = []

    # Prefer latest jobs by jumping to the last N pages, since CEIPAL BI reports
    # often show newest records at the bottom.
    fetch_order = (os.getenv("CEIPAL_JOBS_FETCH_ORDER") or "latest").strip().lower() or "latest"

    def _with_page(url: str, page_num: int) -> str:
        parts = urlsplit(url)
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        q["page"] = str(max(1, int(page_num)))
        new_q = urlencode(q, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_q, parts.fragment))

    start_page: Optional[int] = None
    total_pages: Optional[int] = None

    next_url: Optional[str] = report_url
    while next_url and fetched_pages < max_pages:
        try:
            if fetched_pages == 0 or max_pages <= 10 or (fetched_pages % 10) == 0:
                logger.info(f"CEIPAL jobs: fetching page={page} ({fetched_pages + 1}/{max_pages})")
        except Exception:
            pass
        if throttle_seconds and page > 1:
            time.sleep(throttle_seconds)
        payload = client.get_report_data(next_url)
        if isinstance(payload, dict) and "data" in payload and isinstance(payload.get("data"), dict):
            ceipal_data = payload["data"]
        else:
            ceipal_data = payload if isinstance(payload, dict) else {}

        # One-time: compute total pages and jump to last pages if configured.
        if fetch_order in {"latest", "newest", "desc", "bottom"} and start_page is None:
            try:
                # Common CEIPAL fields: record_count, limit/page_count.
                rc_raw = ceipal_data.get("record_count") if isinstance(ceipal_data, dict) else None
                limit_raw = ceipal_data.get("limit") if isinstance(ceipal_data, dict) else None
                page_count_raw = ceipal_data.get("page_count") if isinstance(ceipal_data, dict) else None

                rc = int(rc_raw) if rc_raw not in (None, "") else 0
                lim = int(limit_raw) if limit_raw not in (None, "") else 0
                pc = int(page_count_raw) if page_count_raw not in (None, "") else 0

                if rc > 0 and lim > 0:
                    total_pages = int(math.ceil(rc / float(lim)))
                elif pc > 0 and lim > 0 and rc == 0:
                    # Some payloads use page_count as record_count; keep as fallback.
                    total_pages = int(math.ceil(pc / float(lim)))
                elif pc > 0 and lim == 0:
                    # Some payloads use page_count as total pages.
                    total_pages = pc

                if total_pages and total_pages > 1:
                    # Fetch the last `max_pages` pages (or all if max_pages is huge).
                    start_page = max(1, (total_pages - max(1, max_pages) + 1))
                    logger.info(
                        f"CEIPAL jobs: report appears to have total_pages={total_pages}; fetching newest pages {start_page}..{total_pages}"
                    )
                    # Reset loop to start from the computed page.
                    page = start_page
                    next_url = _with_page(report_url, page)
                    continue
            except Exception:
                start_page = 1

        result_rows = ceipal_data.get("result") if isinstance(ceipal_data, dict) else None
        if not isinstance(result_rows, list):
            break

        for r in result_rows:
            if not isinstance(r, dict):
                continue

            job_code = str(r.get("JobCode") or r.get("JobID") or r.get("JobId") or "").strip()
            title = str(r.get("JobTitle") or r.get("Title") or "").strip()

            # Common description field names seen in CEIPAL custom reports
            description = (
                r.get("JobDescription")
                or r.get("Description")
                or r.get("Job_Description")
                or r.get("Job Description")
                or r.get("JD")
                or ""
            )
            description_str = str(description or "").strip()

            if not description_str:
                loc = str(r.get("Location") or "").strip()
                client_name = str(r.get("Client") or "").strip()
                end_client_name = str(r.get("EndClient") or "").strip()
                parts = [
                    f"Role: {title}" if title else "",
                    f"Location: {loc}" if loc else "",
                    f"Client: {client_name}" if client_name else "",
                    f"End Client: {end_client_name}" if end_client_name else "",
                    "Job description not provided in CEIPAL report.",
                ]
                description_str = "\n".join([p for p in parts if p]).strip()

            jobs.append(
                {
                    "JobCode": job_code,
                    "JobTitle": title,
                    "JobStatus": str(r.get("JobStatus") or "").strip(),
                    "Location": str(r.get("Location") or "").strip(),
                    "Client": str(r.get("Client") or "").strip(),
                    "EndClient": str(r.get("EndClient") or "").strip(),
                    "JobCreated": str(r.get("JobCreated") or "").strip(),
                    "JobStartDate": str(r.get("JobStartDate") or "").strip(),
                    "JobEndDate": str(r.get("JobEndDate") or "").strip(),
                    "JobDescription": description_str,
                }
            )

        fetched_pages += 1

        # Advance page.
        page += 1

        # If we are fetching newest pages by computing total_pages, use explicit page stepping
        # rather than depending on next_page, so we can jump directly to the last pages.
        if total_pages is not None and start_page is not None:
            if page > total_pages:
                next_url = None
            else:
                next_url = _with_page(report_url, page)
        else:
            has_next = int(ceipal_data.get("has_next_page") or 0) if isinstance(ceipal_data, dict) else 0
            next_url = (ceipal_data.get("next_page") or "").strip() if has_next else ""
            next_url = next_url or None

    # Sort newest-first by numeric job code when available.
    def _job_code_num(j: Dict[str, Any]) -> int:
        raw = str(j.get("JobCode") or "")
        m = re.search(r"(\d+)", raw)
        try:
            return int(m.group(1)) if m else 0
        except Exception:
            return 0

    try:
        jobs.sort(key=_job_code_num, reverse=True)
    except Exception:
        pass

    logger.info(f"Loaded {len(jobs)} jobs from CEIPAL report")
    return jobs


def _extract_min_experience_years_from_jd(jd: str) -> Optional[int]:
    if not isinstance(jd, str) or not jd.strip():
        return None
    text = jd
    matches = re.findall(r"\b(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", text, flags=re.IGNORECASE)
    years = []
    for m in matches:
        try:
            v = int(m)
            if 0 < v < 60:
                years.append(v)
        except Exception:
            continue
    return max(years) if years else None


def _extract_certifications_from_jd(jd: str) -> List[str]:
    if not isinstance(jd, str) or not jd.strip():
        return []
    text = jd
    known = {
        "RN",
        "LPN",
        "LVN",
        "CNA",
        "BSN",
        "MSN",
        "ACLS",
        "BLS",
        "PALS",
        "NRP",
        "CEN",
        "CCRN",
        "TNCC",
        "NIHSS",
        "CPI",
        "ARRT",
        "CMA",
        "RMA",
        "CPR",
    }
    found = set()
    for token in re.findall(r"\b[A-Z]{2,6}\b", text):
        if token in known:
            found.add(token)
    for m in re.findall(r"\b(?:certified|certification)\s*[:\-]\s*([^\n\r]{1,120})", text, flags=re.IGNORECASE):
        for part in re.split(r"[,/;|]", m):
            t = part.strip()
            if not t:
                continue
            up = re.sub(r"\s+", " ", t).upper()
            if 1 < len(up) <= 12 and re.fullmatch(r"[A-Z0-9\- ]+", up):
                found.add(up)
    return sorted(found)


def _extract_required_skills_from_jd(jd: str, limit: int = 20) -> List[str]:
    if not isinstance(jd, str) or not jd.strip():
        return []
    text = jd
    skills = []

    # Only extract from explicit "required skills" section headers.
    # Do NOT fall back to the whole JD, otherwise we end up mixing in position
    # details (start date, shift, on-site, etc.) as "skills".
    m = re.search(
        r"(?is)(?:^|\n)\s*(?:required\s+skills?(?:\s*&\s*certifications?)?|required\s+skills?\s+and\s+certifications?|required\s+qualifications?|skills\s*&\s*certifications?)\s*[:\-]\s*(.{0,1600})",
        text,
        flags=re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    snippet = m.group(1) if m else ""

    # CEIPAL JDs often don't have a clean "Required Skills" header. If we didn't
    # match a strict header, fall back to a conservative heuristic: look for
    # "required/requirements/certs" lines and take nearby bullets/short lines.
    if not snippet:
        lines = re.sub(r"\r\n", "\n", text).split("\n")
        picked: List[str] = []
        capture = False
        for raw in lines[:120]:
            s = (raw or "").strip()
            if not s:
                if capture:
                    capture = False
                continue

            s_lc = s.lower()

            # Start capturing after requirement/cert markers
            if re.search(r"\b(required|requirements|qualifications|certs|certifications|licenses)\b", s_lc):
                capture = True
                # If it's a "Header: items" line, keep the items part
                if ":" in s and len(s.split(":", 1)[0]) <= 32:
                    picked.append(s.split(":", 1)[1].strip())
                continue

            if capture:
                # Common bullet patterns in CEIPAL/ATS dumps
                if re.match(r"^(?:[-•*]|\d+[\.)])\s+", s):
                    picked.append(re.sub(r"^(?:[-•*]|\d+[\.)])\s+", "", s).strip())
                    continue
                # Also allow short non-bullet lines right after the header
                if len(s) <= 80:
                    picked.append(s)
                    continue

        snippet = "\n".join(picked)
        if not snippet.strip():
            return []
    # Stop at the next common section header to avoid capturing unrelated content.
    snippet = re.split(
        r"(?im)^\s*(?:responsibilities|position\s+details|job\s+details|duties|location|shift|bill\s+rate|apply|about\s+the\s+role|summary|description)\s*[:\-]",
        snippet,
        maxsplit=1,
    )[0]
    snippet = re.split(r"\n\s*\n", snippet, maxsplit=1)[0]

    snippet = re.sub(r"\r\n", "\n", snippet)
    lines = snippet.split("\n")
    collected = []
    for line in lines[:40]:
        s = line.strip(" \t-•*\u2022")
        if not s:
            continue
        if len(s) > 120:
            continue
        # Drop obvious non-skill labels that appear in some templates.
        if re.search(
            r"(?i)\b(start\s+date|end\s+date|location|shift|bill\s+rate|role\b|on[-\s]?site|remote|travel|weekend|years\s+of\s+experience|first[-\s]?timers|on[-\s]?call)\b",
            s,
        ):
            continue
        collected.append(s)

    for line in collected:
        for part in re.split(r"[,/;|]", line):
            token = part.strip()
            if not token:
                continue
            token = re.sub(r"\s+", " ", token)
            # If a template line has a "Label: value" shape, prefer the value.
            if ":" in token and len(token.split(":", 1)[0]) <= 28:
                token = token.split(":", 1)[1].strip()
            if len(token) < 2 or len(token) > 60:
                continue
            if re.fullmatch(r"(?i)(yes|no|true|false|n/?a)", token):
                continue
            skills.append(token)

    de_duped = []
    seen = set()
    for s in skills:
        k = s.lower()
        if k not in seen:
            seen.add(k)
            de_duped.append(s)
    return de_duped[:limit]


def _get_structured_requirements_for_job(j: Dict[str, Any]) -> Dict[str, Any]:
    job_code = str(j.get("JobCode") or "").strip()
    job_title = str(j.get("JobTitle") or "").strip()
    jd = str(j.get("JobDescription") or "").strip()
    primary_skills = str(j.get("PrimarySkills") or "").strip()

    # Cache by job_code (best effort)
    cache = _load_structured_jd_cache()
    cache_key = job_code or f"title::{job_title}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cached.get("required_skills") is not None:
        return cached

    extracted = _gemini_extract_structured_jd(
        job_code=job_code,
        job_title=job_title,
        job_description=jd,
        primary_skills=primary_skills,
    )
    if extracted:
        cache[cache_key] = extracted
        _save_structured_jd_cache(cache)
        return extracted

    # Fallback to regex-based extraction
    primary_skill_fallback: List[str] = []
    try:
        if primary_skills:
            # Accept common delimiters in CEIPAL primary skills.
            parts = [p.strip() for p in re.split(r"[,/;|\n]+", primary_skills) if p.strip()]
            primary_skill_fallback = _normalize_skill_list(parts, limit=20)
    except Exception:
        primary_skill_fallback = []

    fallback = {
        "job_code": job_code,
        "job_title": job_title,
        "required_skills": (_extract_required_skills_from_jd(jd) or []) or primary_skill_fallback,
        "preferred_skills": [],
        "certifications": _extract_certifications_from_jd(jd) or [],
        "required_questions": [],
        "min_experience_years": _extract_min_experience_years_from_jd(jd),
    }
    cache[cache_key] = fallback
    _save_structured_jd_cache(cache)
    return fallback


def _get_structured_requirements_for_manual_jd(job_title: str, jd: str) -> Dict[str, Any]:
    """Best-effort structured requirement extraction for manual /rank flows.

    Uses the same Gemini extraction + validation logic as CEIPAL, but caches by a stable
    hash of the JD text to avoid repeated LLM calls.
    """
    job_title = str(job_title or "").strip()
    jd = str(jd or "").strip()

    cache = _load_structured_jd_cache()
    jd_hash = hashlib.sha256(jd.encode("utf-8")).hexdigest()[:16] if jd else "empty"
    cache_key = f"manual::{jd_hash}::{job_title}" if job_title else f"manual::{jd_hash}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and cached.get("required_questions") is not None:
        return cached

    extracted = _gemini_extract_structured_jd(
        job_code="",
        job_title=job_title,
        job_description=jd,
        primary_skills="",
    )
    if extracted:
        # Ensure required_questions are always validated and capped.
        extracted["required_questions"] = _validate_required_questions_against_jd(
            _normalize_question_list(extracted.get("required_questions"), limit=12),
            jd,
            limit=6,
        )
        cache[cache_key] = extracted
        _save_structured_jd_cache(cache)
        return extracted

    # Fallback: keep empty required_questions so we don't inject irrelevant prompts.
    fallback = {
        "job_code": "",
        "job_title": job_title,
        "required_skills": [],
        "preferred_skills": [],
        "certifications": _extract_certifications_from_jd(jd) or [],
        "required_questions": [],
        "min_experience_years": _extract_min_experience_years_from_jd(jd),
    }
    cache[cache_key] = fallback
    _save_structured_jd_cache(cache)
    return fallback



def _candidate_text_blob(c: Dict[str, Any]) -> str:
    """Create a searchable text blob from candidate data."""
    parts = []
    # Include all relevant fields for search
    fields = [
        "FirstName", "LastName", "JobTitle", "Skills", "Experience",
        "City", "State", "Country", "Summary", "Certifications",
        "Education", "PreviousTitles", "Technologies"
    ]
    
    for key in fields:
        val = c.get(key)
        if val is None:
            continue
            
        # Convert lists to comma-separated strings
        if isinstance(val, list):
            val = ", ".join(str(v) for v in val if v)
        
        # Add field name as prefix to boost exact matches
        parts.append(f"{key}: {val}")
    
    # Add full name as a separate field for better name matching
    full_name = f"{c.get('FirstName', '')} {c.get('LastName', '')}".strip()
    if full_name:
        parts.append(f"FullName: {full_name}")
    
    return " ".join(parts)


def _build_fallback_jd_for_ceipal_job(j: Dict[str, Any]) -> str:
    """When CEIPAL BI report lacks JobDescription, synthesize a minimal JD.

    This keeps ranking and interview flows functional and avoids skipping jobs.
    """
    job_title = str(j.get("JobTitle") or "").strip()
    location = str(j.get("Location") or "").strip()
    primary_skills = str(j.get("PrimarySkills") or "").strip()

    parts: List[str] = []
    if job_title:
        parts.append(f"Role: {job_title}.")
    if location:
        parts.append(f"Location: {location}.")
    if primary_skills:
        parts.append(f"Primary skills: {primary_skills}.")
    if not parts:
        return "Job description unavailable."
    # Make it long enough to pass downstream heuristics and produce stable vectorization.
    parts.append("Job description was not provided by CEIPAL export. Use the role title and skills above.")
    return " ".join(parts).strip()


def _build_vector_index(candidates: List[Dict[str, Any]]):
    global _vectorizer, _candidate_matrix, _st_model, _candidate_sem
    texts = [_candidate_text_blob(c) for c in candidates]
    _vectorizer = TfidfVectorizer(stop_words="english")
    _candidate_matrix = _vectorizer.fit_transform(texts)
    # Build semantic embeddings (normalized) using a compact model
    try:
        _st_model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
        emb = _st_model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        _candidate_sem = emb.astype(np.float32)
    except Exception:
        # If model load fails, leave semantic as None
        _st_model = None
        _candidate_sem = None


# SQLAlchemy models (use shared Base from app.database)
class BestList(Base):
    __tablename__ = "best_list"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True)
    created_at = Column(DateTime, server_default=func.now())


class FinalSelectedCandidate(Base):
    __tablename__ = "final_selected_candidates"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(String(255), nullable=True, index=True)
    email = Column(String(255), nullable=True, index=True)
    phone = Column(String(50), nullable=True, index=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class JobConfig(Base):
    __tablename__ = "job_config"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), default="")
    description = Column(Text, default="")
    important_questions_json = Column(Text, default="[]")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SmsLog(Base):
    __tablename__ = "sms_log"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(String(255), nullable=True)
    phone = Column(String(50), index=True)
    direction = Column(String(20), default="outgoing")  # outgoing / incoming
    message = Column(Text)
    status = Column(String(20), default="sent")
    provider_sid = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class InterviewResult(Base):
    __tablename__ = "interview_results"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String(50), index=True)
    job_description = Column(Text)
    answers_json = Column(Text)
    feedback_text = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

class BestListItem(Base):
    __tablename__ = "best_list_item"
    id = Column(Integer, primary_key=True, index=True)
    list_id = Column(Integer, ForeignKey("best_list.id", ondelete="CASCADE"))
    candidate_id = Column(String(255))
    added_at = Column(DateTime, server_default=func.now())

class EmailLog(Base):
    __tablename__ = "email_log"
    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(String(255), index=True)
    subject = Column(String(255))
    body = Column(Text)
    status = Column(String(50))
    provider_message_id = Column(String(255))
    created_at = Column(DateTime, server_default=func.now())

class InterviewAvailability(Base):
    __tablename__ = "interview_availability"
    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(String(255), index=True)
    candidate_name = Column(String(255))
    role = Column(String(255))
    phone = Column(String(50))
    availability_json = Column(Text)
    raw_message = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class CeipalJobRun(Base):
    __tablename__ = "ceipal_job_run"

    id = Column(Integer, primary_key=True, index=True)
    job_code = Column(String(255), index=True)
    job_title = Column(String(255))
    job_status = Column(String(50))
    status = Column(String(50), default="pending")
    score_threshold = Column(String(50), default="0.5")
    total_candidates = Column(Integer, default=0)
    selected_candidates = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, server_default=func.now())
    finished_at = Column(DateTime, nullable=True)


class CeipalBatchRun(Base):
    __tablename__ = "ceipal_batch_run"

    id = Column(String(64), primary_key=True, index=True)
    status = Column(String(50), default="queued")
    request_json = Column(Text, nullable=True)
    request_hash = Column(String(64), nullable=True, index=True)
    jobs_processed = Column(Integer, default=0)
    candidates_selected = Column(Integer, default=0)
    messages_attempted = Column(Integer, default=0)
    messages_sent = Column(Integer, default=0)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class CeipalCandidateRun(Base):
    __tablename__ = "ceipal_candidate_run"

    id = Column(Integer, primary_key=True, index=True)
    job_run_id = Column(Integer, ForeignKey("ceipal_job_run.id", ondelete="CASCADE"), index=True)
    candidate_id = Column(String(255), index=True)
    candidate_name = Column(String(255), nullable=True)
    candidate_email = Column(String(255), nullable=True)
    candidate_phone = Column(String(50), nullable=True)
    score = Column(String(50), default="0")
    sms_status = Column(String(50), default="not_sent")
    email_status = Column(String(50), default="not_sent")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    # Job context for correct interview routing (Option A)
    job_code = Column(String(255), nullable=True, index=True)
    job_title = Column(String(255), nullable=True)
    job_description = Column(Text, nullable=True)
    required_skills_json = Column(Text, nullable=True)  # JSON array
    required_questions_json = Column(Text, nullable=True)  # JSON array
    certifications_json = Column(Text, nullable=True)  # JSON array


def _init_db():
    # Create all tables (dev convenience). In production prefer Alembic migrations.
    auto_create = (os.getenv("DB_AUTO_CREATE_TABLES") or "").strip().lower() in {"1", "true", "yes", "y"}
    if auto_create:
        Base.metadata.create_all(bind=engine)


class RankRequest(BaseModel):
    job_description: str
    top_k: Optional[int] = None  # Make top_k optional with no default limit
    role_title: Optional[str] = None
    required_skills: Optional[List[str]] = None
    min_experience_years: Optional[int] = None
    certifications: Optional[List[str]] = None


class RankResult(BaseModel):
    id: str
    name: str
    email: Optional[str]
    phone: Optional[str] = None
    job_title: Optional[str]
    city: Optional[str]
    state: Optional[str]
    country: Optional[str]
    experience: Optional[str]
    skills: Optional[str]
    score: float
    matched_skills: Optional[List[str]] = None
    mapped_role: Optional[str] = None


class BestListRequest(BaseModel):
    list_name: str
    candidate_ids: List[str]


class EmailRequest(BaseModel):
    candidate_ids: List[str]
    subject: Optional[str] = None
    body_html: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_pass: Optional[str] = None
    smtp_from: Optional[str] = None


class CeipalProcessRequest(BaseModel):
    score_threshold: float = 0.4
    max_jobs: Optional[int] = None
    job_status: Optional[str] = "Open,Active"
    dry_run: bool = False
    force_resend: bool = False
    resend_after_hours: int = 168


class CeipalProcessResponse(BaseModel):
    status: str
    jobs_processed: int
    candidates_selected: int
    job_run_ids: List[int]


class CeipalEnqueueResponse(BaseModel):
    status: str
    batch_id: str


def _pydantic_to_dict(obj: BaseModel) -> Dict[str, Any]:
    # Support both pydantic v1 and v2
    try:
        return obj.model_dump()  # type: ignore[attr-defined]
    except Exception:
        try:
            return obj.dict()  # type: ignore[no-any-return]
        except Exception:
            return {}


class JobConfigPayload(BaseModel):
    title: str = ""
    description: str = ""
    important_questions: List[str] = []


def _require_candidate_pipeline_enabled() -> None:
    enabled = (os.getenv("CANDIDATE_PIPELINE_ENABLED") or "").strip().lower() in {"1", "true", "yes", "y"}
    if not enabled:
        raise HTTPException(
            status_code=410,
            detail="Job fetching and candidate ranking are disabled; use spreadsheet SMS outreach.",
        )


# Database initialization will be handled in on_startup

@app.on_event("startup")
async def on_startup():
    global _candidates
    global _ceipal_jobs
    global _vectorizer, _candidate_matrix, _candidate_sem

    # Initialize database
    _init_db()
    logger.info("Database initialized")

    # This deployment is operating in spreadsheet outreach-only mode: do not
    # load candidate datasets, build ranking indexes, or fetch jobs from CEIPAL.
    _candidates = []
    _ceipal_jobs = []
    _vectorizer = None
    _candidate_matrix = None
    _candidate_sem = None
    logger.info("Candidate loading, ranking, and job fetching are disabled")
    
    # Initialize the messaging service
    db = SessionLocal()
    try:
        messaging_service = get_messaging_service(db=db)
        logger.info("Messaging service initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize messaging service: {e}")
        raise
    finally:
        db.close()
    
    # Mount static after ensuring dir exists
    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    # Do not start automatic follow-ups or scheduled ATS processing. Spreadsheet
    # outreach is sent only when the user explicitly presses Send messages.


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global _followup_task
    global _ceipal_schedule_task
    if _followup_task and not _followup_task.done():
        _followup_task.cancel()
    if _ceipal_schedule_task and not _ceipal_schedule_task.done():
        _ceipal_schedule_task.cancel()


_followup_task: Optional[asyncio.Task] = None
_ceipal_schedule_task: Optional[asyncio.Task] = None


def _any_active_interview_calls() -> bool:
    """Best-effort check for any in-progress calls.

    If a scheduled CEIPAL batch runs while Twilio is actively calling, the process can
    become CPU/network bound (TTS generation, candidate sync, vector rebuilds) and Twilio
    may time out fetching the next TwiML/audio, causing an "application error" mid-call.
    """
    try:
        global interview_service
        svc = interview_service
        if svc is None or not hasattr(svc, "active_interviews"):
            return False
        active = getattr(svc, "active_interviews", None) or {}
        if not isinstance(active, dict):
            return False
        for v in active.values():
            if isinstance(v, dict) and (v.get("status") == "in_progress"):
                return True
        return False
    except Exception:
        return False


def _start_followup_loop() -> None:
    global _followup_task
    if _followup_task and not _followup_task.done():
        return
    _followup_task = asyncio.create_task(_followup_loop())


def _start_ceipal_schedule_loop() -> None:
    global _ceipal_schedule_task
    enabled = (os.getenv("CEIPAL_SCHEDULE_ENABLED") or "").strip().lower() in {"1", "true", "yes", "y"}
    if not enabled:
        return
    if _ceipal_schedule_task and not _ceipal_schedule_task.done():
        return
    _ceipal_schedule_task = asyncio.create_task(_ceipal_schedule_loop())
    logger.info("CEIPAL scheduled worker started")


def _ceipal_any_batch_running(db: Session) -> bool:
    try:
        # Crash recovery: if a batch was left in queued/running for too long,
        # mark it failed so new scheduled runs can proceed.
        try:
            stale_minutes = int(os.getenv("CEIPAL_BATCH_STALE_MINUTES", "240") or "240")
        except Exception:
            stale_minutes = 240
        stale_minutes = max(15, min(stale_minutes, 10080))
        stale_cutoff = datetime.utcnow() - timedelta(minutes=stale_minutes)
        stale = (
            db.query(CeipalBatchRun)
            .filter(
                CeipalBatchRun.status.in_(["queued", "running"]),
                CeipalBatchRun.created_at < stale_cutoff,
            )
            .order_by(CeipalBatchRun.created_at.asc())
            .all()
        )
        if stale:
            for b in stale:
                try:
                    b.status = "failed"
                    b.error = (b.error or "") + f"Stale batch auto-failed after {stale_minutes} minutes; "
                    b.finished_at = datetime.utcnow()
                except Exception:
                    pass
            try:
                db.commit()
            except Exception:
                db.rollback()

        active = (
            db.query(CeipalBatchRun)
            .filter(CeipalBatchRun.status.in_(["queued", "running"]))
            .order_by(CeipalBatchRun.created_at.desc())
            .first()
        )
        return active is not None
    except Exception:
        return False


def _enqueue_ceipal_batch_from_dict(req_dict: Dict[str, Any]) -> str:
    # Idempotency: reuse an existing queued/running/recent batch with the same request_hash.
    # This prevents duplicate scheduled runs (or manual retries) from blasting outreach.
    try:
        canonical = json.dumps(req_dict or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        canonical = json.dumps(req_dict or {}, ensure_ascii=False)
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:64]

    batch_id = uuid.uuid4().hex
    db = SessionLocal()
    try:
        try:
            reuse_hours = float(os.getenv("CEIPAL_BATCH_IDEMPOTENCY_WINDOW_HOURS", "6") or "6")
        except Exception:
            reuse_hours = 6.0
        reuse_hours = max(0.0, min(reuse_hours, 168.0))
        reuse_cutoff = datetime.utcnow() - timedelta(hours=reuse_hours)

        existing = (
            db.query(CeipalBatchRun)
            .filter(
                CeipalBatchRun.request_hash == request_hash,
                CeipalBatchRun.created_at >= reuse_cutoff,
                CeipalBatchRun.status.in_(["queued", "running"]),
            )
            .order_by(CeipalBatchRun.created_at.desc())
            .first()
        )
        if existing is not None and existing.id:
            return str(existing.id)

        db.add(
            CeipalBatchRun(
                id=batch_id,
                status="queued",
                request_json=json.dumps(req_dict or {}, ensure_ascii=False),
                request_hash=request_hash,
            )
        )
        db.commit()
    finally:
        db.close()
    return batch_id


async def _ceipal_schedule_loop() -> None:
    interval_seconds = int(os.getenv("CEIPAL_SCHEDULE_INTERVAL_SECONDS", "3600") or "3600")
    interval_seconds = max(60, min(interval_seconds, 86400))

    score_threshold = float(os.getenv("CEIPAL_SCHEDULE_SCORE_THRESHOLD", "0.4") or "0.4")
    score_threshold = max(0.0, min(score_threshold, 1.0))

    dry_run = (os.getenv("CEIPAL_SCHEDULE_DRY_RUN") or "").strip().lower() in {"1", "true", "yes", "y"}
    force_resend = (os.getenv("CEIPAL_SCHEDULE_FORCE_RESEND") or "").strip().lower() in {"1", "true", "yes", "y"}

    max_jobs = os.getenv("CEIPAL_SCHEDULE_MAX_JOBS")
    max_jobs_int: Optional[int] = None
    if max_jobs is not None and str(max_jobs).strip() != "":
        try:
            max_jobs_int = int(str(max_jobs).strip())
        except Exception:
            max_jobs_int = None

    job_status = (os.getenv("CEIPAL_SCHEDULE_JOB_STATUS") or "").strip() or "Open,Active"

    run_on_startup = (os.getenv("CEIPAL_SCHEDULE_RUN_ON_STARTUP") or "").strip().lower() in {"1", "true", "yes", "y"}
    if not run_on_startup:
        await asyncio.sleep(interval_seconds)

    while True:
        enabled = (os.getenv("CEIPAL_SCHEDULE_ENABLED") or "").strip().lower() in {"1", "true", "yes", "y"}
        if not enabled:
            await asyncio.sleep(30)
            continue

        # If we are actively on a Twilio call, delay CEIPAL batches so we don't starve
        # the webhook/TTS endpoints and trigger Twilio "application error".
        if _any_active_interview_calls():
            await asyncio.sleep(60)
            continue

        db = SessionLocal()
        try:
            if _ceipal_any_batch_running(db):
                await asyncio.sleep(30)
                continue
        finally:
            db.close()

        req = {
            "score_threshold": score_threshold,
            "dry_run": bool(dry_run),
            "force_resend": bool(force_resend),
        }
        if max_jobs_int is not None and max_jobs_int > 0:
            req["max_jobs"] = max_jobs_int
        if job_status:
            req["job_status"] = job_status

        batch_id = _enqueue_ceipal_batch_from_dict(req)
        logger.info(f"CEIPAL scheduled run enqueued: batch_id={batch_id}")
        await asyncio.to_thread(_run_ceipal_batch, batch_id)
        await asyncio.sleep(interval_seconds)


async def _followup_loop() -> None:
    """Background worker: sends follow-ups after 10-12 hours, twice, then calls."""
    check_every_seconds = int(os.getenv("FOLLOWUP_CHECK_SECONDS", "300"))
    followup_hours = int(os.getenv("FOLLOWUP_HOURS", "12"))

    while True:
        try:
            now = datetime.utcnow()
            db = SessionLocal()
            try:
                due = (
                    db.query(OutreachTracker)
                    .filter(
                        OutreachTracker.status == "active",
                        OutreachTracker.replied_at.is_(None),
                        OutreachTracker.next_followup_at.isnot(None),
                        OutreachTracker.next_followup_at <= now,
                    )
                    .order_by(OutreachTracker.next_followup_at.asc())
                    .limit(200)
                    .all()
                )

                if due:
                    logger.info(f"Follow-up worker: found {len(due)} due follow-ups")

                for t in due:
                    try:
                        # If this outreach was tied to a CEIPAL job and that job is no longer
                        # in an "active" status, stop follow-ups.
                        try:
                            job_run_id = getattr(t, "job_run_id", None)
                        except Exception:
                            job_run_id = None
                        if job_run_id:
                            try:
                                job = db.query(CeipalJobRun).filter(CeipalJobRun.id == int(job_run_id)).first()
                            except Exception:
                                job = None
                            if job is not None:
                                allowed_raw = (os.getenv("CEIPAL_SCHEDULE_JOB_STATUS") or "Open,Active").strip() or "Open,Active"
                                allowed = [p.strip().lower() for p in re.split(r"[\s,|]+", allowed_raw) if p.strip()]
                                js = str(getattr(job, "job_status", "") or "").strip().lower()
                                if allowed and not any(a in js for a in allowed):
                                    t.status = "completed"
                                    t.next_followup_at = None
                                    db.commit()
                                    continue

                        # Re-check flags (race safety)
                        if t.replied_at is not None or t.status != "active":
                            continue

                        if t.followup_count < 3:
                            # Send follow-up message
                            if t.channel == "sms":
                                try:
                                    from app.messaging_service import messaging_service

                                    if int(t.followup_count or 0) == 0:
                                        msg = template_manager.get_sms_template("followup1") or os.getenv(
                                            "FOLLOWUP1_SMS_TEXT",
                                            "Hi {name}, following up on {Role}.",
                                        )
                                    elif int(t.followup_count or 0) == 1:
                                        msg = template_manager.get_sms_template("followup2") or os.getenv(
                                            "FOLLOWUP2_SMS_TEXT",
                                            "Hi {name}, quick check-in on {Role} opportunities.",
                                        )
                                    else:
                                        msg = template_manager.get_sms_template("followup3") or os.getenv(
                                            "FOLLOWUP3_SMS_TEXT",
                                            "Hi {name}, last check-in regarding {Role}.",
                                        )
                                    # Placeholder rendering for follow-ups
                                    try:
                                        name = ""
                                        role_val = (getattr(t, "job_title", None) or os.getenv("DEFAULT_ROLE", "the role"))
                                        location_val = ""
                                        if t.candidate_id:
                                            try:
                                                id_to_candidate = {str(c.get("_id")): c for c in _candidates}
                                                cand = id_to_candidate.get(str(t.candidate_id))
                                                if cand:
                                                    first = cand.get('FirstName','')
                                                    last = cand.get('LastName','')
                                                    name = f"{first} {last}".strip()
                                            except Exception:
                                                pass
                                        rendered_msg = _render_outreach_placeholders(
                                            msg,
                                            candidate={"JobTitle": role_val, "Location": location_val},
                                            role=role_val,
                                            location=location_val,
                                        )
                                    except Exception:
                                        rendered_msg = msg

                                    messaging_service.send_sms(t.contact, rendered_msg)
                                    logger.info(f"Sent SMS follow-up #{t.followup_count + 1} to {t.contact}")
                                except Exception as sms_ex:
                                    logger.error(f"Failed sending SMS follow-up to {t.contact}: {sms_ex}")
                                    continue
                            elif t.channel == "email":
                                try:
                                    from sendgrid import SendGridAPIClient
                                    from sendgrid.helpers.mail import Mail, HtmlContent, Email

                                    sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
                                    from_email = os.getenv("SENDGRID_FROM_EMAIL") or "noreply@example.com"
                                    reply_to_email = os.getenv("SENDGRID_INBOUND_EMAIL")

                                    if not sendgrid_api_key:
                                        logger.error("SENDGRID_API_KEY not set; cannot send email follow-up")
                                        continue

                                    sg = SendGridAPIClient(sendgrid_api_key)
                                    fu_num = int(t.followup_count or 0)
                                    if fu_num == 0:
                                        tpl = template_manager.get_email_template("followup1")
                                        subject = tpl.get("subject") or os.getenv(
                                            "FOLLOWUP1_EMAIL_SUBJECT",
                                            "A couple of roles that match you, {name}",
                                        )
                                        body = tpl.get("body") or os.getenv(
                                            "FOLLOWUP1_EMAIL_HTML",
                                            "<p>Hi {name},</p>",
                                        )
                                    elif fu_num == 1:
                                        tpl = template_manager.get_email_template("followup2")
                                        subject = tpl.get("subject") or os.getenv(
                                            "FOLLOWUP2_EMAIL_SUBJECT",
                                            "{name}, here's what similar pros are earning",
                                        )
                                        body = tpl.get("body") or os.getenv(
                                            "FOLLOWUP2_EMAIL_HTML",
                                            "<p>Hi {name},</p>",
                                        )
                                    else:
                                        tpl = template_manager.get_email_template("followup3")
                                        subject = tpl.get("subject") or os.getenv(
                                            "FOLLOWUP3_EMAIL_SUBJECT",
                                            "2026 pay snapshot for you",
                                        )
                                        body = tpl.get("body") or os.getenv(
                                            "FOLLOWUP3_EMAIL_HTML",
                                            "<p>Hi {name},</p>",
                                        )
                                    # Placeholder rendering for follow-up emails
                                    try:
                                        name = ""
                                        role_val = os.getenv("DEFAULT_ROLE", "the role")
                                        location_val = ""
                                        if t.candidate_id:
                                            try:
                                                id_to_candidate = {str(c.get("_id")): c for c in _candidates}
                                                cand = id_to_candidate.get(str(t.candidate_id))
                                                if cand:
                                                    first = cand.get('FirstName','')
                                                    last = cand.get('LastName','')
                                                    name = f"{first} {last}".strip()
                                                    job_title = str(cand.get('JobTitle') or '')
                                                    if job_title:
                                                        role_val = job_title
                                                    location_val = str(cand.get('Location') or '')
                                            except Exception:
                                                pass
                                        subject_rendered = _render_outreach_placeholders(
                                            subject,
                                            candidate={"JobTitle": role_val, "Location": location_val},
                                            role=role_val,
                                            location=location_val,
                                        )
                                        body = _render_outreach_placeholders(
                                            body,
                                            candidate={"JobTitle": role_val, "Location": location_val},
                                            role=role_val,
                                            location=location_val,
                                        )
                                    except Exception:
                                        subject_rendered = subject
                                    
                                    message = Mail(
                                        from_email=from_email,
                                        to_emails=t.contact,
                                        subject=subject_rendered,
                                        html_content=HtmlContent(body),
                                    )
                                    if reply_to_email:
                                        try:
                                            message.reply_to = Email(reply_to_email)
                                        except Exception:
                                            pass
                                    sg.send(message)
                                    logger.info(f"Sent email follow-up #{t.followup_count + 1} to {t.contact}")
                                except Exception as email_ex:
                                    logger.error(f"Failed sending email follow-up to {t.contact}: {email_ex}")
                                    continue
                            else:
                                continue

                            t.followup_count = int(t.followup_count or 0) + 1
                            t.last_outreach_at = now
                            t.next_followup_at = now + timedelta(hours=followup_hours)
                            db.commit()
                            continue

                        # After 3 follow-ups, call for SMS contacts
                        if t.channel == "sms":
                            try:
                                from app.messaging_service import messaging_service

                                ok = await messaging_service.initiate_interview_call(t.contact)
                                if ok:
                                    t.status = "called"
                                    t.last_outreach_at = now
                                    t.next_followup_at = None
                                    db.commit()
                                    logger.info(f"Triggered call after follow-ups to {t.contact}")
                            except Exception as call_ex:
                                logger.error(f"Failed to trigger call to {t.contact}: {call_ex}")
                    except Exception as row_ex:
                        logger.error(f"Follow-up worker row error: {row_ex}", exc_info=True)
            finally:
                db.close()
        except Exception as loop_ex:
            logger.error(f"Follow-up worker loop error: {loop_ex}", exc_info=True)

        await asyncio.sleep(check_every_seconds)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "candidates": len(_candidates),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/health/ceipal")
def health_ceipal():
    """Lightweight health check for CEIPAL flow.

    - DB: verifies we can execute a simple query
    - CEIPAL: verifies credentials are configured and auth token can be fetched

    Does NOT trigger batch processing.
    """
    status_out: Dict[str, Any] = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "db": {"ok": False},
        "ceipal": {"ok": False},
    }

    # DB check
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            status_out["db"] = {"ok": True}
        finally:
            db.close()
    except Exception as db_ex:
        status_out["status"] = "degraded"
        status_out["db"] = {"ok": False, "error": str(db_ex)}

    # CEIPAL auth check
    try:
        from app.ceipal_client import build_ceipal_client_from_env

        client = build_ceipal_client_from_env()
        token = client._get_token()
        status_out["ceipal"] = {"ok": bool(token), "token": "present" if token else "missing"}
        if not token:
            status_out["status"] = "degraded"
    except Exception as c_ex:
        status_out["status"] = "degraded"
        status_out["ceipal"] = {"ok": False, "error": str(c_ex)}

    return status_out


@app.get("/api/ceipal/reports/jd")
def ceipal_jd_report():
    client = build_ceipal_client_from_env()
    report_url = (os.getenv("CEIPAL_JD_REPORT_URL") or "").strip()
    if not report_url:
        raise HTTPException(status_code=400, detail="CEIPAL_JD_REPORT_URL is not set")
    try:
        data = client.get_report_data(report_url)
        return {"report": "jd", "data": data}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


def _render_outreach_placeholders(
    text: str,
    *,
    candidate: Dict[str, Any],
    role: str,
    location: Optional[str] = None,
) -> str:
    if not isinstance(text, str):
        return text
    contact = _resolve_candidate_contact_fields(candidate)
    name_val = contact.get("name") or ""
    try:
        job_title_val = str(candidate.get("JobTitle") or "").strip()
    except Exception:
        job_title_val = ""
    try:
        role_in = str(role or "").strip()
    except Exception:
        role_in = ""

    # If the candidate record doesn't carry a job title (common with CEIPAL reports),
    # fallback to the role we are outreaching for.
    if not job_title_val and role_in:
        job_title_val = role_in

    default_role_env = (os.getenv("DEFAULT_ROLE") or "").strip()
    if not default_role_env:
        default_role_env = "the role"

    role_val = role_in or job_title_val or default_role_env
    if not str(role_val or "").strip():
        role_val = "the role"
    # Prefer job context (passed in via `location`) over any candidate profile fields.
    # For CEIPAL outreach, location should come from the job/JD, not the candidate.
    location_val = (location or "")
    try:
        location_val = str(location_val)
    except Exception:
        location_val = ""
    location_val = location_val.strip()
    if location_val.startswith("[") and location_val.endswith("]"):
        location_val = location_val[1:-1].strip()
    out = text
    out = out.replace("{name}", name_val)
    out = out.replace("{Role}", role_val)
    out = out.replace("{{name}}", name_val)
    out = out.replace("{{job_title}}", role_val)
    out = out.replace("{{location}}", location_val)
    out = out.replace("{location}", location_val)
    # New placeholders: map to existing fields or generic fallbacks
    hot_markets_val = location_val or "high-demand markets"
    specialty_val = job_title_val or "your specialty"
    out = out.replace("{{hot_markets}}", hot_markets_val)
    out = out.replace("{{specialty}}", specialty_val)
    # Backward-compatible: replace bracketed placeholders that mention "location(s)"
    # e.g. "[their specialty or location, e.g., ICU or South Carolina]".
    if location_val:
        out = re.sub(r"\[[^\]]*\blocati(?:on|ons)\b[^\]]*\]", location_val, out, flags=re.I)
    return out


def _send_outreach_sms(
    db: Session,
    *,
    candidate: Dict[str, Any],
    role: str,
    location: Optional[str] = None,
    template_key: str = "first_outreach",
    job_run_id: Optional[int] = None,
    job_code: Optional[str] = None,
    job_title: Optional[str] = None,
) -> Dict[str, Any]:
    contact = _resolve_candidate_contact_fields(candidate)
    to_number = contact.get("phone")
    if not to_number:
        return {"ok": False, "error": "Missing phone"}

    messaging_service = get_messaging_service(db=db)
    try:
        template_manager.reload()
    except Exception:
        pass

    msg = template_manager.get_sms_template(template_key)
    if not msg:
        msg = os.getenv(
            "FIRST_OUTREACH_SMS_TEXT",
            "Hi {name}, I'd love to connect about {Role}. When are you available? Please include your timezone (e.g., EST).",
        )
    rendered = _render_outreach_placeholders(msg, candidate=candidate, role=role, location=location)
    ok = messaging_service.send_sms(to_number, rendered)
    # Create/update follow-up tracker so CEIPAL outreach can be followed up automatically.
    try:
        now = datetime.utcnow()
        tracker = (
            db.query(OutreachTracker)
            .filter(OutreachTracker.channel == "sms", OutreachTracker.contact == to_number)
            .order_by(OutreachTracker.id.desc())
            .first()
        )
        if not tracker:
            tracker = OutreachTracker(
                candidate_id=str(candidate.get("_id")) if candidate.get("_id") is not None else None,
                contact=to_number,
                channel="sms",
                first_contacted_at=now,
                last_outreach_at=now,
                followup_count=0,
                next_followup_at=now + timedelta(hours=int(os.getenv("FOLLOWUP_HOURS", "12"))),
                status="active",
            )
            db.add(tracker)
        else:
            if tracker.replied_at is None and tracker.status == "active":
                tracker.next_followup_at = now + timedelta(hours=int(os.getenv("FOLLOWUP_HOURS", "12")))
            tracker.last_outreach_at = now

        # Attach CEIPAL job context (job-aware followups)
        if job_run_id and not getattr(tracker, "job_run_id", None):
            tracker.job_run_id = int(job_run_id)
        if job_code and not getattr(tracker, "job_code", None):
            tracker.job_code = str(job_code)
        jt = (job_title or role or "").strip()
        if jt and not getattr(tracker, "job_title", None):
            tracker.job_title = jt

        db.commit()
    except Exception:
        pass
    return {"ok": bool(ok), "error": None if ok else "Failed to send SMS", "to": to_number}


def _send_outreach_email(
    db: Session,
    *,
    candidate: Dict[str, Any],
    role: str,
    location: Optional[str] = None,
    template_key: str = "first_outreach",
    job_run_id: Optional[int] = None,
    job_code: Optional[str] = None,
    job_title: Optional[str] = None,
) -> Dict[str, Any]:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, HtmlContent, Email

    contact = _resolve_candidate_contact_fields(candidate)
    to_email = contact.get("email")
    if not to_email:
        return {"ok": False, "error": "Missing email"}

    sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("SENDGRID_FROM_EMAIL") or "noreply@example.com"
    reply_to_email = os.getenv("SENDGRID_INBOUND_EMAIL")
    if not sendgrid_api_key:
        return {"ok": False, "error": "SENDGRID_API_KEY not configured"}

    try:
        template_manager.reload()
    except Exception:
        pass

    tpl = template_manager.get_email_template(template_key) or {}
    subject_val = (tpl.get("subject") or "").strip() or os.getenv(
        "FIRST_OUTREACH_EMAIL_SUBJECT",
        "[Name], quick question about your next move",
    )
    body_val = (tpl.get("body") or "").strip() or os.getenv(
        "FIRST_OUTREACH_EMAIL_HTML",
        "<p>Hi {name},</p><p>Would you be open to a quick call this week? Reply with your availability and timezone.</p>",
    )

    subject_rendered = _render_outreach_placeholders(subject_val, candidate=candidate, role=role, location=location)
    body_rendered = _render_outreach_placeholders(body_val, candidate=candidate, role=role, location=location)

    sg = SendGridAPIClient(sendgrid_api_key)
    message = Mail(
        from_email=from_email,
        to_emails=to_email,
        subject=subject_rendered,
        html_content=HtmlContent(body_rendered),
    )
    if reply_to_email:
        try:
            message.reply_to = Email(reply_to_email)
        except Exception:
            pass

    try:
        resp = sg.send(message)
        db.add(
            EmailLog(
                candidate_id=str(candidate.get("_id")) if candidate.get("_id") is not None else None,
                subject=subject_rendered,
                body=body_rendered,
                status="sent",
                provider_message_id=(resp.headers.get("X-Message-Id") if hasattr(resp, "headers") else None),
            )
        )
        db.commit()
        # Create/update follow-up tracker for email as well.
        try:
            now = datetime.utcnow()
            tracker = (
                db.query(OutreachTracker)
                .filter(OutreachTracker.channel == "email", OutreachTracker.contact == to_email.lower())
                .order_by(OutreachTracker.id.desc())
                .first()
            )
            if not tracker:
                tracker = OutreachTracker(
                    candidate_id=str(candidate.get("_id")) if candidate.get("_id") is not None else None,
                    contact=to_email.lower(),
                    channel="email",
                    first_contacted_at=now,
                    last_outreach_at=now,
                    followup_count=0,
                    next_followup_at=now + timedelta(hours=int(os.getenv("FOLLOWUP_HOURS", "12"))),
                    status="active",
                )
                db.add(tracker)
            else:
                if tracker.replied_at is None and tracker.status == "active":
                    tracker.next_followup_at = now + timedelta(hours=int(os.getenv("FOLLOWUP_HOURS", "12")))
                tracker.last_outreach_at = now

            if job_run_id and not getattr(tracker, "job_run_id", None):
                tracker.job_run_id = int(job_run_id)
            if job_code and not getattr(tracker, "job_code", None):
                tracker.job_code = str(job_code)
            jt = (job_title or role or "").strip()
            if jt and not getattr(tracker, "job_title", None):
                tracker.job_title = jt

            db.commit()
        except Exception:
            pass
        return {"ok": True, "error": None, "to": to_email}
    except Exception as e:
        return {"ok": False, "error": str(e), "to": to_email}


@app.post("/api/ceipal/process", response_model=CeipalEnqueueResponse)
def ceipal_process(req: CeipalProcessRequest, background_tasks: BackgroundTasks):
    _require_candidate_pipeline_enabled()
    # Enqueue: run in background task so request returns quickly
    req_dict = _pydantic_to_dict(req)
    batch_id = _enqueue_ceipal_batch_from_dict(req_dict)
    background_tasks.add_task(_run_ceipal_batch, batch_id)
    return CeipalEnqueueResponse(status="queued", batch_id=batch_id)


@app.post("/api/ceipal/process/async", response_model=CeipalEnqueueResponse)
def ceipal_process_async(req: CeipalProcessRequest, background_tasks: BackgroundTasks):
    _require_candidate_pipeline_enabled()
    req_dict = _pydantic_to_dict(req)
    batch_id = _enqueue_ceipal_batch_from_dict(req_dict)
    background_tasks.add_task(_run_ceipal_batch, batch_id)
    return CeipalEnqueueResponse(status="queued", batch_id=batch_id)


@app.get("/api/ceipal/batches")
def ceipal_batches(limit: int = 50):
    db = SessionLocal()
    try:
        rows = (
            db.query(CeipalBatchRun)
            .order_by(CeipalBatchRun.created_at.desc())
            .limit(max(1, min(int(limit), 200)))
            .all()
        )
        out = []
        for r in rows:
            out.append(
                {
                    "id": r.id,
                    "status": r.status,
                    "jobs_processed": r.jobs_processed,
                    "candidates_selected": r.candidates_selected,
                    "messages_attempted": r.messages_attempted,
                    "messages_sent": r.messages_sent,
                    "error": r.error,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                }
            )
        return {"batches": out}
    finally:
        db.close()


@app.get("/api/ceipal/batches/{batch_id}")
def ceipal_batch_detail(batch_id: str):
    db = SessionLocal()
    try:
        r = db.query(CeipalBatchRun).filter(CeipalBatchRun.id == str(batch_id)).first()
        if not r:
            raise HTTPException(status_code=404, detail="Batch not found")
        return {
            "id": r.id,
            "status": r.status,
            "request_json": r.request_json,
            "jobs_processed": r.jobs_processed,
            "candidates_selected": r.candidates_selected,
            "messages_attempted": r.messages_attempted,
            "messages_sent": r.messages_sent,
            "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
    finally:
        db.close()


def _run_ceipal_batch(batch_id: str) -> None:
    """Background runner for CEIPAL processing."""
    try:
        _require_candidate_pipeline_enabled()
    except HTTPException:
        logger.warning(f"CEIPAL batch {batch_id} skipped because the candidate pipeline is disabled")
        return
    global _candidates
    global _ceipal_jobs

    db = SessionLocal()
    _ceipal_rank_lock_acquired = False
    try:
        batch = db.query(CeipalBatchRun).filter(CeipalBatchRun.id == str(batch_id)).first()
        if not batch:
            return

        # If a manual HTTP /rank is running, do not start CEIPAL batch.
        # Wait a bounded amount of time, then mark this batch as blocked.
        wait_s = float(os.getenv("CEIPAL_WAIT_FOR_MANUAL_RANK_SECONDS", "20") or "20")
        wait_s = max(0.0, min(300.0, wait_s))
        got_rank_lock = _manual_rank_lock.acquire(timeout=wait_s)
        if not got_rank_lock:
            try:
                batch.status = "blocked"
                batch.started_at = datetime.utcnow()
                batch.finished_at = batch.started_at
                batch.error = "manual_rank_in_progress"
                db.commit()
            except Exception:
                pass
            logger.warning(
                f"CEIPAL batch {batch_id}: blocked (manual /rank in progress; waited {wait_s}s)."
            )
            return
        _ceipal_rank_lock_acquired = True

        try:
            req_data = json.loads(batch.request_json or "{}")
        except Exception:
            req_data = {}

        score_threshold = float(req_data.get("score_threshold") or 0.4)
        score_threshold = max(0.0, min(1.0, score_threshold))

        # Blast protection controls
        outreach_enabled = (os.getenv("OUTREACH_ENABLED") or "").strip().lower() in {"1", "true", "yes", "y"}
        throttle_seconds = float(os.getenv("OUTREACH_THROTTLE_SECONDS", "0.25") or "0.25")
        max_jobs_per_run = int(os.getenv("OUTREACH_MAX_JOBS_PER_RUN", "10") or "10")
        max_selected_per_job = int(os.getenv("OUTREACH_MAX_SELECTED_PER_JOB", "50") or "50")
        max_messages_per_run = int(os.getenv("OUTREACH_MAX_MESSAGES_PER_RUN", "500") or "500")

        dry_run = bool(req_data.get("dry_run", False))
        force_resend = bool(req_data.get("force_resend", False))
        resend_after_hours = int(req_data.get("resend_after_hours", 168) or 168)
        job_status_filter = req_data.get("job_status")
        max_jobs_req = req_data.get("max_jobs")

        batch.status = "running"
        batch.started_at = datetime.utcnow()
        db.commit()

        logger.info(f"CEIPAL batch {batch_id}: starting (threshold={score_threshold}, dry_run={dry_run}, outreach_enabled={outreach_enabled}, force_resend={force_resend})")

        # Swap the shared candidate dataset/index under a lock to avoid races with
        # concurrent /rank or interview flows.
        with _candidates_lock:
            _ceipal_jobs = _load_jobs_from_ceipal()
            _candidates = _load_candidates_from_ceipal()
            _build_vector_index(_candidates)

        jobs = [j for j in (_ceipal_jobs or []) if isinstance(j, dict)]
        try:
            distinct_statuses = sorted({str(j.get("JobStatus") or "").strip() for j in jobs if isinstance(j, dict)})
            if distinct_statuses:
                logger.info(f"CEIPAL batch {batch_id}: job statuses in report: {distinct_statuses[:20]}")
        except Exception:
            pass
        # Default: only process Open jobs unless explicitly overridden.
        if job_status_filter is None or str(job_status_filter).strip() == "":
            job_status_filter = "Open,Active"
        if job_status_filter:
            raw = str(job_status_filter).strip()
            raw_lc = raw.lower()
            if raw_lc not in {"all", "*", "any"}:
                # Support comma/pipe separated values, e.g. "Open,Active".
                parts = [p.strip().lower() for p in re.split(r"[\s,|]+", raw) if p.strip()]
                want_set = set(parts)
                if want_set:
                    jobs = [
                        j for j in jobs
                        if any(
                            w in str(j.get("JobStatus") or "").strip().lower()
                            for w in want_set
                        )
                    ]

        if not jobs:
            logger.info(f"CEIPAL batch {batch_id}: no jobs to process after status filter={job_status_filter!r}")

        # Apply caps (request cap is bounded by server cap). We apply the cap while
        # processing, not by slicing the initial list, so already-completed jobs don't
        # consume the limit and result in jobs_processed=0.
        if isinstance(max_jobs_req, int) and max_jobs_req > 0:
            target_jobs_to_process = max(1, min(max_jobs_req, max_jobs_per_run))
        else:
            target_jobs_to_process = max_jobs_per_run

        def _should_skip_candidate_for_job(job_code: str, candidate_id: str) -> bool:
            if force_resend:
                return False
            cutoff = datetime.utcnow() - timedelta(hours=max(1, resend_after_hours))
            existing = (
                db.query(CeipalCandidateRun)
                .join(CeipalJobRun, CeipalCandidateRun.job_run_id == CeipalJobRun.id)
                .filter(
                    CeipalJobRun.job_code == job_code,
                    CeipalCandidateRun.candidate_id == candidate_id,
                    CeipalCandidateRun.created_at >= cutoff,
                )
                .order_by(CeipalCandidateRun.id.desc())
                .first()
            )
            return existing is not None

        def _should_skip_candidate_globally(candidate_id: str, phone: str, email: str) -> bool:
            """Prevent contacting the same candidate for multiple jobs in parallel."""
            if force_resend:
                return False
            try:
                # Hard exclude: recruiter marked this candidate as final selected.
                try:
                    cid = str(candidate_id or "").strip()
                    phone_n = _norm_e164(phone or "")
                    email_n = (str(email or "").strip().lower() if email else "")
                    fs_q = db.query(FinalSelectedCandidate)
                    if cid:
                        fs_q = fs_q.filter(FinalSelectedCandidate.candidate_id == cid)
                    elif phone_n:
                        fs_q = fs_q.filter(FinalSelectedCandidate.phone == phone_n)
                    elif email_n:
                        fs_q = fs_q.filter(FinalSelectedCandidate.email == email_n)
                    else:
                        fs_q = None
                    if fs_q is not None:
                        fs_hit = fs_q.order_by(FinalSelectedCandidate.id.desc()).first()
                        if fs_hit is not None:
                            return True
                except Exception:
                    pass

                # If this candidate has *any* prior outreach thread that is no longer
                # active, treat them as "finished" for a role and never outreach
                # them for another job (prevents multi-role blasts over time).
                finished_q = db.query(OutreachTracker)
                if candidate_id:
                    finished_q = finished_q.filter(OutreachTracker.candidate_id == str(candidate_id))
                elif phone:
                    finished_q = finished_q.filter(OutreachTracker.contact == _norm_e164(phone))
                elif email:
                    finished_q = finished_q.filter(OutreachTracker.contact == str(email).lower())

                finished = (
                    finished_q.filter(
                        (OutreachTracker.replied_at.isnot(None))
                        | (OutreachTracker.status.in_(["replied", "called", "completed"]))
                    )
                    .order_by(OutreachTracker.id.desc())
                    .first()
                )
                if finished is not None:
                    return True

                # Otherwise, also block if there is an active thread still in progress.
                active_q = finished_q.filter(
                    OutreachTracker.replied_at.is_(None),
                    OutreachTracker.status == "active",
                )
                active = active_q.order_by(OutreachTracker.id.desc()).first()
                return active is not None
            except Exception:
                return False

        id_to_candidate = {
            str(c.get("_id")): c
            for c in (_candidates or [])
            if isinstance(c, dict) and c.get("_id") is not None
        }

        messages_attempted = 0
        messages_sent = 0
        jobs_processed = 0
        candidates_selected = 0

        skip_counts: Dict[str, int] = {
            "missing_job_code": 0,
            "already_completed": 0,
            "invalid_jd": 0,
            "job_run_error": 0,
        }
        skip_examples: List[str] = []

        def _result_id(res: Any) -> str:
            if isinstance(res, dict):
                return str(res.get("id") or res.get("_id") or res.get("candidate_id") or "").strip()
            return str(getattr(res, "id", "") or "").strip()

        def _result_score(res: Any) -> float:
            if isinstance(res, dict):
                val = res.get("score")
            else:
                val = getattr(res, "score", 0)
            try:
                return float(val or 0)
            except Exception:
                return 0.0

        for j in jobs:
            if target_jobs_to_process > 0 and jobs_processed >= target_jobs_to_process:
                break
            job_code = str(j.get("JobCode") or "").strip()
            job_title = str(j.get("JobTitle") or "").strip()
            job_status = str(j.get("JobStatus") or "").strip()
            jd = str(j.get("JobDescription") or "").strip()

            if not job_code:
                skip_counts["missing_job_code"] = int(skip_counts.get("missing_job_code") or 0) + 1
                if len(skip_examples) < 5:
                    skip_examples.append(f"missing_job_code title={job_title!r} status={job_status!r}")
                continue

            # Skip already-processed jobs (do not process the same job_code twice)
            if job_code and not force_resend:
                already_done = (
                    db.query(CeipalJobRun)
                    .filter(
                        CeipalJobRun.job_code == job_code,
                        CeipalJobRun.status == "completed",
                    )
                    .first()
                )
                if already_done is not None:
                    skip_counts["already_completed"] = int(skip_counts.get("already_completed") or 0) + 1
                    continue

            jd_lc = jd.strip().lower()
            jd_invalid = (not jd) or (jd_lc in {"reserved", "tbd", "na", "n/a"}) or (len(jd) < 40)
            if jd_invalid:
                # Do not skip: build fallback JD from non-empty fields so ranking/interview stay operational.
                skip_counts["invalid_jd"] = int(skip_counts.get("invalid_jd") or 0) + 1
                if len(skip_examples) < 5:
                    skip_examples.append(
                        f"invalid_jd_fallback job_code={job_code!r} title={job_title!r} status={job_status!r} jd_len={len(jd or '')}"
                    )
                jd = _build_fallback_jd_for_ceipal_job(j)

            job_run = CeipalJobRun(
                job_code=job_code,
                job_title=job_title,
                job_status=job_status,
                status="running",
                score_threshold=str(score_threshold),
                total_candidates=len(_candidates or []),
                selected_candidates=0,
            )
            db.add(job_run)
            db.commit()
            db.refresh(job_run)

            try:
                # Ensure extraction sees a non-empty description (fallback is ok).
                j_eff = dict(j)
                j_eff["JobDescription"] = jd
                structured_req = _get_structured_requirements_for_job(j_eff)
                
                # Extract job context for per-candidate storage (don't use globals)
                job_context_skills = list(structured_req.get("required_skills") or [])
                job_context_questions = list(structured_req.get("required_questions") or [])

                rank_req = RankRequest(
                    job_description=jd,
                    role_title=job_title or None,
                    top_k=None,
                    required_skills=(structured_req.get("required_skills") or None),
                    min_experience_years=structured_req.get("min_experience_years"),
                    certifications=None,
                )
                results = rank(rank_req, use_ceipal_candidates=True)
                selected = [
                    r for r in (results or [])
                    if _result_score(r) >= score_threshold
                ]
                if max_selected_per_job > 0 and len(selected) > max_selected_per_job:
                    selected = selected[:max_selected_per_job]

                job_run.selected_candidates = len(selected)
                db.commit()
                jobs_processed += 1
                candidates_selected += len(selected)

                for r in selected:
                    cid = _result_id(r)
                    if not cid:
                        continue
                    if _should_skip_candidate_for_job(job_code, cid):
                        continue
                    cand = id_to_candidate.get(cid)
                    if not cand:
                        continue

                    contact = _resolve_candidate_contact_fields(cand)
                    if _should_skip_candidate_globally(cid, contact.get("phone") or "", contact.get("email") or ""):
                        continue
                    c_run = CeipalCandidateRun(
                        job_run_id=job_run.id,
                        candidate_id=cid,
                        candidate_name=contact.get("name"),
                        candidate_email=contact.get("email"),
                        candidate_phone=contact.get("phone"),
                        score=str(_result_score(r) or 0),
                        sms_status="not_sent",
                        email_status="not_sent",
                        # Store job context for correct interview routing (Option A)
                        job_code=job_code,
                        job_title=job_title,
                        job_description=jd,
                        required_skills_json=json.dumps(job_context_skills, ensure_ascii=False) if job_context_skills else None,
                        required_questions_json=json.dumps(job_context_questions, ensure_ascii=False) if job_context_questions else None,
                    )
                    db.add(c_run)
                    db.commit()
                    db.refresh(c_run)

                    if dry_run:
                        c_run.sms_status = "dry_run"
                        c_run.email_status = "dry_run"
                        db.commit()
                        continue

                    if not outreach_enabled:
                        c_run.sms_status = "blocked"
                        c_run.email_status = "blocked"
                        c_run.error = (c_run.error or "") + "Outreach disabled by OUTREACH_ENABLED; "
                        db.commit()
                        continue

                    # Global cap
                    if max_messages_per_run > 0 and messages_attempted >= max_messages_per_run:
                        c_run.sms_status = "blocked"
                        c_run.email_status = "blocked"
                        c_run.error = (c_run.error or "") + "Blocked by OUTREACH_MAX_MESSAGES_PER_RUN; "
                        db.commit()
                        continue

                    # SMS
                    messages_attempted += 1
                    sms_res = _send_outreach_sms(
                        db,
                        candidate=cand,
                        role=job_title,
                        location=j.get("Location"),
                        job_run_id=job_run.id,
                        job_code=job_code,
                        job_title=job_title,
                    )
                    c_run.sms_status = "sent" if sms_res.get("ok") else "failed"
                    if sms_res.get("ok"):
                        messages_sent += 1
                    else:
                        c_run.error = (c_run.error or "") + f"SMS: {sms_res.get('error')}; "
                    db.commit()
                    time.sleep(max(0.0, throttle_seconds))

                    if max_messages_per_run > 0 and messages_attempted >= max_messages_per_run:
                        continue

                    # Email
                    messages_attempted += 1
                    em_res = _send_outreach_email(
                        db,
                        candidate=cand,
                        role=job_title,
                        location=j.get("Location"),
                        job_run_id=job_run.id,
                        job_code=job_code,
                        job_title=job_title,
                    )
                    c_run.email_status = "sent" if em_res.get("ok") else "failed"
                    if em_res.get("ok"):
                        messages_sent += 1
                    else:
                        c_run.error = (c_run.error or "") + f"Email: {em_res.get('error')}; "
                    db.commit()
                    time.sleep(max(0.0, throttle_seconds))

                job_run.status = "completed"
                job_run.finished_at = datetime.utcnow()
                db.commit()
            except Exception as job_ex:
                job_run.status = "failed"
                job_run.error = str(job_ex)
                job_run.finished_at = datetime.utcnow()
                db.commit()
                skip_counts["job_run_error"] = int(skip_counts.get("job_run_error") or 0) + 1
                logger.error(f"CEIPAL batch {batch_id}: job failed job_code={job_code}: {job_ex}", exc_info=True)

            if max_messages_per_run > 0 and messages_attempted >= max_messages_per_run:
                break

        batch.jobs_processed = jobs_processed
        batch.candidates_selected = candidates_selected
        batch.messages_attempted = messages_attempted
        batch.messages_sent = messages_sent
        batch.status = "completed"
        batch.finished_at = datetime.utcnow()
        db.commit()
        if jobs_processed == 0:
            try:
                logger.info(
                    f"CEIPAL batch {batch_id}: processed 0 jobs. Skip summary: "
                    f"missing_job_code={skip_counts.get('missing_job_code', 0)}, "
                    f"already_completed={skip_counts.get('already_completed', 0)}, "
                    f"invalid_jd={skip_counts.get('invalid_jd', 0)}, "
                    f"job_run_error={skip_counts.get('job_run_error', 0)}"
                )
                if skip_examples:
                    logger.info(f"CEIPAL batch {batch_id}: skip examples: {skip_examples}")
            except Exception:
                pass
        logger.info(f"CEIPAL batch {batch_id}: completed jobs={jobs_processed} selected={candidates_selected} msgs_sent={messages_sent}/{messages_attempted}")
    except Exception as e:
        logger.error(f"CEIPAL batch {batch_id}: failed: {e}", exc_info=True)
        try:
            batch = db.query(CeipalBatchRun).filter(CeipalBatchRun.id == str(batch_id)).first()
            if batch is not None:
                batch.status = "failed"
                batch.error = str(e)
                batch.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            pass
    finally:
        if _ceipal_rank_lock_acquired:
            try:
                _manual_rank_lock.release()
            except Exception:
                pass
        db.close()
@app.get("/api/ceipal/status")
def ceipal_status(limit: int = 50):
    db = SessionLocal()
    try:
        q = (
            db.query(CeipalJobRun)
            .order_by(CeipalJobRun.id.desc())
            .limit(max(1, min(int(limit), 200)))
            .all()
        )
        out = []
        for r in q:
            out.append(
                {
                    "id": r.id,
                    "job_code": r.job_code,
                    "job_title": r.job_title,
                    "job_status": r.job_status,
                    "status": r.status,
                    "score_threshold": r.score_threshold,
                    "total_candidates": r.total_candidates,
                    "selected_candidates": r.selected_candidates,
                    "error": r.error,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                }
            )
        return {"jobs": out}
    finally:
        db.close()


@app.get("/api/ceipal/status/{job_run_id}")
def ceipal_status_job(job_run_id: int, limit: int = 500):
    db = SessionLocal()
    try:
        job = db.query(CeipalJobRun).filter(CeipalJobRun.id == int(job_run_id)).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job run not found")
        # NOTE: This endpoint is used by the ATS UI. If the DB schema migration for
        # CeipalCandidateRun hasn't been applied yet, selecting the newer columns
        # (job_code/job_title/job_description/required_*_json) will crash the query.
        # So we explicitly select only legacy columns needed for rendering the ATS table.
        rows = (
            db.query(CeipalCandidateRun)
            .options(
                load_only(
                    CeipalCandidateRun.id,
                    CeipalCandidateRun.job_run_id,
                    CeipalCandidateRun.candidate_id,
                    CeipalCandidateRun.candidate_name,
                    CeipalCandidateRun.candidate_email,
                    CeipalCandidateRun.candidate_phone,
                    CeipalCandidateRun.score,
                    CeipalCandidateRun.sms_status,
                    CeipalCandidateRun.email_status,
                    CeipalCandidateRun.error,
                    CeipalCandidateRun.created_at,
                )
            )
            .filter(CeipalCandidateRun.job_run_id == int(job_run_id))
            .order_by(CeipalCandidateRun.id.asc())
            .limit(max(1, min(int(limit), 5000)))
            .all()
        )
        candidates = []
        for r in rows:
            # Compute a high-level status for the candidate.
            # Priority: final_selected > completed > called > scheduled > replied > messaged > not_sent
            high_status = ""
            final_selected = False
            final_notes = None
            latest_result_id = None
            transcript_available = False
            try:
                cid = str(r.candidate_id or "").strip()
                email_n = (str(r.candidate_email or "").strip().lower() if r.candidate_email else "")
                phone_n = _norm_e164(r.candidate_phone or "")

                fs_q = db.query(FinalSelectedCandidate)
                if cid:
                    fs_q = fs_q.filter(FinalSelectedCandidate.candidate_id == cid)
                elif phone_n:
                    fs_q = fs_q.filter(FinalSelectedCandidate.phone == phone_n)
                elif email_n:
                    fs_q = fs_q.filter(FinalSelectedCandidate.email == email_n)
                else:
                    fs_q = None
                if fs_q is not None:
                    fs_hit = fs_q.order_by(FinalSelectedCandidate.id.desc()).first()
                    if fs_hit is not None:
                        final_selected = True
                        final_notes = fs_hit.notes

                if phone_n:
                    # Any completed interview transcript?
                    try:
                        ir = (
                            db.query(InterviewResult)
                            .filter(InterviewResult.phone_number == phone_n)
                            .order_by(InterviewResult.id.desc())
                            .first()
                        )
                        if ir is not None:
                            latest_result_id = ir.id
                            transcript_available = True
                    except Exception:
                        pass
            except Exception:
                pass

            if final_selected:
                high_status = "final_selected"
            elif transcript_available:
                high_status = "completed"
            else:
                # Fallback to outreach statuses
                sms_s = str(r.sms_status or "").lower()
                email_s = str(r.email_status or "").lower()
                if sms_s == "sent" or email_s == "sent":
                    high_status = "messaged"
                elif sms_s == "failed" or email_s == "failed":
                    high_status = "message_failed"
                else:
                    high_status = "not_messaged"

            candidates.append(
                {
                    "id": r.id,
                    "candidate_id": r.candidate_id,
                    "candidate_name": r.candidate_name,
                    "candidate_email": r.candidate_email,
                    "candidate_phone": r.candidate_phone,
                    "score": r.score,
                    "sms_status": r.sms_status,
                    "email_status": r.email_status,
                    "error": r.error,
                    "current_status": high_status,
                    "final_selected": final_selected,
                    "final_notes": final_notes,
                    "latest_interview_result_id": latest_result_id,
                    "transcript_available": transcript_available,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )
        return {
            "job": {
                "id": job.id,
                "job_code": job.job_code,
                "job_title": job.job_title,
                "job_status": job.job_status,
                "status": job.status,
                "score_threshold": job.score_threshold,
                "total_candidates": job.total_candidates,
                "selected_candidates": job.selected_candidates,
                "error": job.error,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            },
            "candidates": candidates,
        }
    finally:
        db.close()


class FinalSelectedUpsert(BaseModel):
    candidate_id: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None


@app.get("/api/ats/final")
def ats_list_final(limit: int = 200):
    db = SessionLocal()
    try:
        rows = (
            db.query(FinalSelectedCandidate)
            .order_by(FinalSelectedCandidate.updated_at.desc(), FinalSelectedCandidate.id.desc())
            .limit(max(1, min(int(limit), 1000)))
            .all()
        )
        out = []
        for r in rows:
            out.append(
                {
                    "id": r.id,
                    "candidate_id": r.candidate_id,
                    "email": r.email,
                    "phone": r.phone,
                    "notes": r.notes,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
            )
        return {"items": out}
    finally:
        db.close()


@app.post("/api/ats/final")
def ats_upsert_final(payload: FinalSelectedUpsert):
    db = SessionLocal()
    try:
        cid = (payload.candidate_id or "").strip() or None
        email = (payload.email or "").strip().lower() or None
        phone = _norm_e164(payload.phone or "") or None
        notes = (payload.notes or "").strip() or None

        if not (cid or email or phone):
            raise HTTPException(status_code=400, detail="Provide at least one of candidate_id, email, or phone")

        q = db.query(FinalSelectedCandidate)
        if cid:
            q = q.filter(FinalSelectedCandidate.candidate_id == cid)
        elif phone:
            q = q.filter(FinalSelectedCandidate.phone == phone)
        else:
            q = q.filter(FinalSelectedCandidate.email == email)

        existing = q.order_by(FinalSelectedCandidate.id.desc()).first()
        if existing:
            existing.candidate_id = existing.candidate_id or cid
            existing.email = existing.email or email
            existing.phone = existing.phone or phone
            existing.notes = notes
            db.commit()
            db.refresh(existing)
            return {"status": "updated", "id": existing.id}

        row = FinalSelectedCandidate(candidate_id=cid, email=email, phone=phone, notes=notes)
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"status": "created", "id": row.id}
    finally:
        db.close()


@app.delete("/api/ats/final/{final_id}")
def ats_delete_final(final_id: int):
    db = SessionLocal()
    try:
        row = db.query(FinalSelectedCandidate).filter(FinalSelectedCandidate.id == int(final_id)).first()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        db.delete(row)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


@app.post("/api/ceipal/sync/jobs")
def ceipal_sync_jobs():
    _require_candidate_pipeline_enabled()
    global _ceipal_jobs
    try:
        _ceipal_jobs = _load_jobs_from_ceipal()
        return {"status": "ok", "jobs": len(_ceipal_jobs)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/ceipal/jobs")
def ceipal_list_jobs():
    return {"jobs": _ceipal_jobs}


@app.get("/api/ceipal/jobs/{job_code}")
def ceipal_get_job(job_code: str):
    job_code = (job_code or "").strip()
    for j in _ceipal_jobs:
        if str(j.get("JobCode") or "").strip() == job_code:
            return j
    raise HTTPException(status_code=404, detail="Job not found")


def _resolve_candidate_contact_fields(c: Dict[str, Any]) -> Dict[str, Optional[str]]:
    if not isinstance(c, dict):
        return {"name": None, "email": None, "phone": None}
    name = f"{c.get('FirstName', '')} {c.get('LastName', '')}".strip() or (c.get("ApplicantName") or None)
    email = (
        c.get("Email")
        or c.get("EmailAddress")
        or c.get("EmailID1")
        or c.get("EmailID2")
        or c.get("AlternateEmailAddress")
        or None
    )
    phone = (
        c.get("PhoneNumber")
        or c.get("MobileNumber")
        or c.get("phone")
        or c.get("Phone")
        or c.get("ContactNumber")
        or None
    )
    phone_norm = _norm_e164(str(phone)) if phone else ""
    return {
        "name": str(name).strip() if name else None,
        "email": str(email).strip().lower() if email else None,
        "phone": phone_norm or None,
    }


@app.get("/api/ceipal/reports/candidates")
def ceipal_candidates_report():
    _require_candidate_pipeline_enabled()
    client = build_ceipal_client_from_env()
    report_url = (os.getenv("CEIPAL_CANDIDATE_REPORT_URL") or "").strip()
    if not report_url:
        raise HTTPException(status_code=400, detail="CEIPAL_CANDIDATE_REPORT_URL is not set")
    try:
        data = client.get_report_data(report_url)
        return {"report": "candidates", "data": data}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


def _local_candidate_data_available() -> bool:
    try:
        candidate_data_file = (os.getenv("CANDIDATE_DATA_FILE") or "").strip() or None
        if candidate_data_file and os.path.exists(candidate_data_file):
            return True
        if os.path.exists(os.path.join(PROJECT_ROOT, "data.json")):
            return True
        if os.path.exists(DATA_FILE):
            return True
        return False
    except Exception:
        return True


def _ensure_rank_uses_local_candidates() -> None:
    global _candidates
    global _ceipal_candidates_loaded
    global _candidate_source
    if not _local_candidate_data_available():
        return

    try:
        # If CEIPAL candidates were loaded earlier in this process, switch back
        # to the local JSON dataset for manual/UI requests.
        if _candidate_source != "local":
            with _candidates_lock:
                _candidates = _load_candidates()
                _build_vector_index(_candidates)
                _ceipal_candidates_loaded = False
    except Exception as e:
        logger.error(f"Failed to load local JSON candidates: {e}", exc_info=True)


def _ensure_rank_uses_ceipal_candidates() -> None:
    global _candidates
    global _ceipal_candidates_loaded
    try:
        # Load CEIPAL candidates if not already loaded
        if not _ceipal_candidates_loaded:
            logger.info("Loading CEIPAL candidates for ranking")
            with _candidates_lock:
                _candidates = _load_candidates_from_ceipal()
                _ceipal_candidates_loaded = True
                try:
                    _build_vector_index(_candidates)
                except Exception as idx_ex:
                    logger.error(f"Failed to build vector index for CEIPAL candidates: {idx_ex}", exc_info=True)
                    raise
            logger.info(f"Loaded {len(_candidates) if _candidates else 0} CEIPAL candidates")
    except Exception as e:
        logger.error(f"Failed to load CEIPAL candidates: {e}", exc_info=True)
        # Fallback to local candidates if CEIPAL loading fails
        _ensure_rank_uses_local_candidates()


@app.post("/api/ceipal/sync/candidates")
def ceipal_sync_candidates():
    _require_candidate_pipeline_enabled()
    global _candidates
    global _ceipal_candidates_loaded

    try:
        with _candidates_lock:
            candidates = _load_candidates_from_ceipal()
            _candidates = candidates
            _build_vector_index(_candidates)
            _ceipal_candidates_loaded = True
        return {"status": "ok", "candidates": len(_candidates)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/job-description")
async def get_job_description():
    """Get the most recent job description that was used for ranking"""
    return {"job_description": _latest_job_description}


@app.get("/api/job-config")
async def get_job_config(db: Session = Depends(get_db)):
    """Get the latest saved job config (JD + important questions) for the UI."""
    cfg = db.query(JobConfig).order_by(JobConfig.id.desc()).first()
    if not cfg:
        return {
            "title": "",
            "description": _latest_job_description,
            "important_questions": [],
            "updated_at": None,
        }

    try:
        important_questions = json.loads(cfg.important_questions_json or "[]")
        if not isinstance(important_questions, list):
            important_questions = []
    except Exception:
        important_questions = []

    return {
        "id": cfg.id,
        "title": cfg.title or "",
        "description": cfg.description or "",
        "important_questions": important_questions,
        "updated_at": cfg.updated_at,
    }


@app.post("/api/job-config")
async def save_job_config(payload: JobConfigPayload, db: Session = Depends(get_db)):
    """Persist JD + important questions. Also updates the in-memory latest JD used elsewhere."""
    global _latest_job_description

    title = (payload.title or "").strip()
    description = (payload.description or "").strip()
    important_questions = payload.important_questions or []
    important_questions = [q.strip() for q in important_questions if isinstance(q, str) and q.strip()]

    cfg = JobConfig(
        title=title,
        description=description,
        important_questions_json=json.dumps(important_questions, ensure_ascii=False),
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)

    if description:
        _latest_job_description = description

    # Also update messaging service (SMS) job description context
    try:
        ms = get_messaging_service(db=db)
        if hasattr(ms, "update_job_description"):
            ms.update_job_description(description)
    except Exception:
        pass

    try:
        from app.messaging_service import messaging_service as sms_messaging_service
        if hasattr(sms_messaging_service, "update_job_description"):
            sms_messaging_service.update_job_description(description)
    except Exception:
        pass

    return {"status": "ok", "id": cfg.id}


def _find_latest_file(dir_path: str, prefix: str) -> Optional[str]:
    try:
        if not dir_path or not os.path.isdir(dir_path):
            return None
        matches = [f for f in os.listdir(dir_path) if f.startswith(prefix)]
        if not matches:
            return None
        matches.sort(reverse=True)
        return os.path.join(dir_path, matches[0])
    except Exception:
        return None


def _parse_transcript_file(path: str) -> Dict[str, Any]:
    """Parse the text transcript into structured Q/A entries."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()

        entries = []
        current_q = None
        current_a = None
        current_ts = None

        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Q") and ":" in line and line[1].isdigit():
                # Flush previous
                if current_q is not None:
                    entries.append({"question": current_q, "answer": current_a or "", "timestamp": current_ts or ""})
                current_q = line.split(":", 1)[1].strip()
                current_a = ""
                current_ts = ""
                continue
            if line.startswith("A") and ":" in line and line[1].isdigit():
                current_a = line.split(":", 1)[1].strip()
                continue
            if line.lower().startswith("timestamp:"):
                current_ts = line.split(":", 1)[1].strip()
                continue

        if current_q is not None:
            entries.append({"question": current_q, "answer": current_a or "", "timestamp": current_ts or ""})

        return {"raw": text, "entries": entries}
    except Exception as e:
        return {"raw": "", "entries": [], "error": str(e)}


def _parse_performance_file(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        return {"raw": text}
    except Exception as e:
        return {"raw": "", "error": str(e)}


@app.get("/api/interviews/{result_id}/detail")
async def get_interview_detail(result_id: int, db: Session = Depends(get_db)):
    """Return interview result + latest transcript/performance files for UI viewing."""
    result = db.query(InterviewResult).filter(InterviewResult.id == result_id).first()
    if not result:
        raise HTTPException(status_code=404, detail="Interview result not found")

    try:
        answers = json.loads(result.answers_json or "[]")
        if not isinstance(answers, list):
            answers = []
    except Exception:
        answers = []

    phone = (result.phone_number or "").strip()

    transcripts_dir = os.getenv("TRANSCRIPTS_DIR", "transcripts")
    performance_dir = os.getenv("PERFORMANCE_DIR", "Performance")

    transcript_path = _find_latest_file(transcripts_dir, f"interview_{phone}_") if phone else None
    performance_path = _find_latest_file(performance_dir, f"performance_{phone}_") if phone else None

    transcript = _parse_transcript_file(transcript_path) if transcript_path else {"raw": "", "entries": []}
    performance = _parse_performance_file(performance_path) if performance_path else {"raw": ""}

    phone_norm = _norm_e164(phone) if phone else ""
    messages: List[Dict[str, Any]] = []
    try:
        if phone_norm:
            sms_rows = (
                db.query(SmsLog)
                .filter(SmsLog.phone == phone_norm)
                .order_by(SmsLog.created_at.asc(), SmsLog.id.asc())
                .limit(500)
                .all()
            )
            for r in sms_rows:
                created_at_iso = r.created_at.isoformat() if getattr(r, "created_at", None) else None
                created_at_ts = int(r.created_at.timestamp()) if getattr(r, "created_at", None) else None
                messages.append(
                    {
                        "channel": "sms",
                        "direction": r.direction,
                        "message": r.message,
                        "status": r.status,
                        "created_at": created_at_iso,
                        "created_at_ts": created_at_ts,
                    }
                )

            # Email conversation (CEIPAL outreach) is stored by candidate_id.
            # Resolve candidate_id by phone via the most recent CEIPAL candidate run.
            cand_id = None
            try:
                c_run = (
                    db.query(CeipalCandidateRun)
                    .filter(CeipalCandidateRun.candidate_phone == phone_norm)
                    .order_by(CeipalCandidateRun.id.desc())
                    .first()
                )
                if c_run and c_run.candidate_id:
                    cand_id = str(c_run.candidate_id).strip() or None
            except Exception:
                cand_id = None

            if cand_id:
                email_rows = (
                    db.query(EmailLog)
                    .filter(EmailLog.candidate_id == cand_id)
                    .order_by(EmailLog.created_at.asc(), EmailLog.id.asc())
                    .limit(200)
                    .all()
                )
                for r in email_rows:
                    created_at_iso = r.created_at.isoformat() if getattr(r, "created_at", None) else None
                    created_at_ts = int(r.created_at.timestamp()) if getattr(r, "created_at", None) else None
                    messages.append(
                        {
                            "channel": "email",
                            "direction": "outgoing",
                            "message": r.body,
                            "subject": r.subject,
                            "status": r.status,
                            "created_at": created_at_iso,
                            "created_at_ts": created_at_ts,
                        }
                    )

        messages.sort(key=lambda x: (x.get("created_at_ts") is None, x.get("created_at_ts") or 0, str(x.get("created_at") or "")))
    except Exception as msg_ex:
        logger.warning(f"Failed loading message conversation for interview result_id={result_id}: {msg_ex}")

    return {
        "id": result.id,
        "phone_number": phone,
        "created_at": result.created_at,
        "feedback_text": result.feedback_text,
        "answers": answers,
        "transcript": transcript,
        "performance": performance,
        "messages": messages,
    }

@app.post("/api/voice/handle-key")
async def handle_voice_key(request: Request):
    """Disable the legacy interactive voice menu."""
    response = VoiceResponse()
    response.hangup()
    return Response(content=str(response), media_type="application/xml")

@app.post("/api/voice/response")
async def voice_response(request: Request, call_sid: str = None):
    """Disable the legacy interactive voice conversation."""
    response = VoiceResponse()
    response.hangup()
    return Response(content=str(response), media_type="application/xml")

@app.post("/api/call/status")
async def call_status(request: Request):
    """Accept Twilio call status callbacks without triggering follow-up actions."""
    logger.info("Received call status callback; no call workflow is active")
    return {"ok": True}

    # Legacy status logging/reschedule flow retained below but is unreachable.
    try:
        from app.messaging_service import messaging_service

        form_data = await request.form()
        call_sid = form_data.get('CallSid')
        call_status = form_data.get('CallStatus')
        call_direction = form_data.get('Direction')
        call_duration = form_data.get('CallDuration', '0')
        call_from = form_data.get('From', 'Unknown')
        call_to = form_data.get('To', 'Unknown')
        
        # Log all available form data for debugging
        logger.info(f"\n{'='*50}\n"
                   f"📞 Call Status Update\n"
                   f"  - SID: {call_sid}\n"
                   f"  - Status: {call_status}\n"
                   f"  - Direction: {call_direction}\n"
                   f"  - Duration: {call_duration}s\n"
                   f"  - From: {call_from}\n"
                   f"  - To: {call_to}\n"
                   f"  - All Data: {dict(form_data)}\n"
                   f"{'='*50}")
        
        # Detailed status handling
        if call_status == 'queued':
            logger.info(f"Call {call_sid} is queued and will be initiated shortly")
            
        elif call_status == 'ringing':
            logger.info(f"Call {call_sid} is now ringing on {call_to}")
            
        elif call_status == 'in-progress':
            logger.info(f"Call {call_sid} has been answered and is in progress")
            
        elif call_status == 'completed':
            duration = int(call_duration) if call_duration.isdigit() else 0
            if duration > 0:
                logger.info(f"✅ Call {call_sid} completed successfully after {duration} seconds")
            else:
                logger.warning(f"⚠️ Call {call_sid} completed with 0 duration - may have been missed")
                
        elif call_status == 'busy':
            logger.warning(f"⚠️ Call {call_sid} to {call_to} was busy")
            
        elif call_status == 'no-answer':
            logger.warning(f"⚠️ Call {call_sid} to {call_to} was not answered")
            
        elif call_status == 'failed':
            error_message = form_data.get('ErrorMessage', 'No error details')
            logger.error(f"❌ Call {call_sid} failed: {error_message}")
            
        elif call_status == 'canceled':
            logger.info(f"ℹ️ Call {call_sid} was canceled before being answered")

        # Missed-call reschedule flow for scheduled interviews.
        # Twilio may not always provide AnsweredBy=machine, so rely on status callbacks too.
        try:
            duration_val = int(call_duration) if str(call_duration).isdigit() else 0
            status_lc = str(call_status or "").lower()
            missed_statuses = {"no-answer", "busy", "failed", "canceled"}
            missed_call = status_lc in missed_statuses or (status_lc == "completed" and duration_val == 0)

            if missed_call and call_to and call_to != "Unknown":
                from datetime import datetime, timedelta

                db = SessionLocal()
                try:
                    now_utc = datetime.utcnow()
                    window_start = now_utc - timedelta(hours=2)
                    existing = (
                        db.query(InterviewSchedule)
                        .filter(
                            InterviewSchedule.candidate_phone == call_to,
                            InterviewSchedule.status.in_(["scheduled", "in_progress"]),
                            InterviewSchedule.scheduled_datetime >= window_start,
                        )
                        .order_by(InterviewSchedule.scheduled_datetime.asc())
                        .first()
                    )

                    if existing:
                        already_marked_no = (existing.call_pickup or "").strip().lower() == "no"
                        if not already_marked_no:
                            existing.call_pickup = "No"
                            db.commit()

                            try:
                                messaging_service.send_sms(
                                    call_to,
                                    (
                                        "We tried to reach you but you didn't respond to your interview call. "
                                        "Please tell me another suitable time for the interview in date-time-timezone format "
                                        "so that I can reschedule your interview again."
                                    ),
                                )
                                logger.info(f"Sent missed-call reschedule SMS to {call_to} (call_sid={call_sid})")
                            except Exception as sms_err:
                                logger.error(f"Failed to send missed-call reschedule SMS to {call_to}: {sms_err}")
                finally:
                    db.close()
        except Exception as resched_ex:
            logger.error(f"Failed missed-call reschedule handling for call_sid={call_sid}: {resched_ex}", exc_info=True)
            
        # Clean up resources for completed/failed calls
        if call_status in ['completed', 'failed', 'busy', 'no-answer', 'canceled']:
            logger.info(f"🔄 Cleaning up resources for call {call_sid} (status: {call_status})")
            try:
                from app.messaging import messaging_service as interview_messaging_service
                if hasattr(interview_messaging_service, "end_interview"):
                    interview_messaging_service.end_interview(call_sid)
                else:
                    logger.warning("Interview messaging service has no end_interview; skipping transcript cleanup")

                # Also drop any in-memory InterviewService state to avoid leaks.
                try:
                    global interview_service
                    if interview_service is not None and hasattr(interview_service, "active_interviews"):
                        if call_sid in interview_service.active_interviews:
                            interview_service.active_interviews.pop(call_sid, None)
                except Exception:
                    pass
                logger.info(f"✅ Successfully cleaned up resources for call {call_sid}")
            except Exception as e:
                logger.error(f"❌ Error cleaning up call {call_sid}: {str(e)}", exc_info=True)
        
        return JSONResponse(content={"status": "ok"})
        
    except Exception as e:
        logger.error(f"❌ Error processing call status: {str(e)}\n{traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "details": str(e)}
        )

@app.get("/api/transcripts/{call_sid}")
async def get_transcript(call_sid: str):
    """Retrieve a transcript by call SID."""
    try:
        # Get the transcript as text
        transcript_text = messaging_service.transcript_manager.get_transcript_text(call_sid)
        
        if not transcript_text or "No transcript found" in transcript_text:
            return JSONResponse(
                status_code=404,
                content={"error": "Transcript not found"}
            )
            
        # Return as plain text
        return Response(
            content=transcript_text,
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename=transcript_{call_sid}.txt"}
        )
        
    except Exception as e:
        logger.error(f"Error retrieving transcript for {call_sid}: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to retrieve transcript"}
        )

@app.get("/api/transcripts")
async def list_transcripts():
    """List all available transcripts."""
    try:
        # Get all transcript files
        transcripts_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts')
        if not os.path.exists(transcripts_dir):
            return JSONResponse(content={"transcripts": []})
            
        transcripts = []
        for filename in os.listdir(transcripts_dir):
            if filename.endswith('.json'):
                call_sid = filename.split('_')[1]  # Format: interview_<call_sid>_<timestamp>.json
                filepath = os.path.join(transcripts_dir, filename)
                mtime = os.path.getmtime(filepath)
                
                # Try to get metadata from the file
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        metadata = data.get('metadata', {})
                        
                    transcripts.append({
                        'call_sid': call_sid,
                        'filename': filename,
                        'created_at': metadata.get('saved_at', mtime),
                        'questions_asked': metadata.get('questions_asked', 0),
                        'url': f"/api/transcripts/{call_sid}"
                    })
                except Exception as e:
                    logger.error(f"Error reading transcript {filename}: {str(e)}")
        
        # Sort by creation time, newest first
        transcripts.sort(key=lambda x: x['created_at'], reverse=True)
        
        return JSONResponse(content={"transcripts": transcripts})
        
    except Exception as e:
        logger.error(f"Error listing transcripts: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"error": "Failed to list transcripts"}
        )


@app.get("/api/candidates/original")
def get_original_candidates() -> List[Dict[str, Any]]:
    """Return the original candidate records loaded from Applicant Data.json.

    This is used by the frontend (index.html) to populate
    window.originalCandidatesData.
    """
    if _candidates is None:
        raise HTTPException(status_code=500, detail="Candidates not loaded")
    try:
        with _candidates_lock:
            _ensure_rank_uses_local_candidates()
            return list(_candidates or [])
    except Exception:
        # Fall back to whatever is currently loaded rather than failing the UI.
        return list(_candidates or [])


@app.post("/rank", response_model=List[RankResult])
def rank(req: RankRequest, request: Request = None, use_ceipal_candidates: bool = False):
    _require_candidate_pipeline_enabled()
    global _latest_job_description
    global _latest_required_skills
    global _ceipal_jobs

    # Manual/UI flow: never use CEIPAL candidates.
    # Internal batch code calls rank() directly with request=None.
    if request is not None:
        use_ceipal_candidates = False

    # Manual HTTP /rank is CPU-heavy; serialize it and block CEIPAL batch starts while it runs.
    # We only acquire this lock for HTTP requests (request is not None).
    _rank_lock_acquired = False
    if request is not None:
        if not _manual_rank_lock.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="Ranking already in progress. Please retry in a moment.")
        _rank_lock_acquired = True

    if True:
        jd = (req.job_description or "").strip()
        if not jd:
            # Prefer latest persisted JobConfig description
            try:
                _db = SessionLocal()
                try:
                    cfg = _db.query(JobConfig).order_by(JobConfig.id.desc()).first()
                    if cfg and isinstance(cfg.description, str) and cfg.description.strip():
                        jd = cfg.description.strip()
                finally:
                    _db.close()
            except Exception as jd_db_err:
                logger.error(f"Failed to load JobConfig description for /rank: {jd_db_err}", exc_info=True)

        if not jd and isinstance(_ceipal_jobs, list) and _ceipal_jobs:
            # Fallback to most recent CEIPAL job description if available
            try:
                best = next((j for j in _ceipal_jobs if isinstance(j, dict) and (j.get("JobDescription") or "").strip()), None)
                if best:
                    jd = str(best.get("JobDescription") or "").strip()
            except Exception:
                pass

        # Mutate req.job_description so downstream logic uses the resolved value
        req.job_description = jd

        # Auto-derive requirements from JD if not explicitly provided
        if not req.required_skills:
            req.required_skills = _extract_required_skills_from_jd(jd)
        if req.min_experience_years is None:
            req.min_experience_years = _extract_min_experience_years_from_jd(jd)
        if not req.certifications:
            req.certifications = _extract_certifications_from_jd(jd)

        # Normalize caller-provided or extracted lists so non-skill requirements
        # (licenses, travel rules, years-of-experience phrases, generic soft skills)
        # don't accidentally become strict skill filters.
        try:
            if req.required_skills:
                req.required_skills = _normalize_skill_list(req.required_skills, limit=20)
        except Exception:
            pass
        try:
            if req.certifications:
                req.certifications = _normalize_skill_list(req.certifications, limit=20)
        except Exception:
            pass

        # Derive required screening questions for interview flows.
        # CEIPAL batch already calls _get_structured_requirements_for_job() upstream.
        # Manual /rank should also populate _latest_required_questions so InterviewService
        # can ask JD-grounded screening questions without hallucinations.
        if request is not None:
            try:
                structured_manual = _get_structured_requirements_for_manual_jd(req.role_title or "", jd)
                derived_questions = list(structured_manual.get("required_questions") or [])
                derived_questions = _validate_required_questions_against_jd(derived_questions, jd, limit=6)
                global _latest_required_questions
                _latest_required_questions = derived_questions
                if _latest_required_questions:
                    _save_latest_required_questions_to_file(_latest_required_questions)
            except Exception as q_ex:
                logger.warning(f"Failed deriving required_questions for manual /rank: {q_ex}")
        
        # Store the job description for later use
        _latest_job_description = req.job_description
        logger.info(f"Stored job description (first 100 chars): {_latest_job_description[:100]}...")

        # Cache the latest manual /rank context so manual /api/sms/send can reuse it
        # even if the frontend does not resend role_title/JD in the SMS request.
        if request is not None:
            try:
                with _latest_manual_rank_lock:
                    _latest_manual_rank_context.clear()
                    _latest_manual_rank_context.update(
                        {
                            "role_title": (req.role_title or "").strip(),
                            "job_description": (req.job_description or "").strip(),
                            "required_skills": list(req.required_skills or []),
                            "certifications": list(req.certifications or []),
                            "min_experience_years": req.min_experience_years,
                            "required_questions": list(globals().get("_latest_required_questions") or []),
                        }
                    )
            except Exception:
                pass

        # Persist manual /rank JD so subsequent /api/sms/send + /api/voice calls
        # can safely fall back to the latest JobConfig without relying on globals/files.
        try:
            _db = SessionLocal()
            try:
                cfg = JobConfig(
                    title=str(req.role_title or req.job_title or "").strip(),
                    description=str(req.job_description or "").strip(),
                    important_questions_json=json.dumps(list(globals().get("_latest_required_questions") or [])),
                )
                _db.add(cfg)
                _db.commit()
            finally:
                _db.close()
        except Exception as cfg_ex:
            logger.warning(f"Failed to persist JobConfig from manual /rank: {cfg_ex}")

        _latest_required_skills = [
            str(s).strip() for s in (req.required_skills or [])
            if s is not None and str(s).strip()
        ]

        _save_latest_required_skills_to_file(_latest_required_skills)

        # Also update the messaging service so Gemini SMS conversations can
        # reference the same job description for role/position related queries.
        try:
            from app.messaging_service import messaging_service as sms_service
            sms_service.update_job_description(req.job_description)
        except Exception as jd_ex:
            logger.error(f"Failed to propagate job description to MessagingService: {jd_ex}", exc_info=True)
        
        logger.info("="*80)
        logger.info(f"RECEIVED RANK REQUEST: {req.dict()}")
        logger.info(f"Use CEIPAL candidates: {use_ceipal_candidates}")
        logger.info("="*80)

        # IMPORTANT: CEIPAL scheduled batches may swap the global candidate dataset.
        # Select the dataset AND take a snapshot atomically under the same lock so
        # manual UI /rank requests can't accidentally run against CEIPAL candidates.
        with _candidates_lock:
            if use_ceipal_candidates:
                logger.info("Ranking against CEIPAL candidates")
                _ensure_rank_uses_ceipal_candidates()
            else:
                logger.info("Ranking against local JSON candidates")
                _ensure_rank_uses_local_candidates()

            # Snapshot candidate dataset + vector indices to avoid concurrent mutations
            # (e.g. CEIPAL scheduled refresh) causing idx_sorted to mismatch candidates.
            candidates_snapshot = list(_candidates or [])
            vectorizer_snapshot = _vectorizer
            matrix_snapshot = _candidate_matrix
            st_model_snapshot = _st_model
            sem_snapshot = _candidate_sem
    
    # Initialize healthcare candidates list and rejection tracking
    healthcare_candidates = []
    rejection_reasons = {
        'experience': 0,
        'title_similarity': 0,
        'skills': 0,
        'certification': 0,
        'other': 0
    }
    
    # Log initial request details
    logger.info(f"\n{'*'*50}")
    logger.info(f"PROCESSING REQUEST")
    logger.info(f"Role Title: {req.role_title}")
    logger.info(f"Min Experience: {req.min_experience_years} years")
    logger.info(f"Required Skills: {req.required_skills}")
    logger.info(f"Requested Certifications: {req.certifications}")
    logger.info(f"Job Description: {req.job_description[:200]}..." if req.job_description else "No job description")
    logger.info(f"Total candidates in system: {len(_candidates) if _candidates else 0}")
    
    if not (req.job_description or "").strip():
        raise HTTPException(status_code=400, detail="job_description is required")
    # (snapshot performed above under lock)

    if vectorizer_snapshot is None or matrix_snapshot is None:
        raise HTTPException(status_code=500, detail="Vector index is not initialized")

    # Prepare query text for semantic search
    query_parts = [req.job_description]
    if req.role_title:
        query_parts.append(f"Role Title: {req.role_title}")
    if req.required_skills:
        query_parts.append("Required Skills: " + ", ".join(req.required_skills))
    if req.min_experience_years is not None:
        query_parts.append(f"Minimum Experience: {req.min_experience_years} years")

    query_text = " \n ".join(query_parts)
    jd_lower = req.job_description.lower()
    # Heuristic: detect if the JD is talking about a specific location so that
    # we can give a small boost to candidates in that location.
    location_hint_keywords = [
        "based in ",
        "located in ",
        "location:",
        "work from ",
        "onsite",
        "on-site",
        "hybrid",
        "relocation",
    ]
    jd_has_location_hint = any(k in jd_lower for k in location_hint_keywords)
    
    # Log query text for debugging
    logger.info(f"\n{'*'*50}")
    logger.info("SEARCH QUERY:")
    logger.info(query_text)
    logger.info("-"*50)

    # Get TF-IDF similarities
    jd_vec = vectorizer_snapshot.transform([query_text])
    tfidf_sims = cosine_similarity(jd_vec, matrix_snapshot).flatten()

    # Get semantic similarities if model is available
    if st_model_snapshot is not None and sem_snapshot is not None:
        try:
            q_sem = st_model_snapshot.encode([query_text], normalize_embeddings=True, convert_to_numpy=True)[0].astype(np.float32)
            sem_sims = (sem_snapshot @ q_sem).astype(float)
            sims = 0.7 * sem_sims + 0.3 * tfidf_sims  # Weighted combination
            logger.info("Using semantic + TF-IDF similarity")
        except Exception as e:
            logger.warning(f"Error in semantic similarity: {str(e)}. Falling back to TF-IDF only.")
            sims = tfidf_sims
    else:
        sims = tfidf_sims
        logger.info("Using TF-IDF similarity only (semantic model not available)")

    # Get all indices sorted by score in descending order
    idx_sorted = sims.argsort()[::-1]
    logger.info(f"Top 5 similarity scores: {np.sort(sims)[-5:][::-1]}")

    results: List[RankResult] = []
    
    # Process role title matching
    input_role = (req.role_title or "").strip()
    role_lower = input_role.lower()

    def _normalize_role_for_lookup(title: str) -> str:
        t = (title or "").strip().lower()
        t = re.sub(r"[^a-z0-9\s\-/]+", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    def _any_candidate_title_matches_role(role_title: str) -> bool:
        """Return True if the dataset already contains reasonable matches for the original role title.

        This prevents fuzzy mapping from sending niche titles to unrelated roles.
        """
        r = _normalize_role_for_lookup(role_title)
        if not r:
            return False
        # Ignore very short role strings to avoid over-matching.
        if len(r) < 4:
            return False

        # Tokenize and drop common generic words.
        stop = {
            "and",
            "or",
            "the",
            "a",
            "an",
            "of",
            "to",
            "with",
            "for",
            "in",
            "on",
            "at",
            "tech",
            "technician",
            "technologist",
            "specialist",
            "associate",
            "ii",
            "iii",
        }
        tokens = [t for t in re.split(r"[\s\-/]+", r) if t and t not in stop]
        if not tokens:
            tokens = [r]

        found = 0
        # Early exit after we find a few plausible matches.
        for c0 in candidates_snapshot:
            ct = _normalize_role_for_lookup(str((c0 or {}).get("JobTitle") or ""))
            if not ct:
                continue
            # Fast contains check using any non-stop token.
            if not any(tok in ct for tok in tokens):
                continue
            sim = difflib.SequenceMatcher(None, r, ct).ratio()
            if sim >= 0.52:
                found += 1
                if found >= 3:
                    return True
        return found > 0
    
    # Enhanced role aliases with more healthcare roles
    aliases = {
        # Healthcare roles
        'rn': 'Registered Nurse',
        'r.n.': 'Registered Nurse',
        'medical-surgical rn': 'Registered Nurse',
        'medical surgical rn': 'Registered Nurse',
        'med-surg rn': 'Registered Nurse',
        'med surg rn': 'Registered Nurse',
        'med/surg rn': 'Registered Nurse',
        'med-surg registered nurse': 'Registered Nurse',
        'medical-surgical registered nurse': 'Registered Nurse',
        'medical surgical registered nurse': 'Registered Nurse',
        'registered nurse': 'Registered Nurse',
        'nurse': 'Registered Nurse',
        'md': 'Physician',
        'doctor': 'Physician',
        'physician': 'Physician',
        'radiologic technologist': 'Radiologic Technologist',
        'radiology tech': 'Radiologic Technologist',
        'x-ray tech': 'Radiologic Technologist',
        'xray tech': 'Radiologic Technologist',
        'pt': 'Physical Therapist',
        'physiotherapist': 'Physical Therapist',
        'physical therapist': 'Physical Therapist',
        'medical assistant': 'Medical Assistant',
        'ma': 'Medical Assistant',
        'lpn': 'Licensed Practical Nurse',
        'lvn': 'Licensed Vocational Nurse',
        'cna': 'Certified Nursing Assistant',
        'nursing assistant': 'Certified Nursing Assistant',
        'nurse practitioner': 'Nurse Practitioner',
        'np': 'Nurse Practitioner',
        'pa': 'Physician Assistant',
        'physician assistant': 'Physician Assistant',
        
        # Technical roles
        'ai': 'AI Engineer',
        'ml': 'Machine Learning Engineer',
        'machine learning': 'Machine Learning Engineer',
        'data scientist': 'Data Scientist',
        'data science': 'Data Scientist',
        'software engineer': 'Software Engineer',
        'software developer': 'Software Engineer',
        'devops': 'DevOps Engineer',
        'cloud engineer': 'Cloud Engineer',
    }
    
    # Map the input role to a standard role if possible
    mapped_role = None
    if role_lower:
        logger.info(f"Input role: '{input_role}' (lowercase: '{role_lower}')")

        # Only attempt role mapping if the current dataset does not already contain
        # reasonable matches for the original role title.
        allow_mapping = not _any_candidate_title_matches_role(input_role)
        if not allow_mapping:
            mapped_role = input_role
            logger.info(f"Role mapping skipped (dataset has title matches): '{input_role}'")

        rn_markers = (
            ' rn',
            'r.n',
            'registered nurse',
        )
        med_surg_markers = (
            'med surg',
            'med-surg',
            'med/surg',
            'medical surgical',
            'medical-surgical',
        )
        if mapped_role is None and (any(m in role_lower for m in rn_markers) or any(m in role_lower for m in med_surg_markers)):
            mapped_role = 'Registered Nurse'
            logger.info(f"Keyword role override found: '{input_role}' -> '{mapped_role}'")
        
        
        # Check direct match first
        if mapped_role is None and role_lower in aliases:
            mapped_role = aliases[role_lower]
            logger.info(f"Direct role match found: '{role_lower}' -> '{mapped_role}'")
        elif mapped_role is None:
            # Try to find a close match in our known roles
            roles = list(set(HEALTHCARE_ROLES.keys()).union(set(aliases.values())))
            # Use a higher cutoff to avoid mapping unrelated roles.
            match = difflib.get_close_matches(input_role, roles, n=1, cutoff=0.62)
            if match:
                mapped_role = match[0]
                logger.info(f"Fuzzy role match found: '{input_role}' -> '{mapped_role}'")
            else:
                logger.warning(f"No close match found for role: '{input_role}'. Will use as-is.")
                mapped_role = input_role  # Use the input role as is if no match found
    
    logger.info(f"Mapped role: '{mapped_role}'")

    # Get required skills for the role
    role_skills_lower: List[str] = []
    is_healthcare_role = False
    
    if mapped_role:
        # Check if it's a healthcare role (case-insensitive check)
        is_healthcare_role = any(mapped_role.lower() == role.lower() for role in HEALTHCARE_ROLES.keys())
        
        # Get role-specific skills
        if is_healthcare_role:
            # Find the correct case-sensitive key
            role_key = next((role for role in HEALTHCARE_ROLES.keys() 
                           if role.lower() == mapped_role.lower()), mapped_role)
            role_skills_lower = [s.lower() for s in HEALTHCARE_ROLES.get(role_key, [])]
            
            logger.info("\n" + "="*50)
            logger.info(f"HEALTHCARE ROLE DETECTED: {role_key}")
            logger.info(f"Required skills: {role_skills_lower}")
            logger.info("="*50 + "\n")
    
    # Process minimum experience - healthcare roles often require specific certifications
    min_exp_years = req.min_experience_years or 0
    if is_healthcare_role and min_exp_years < 1:  # Default to 1 year if not specified
        min_exp_years = 1
        logger.info(f"Setting minimum experience to {min_exp_years} year(s) for healthcare role")
    
    # Process required skills from the request and combine with role-specific skills
    req_skills_raw: List[str] = []
    for _s in (req.required_skills or []):
        if _s is None:
            continue
        if isinstance(_s, str):
            parts = [p.strip() for p in re.split(r"[\u2022\u2023\u25E6\u2043\u2219•;|,/]+", _s) if p and p.strip()]
            req_skills_raw.extend(parts)
        else:
            req_skills_raw.append(str(_s).strip())

    req_skills_lower = [(s or '').strip().lower() for s in req_skills_raw if (s or '').strip()]

    # Dedupe required skills while preserving order
    _seen_req = set()
    req_skills_lower = [s for s in req_skills_lower if not (s in _seen_req or _seen_req.add(s))]
    
    # For healthcare roles, use default skills if none specified, but be more flexible
    if is_healthcare_role and not req_skills_lower:
        req_skills_lower = role_skills_lower[:3]  # Take top 3 most important skills if none specified
        logger.info(f"Using default required skills for healthcare role: {req_skills_lower}")

    # For healthcare roles, huge required_skills lists become overly strict (can
    # require 20+ matches). Cap the list used for strict matching.
    if is_healthcare_role and len(req_skills_lower) > 0:
        max_req_skills = int(os.getenv("HEALTHCARE_MAX_REQUIRED_SKILLS", "12"))
        if max_req_skills > 0 and len(req_skills_lower) > max_req_skills:
            req_skills_lower = req_skills_lower[:max_req_skills]
            logger.info(f"Capped required skills for healthcare matching to top {max_req_skills}: {req_skills_lower}")
    
    # Make a copy of required skills that we can modify for matching
    effective_req_skills = req_skills_lower.copy()
    
    # For healthcare roles, be more flexible with certifications
    if is_healthcare_role and mapped_role and 'nurse' in mapped_role.lower():
        # If BLS is required, also check for CPR as an alternative
        if 'bls' in effective_req_skills and 'cpr' not in effective_req_skills:
            effective_req_skills.append('cpr')
        # If RN is required, also check for variations
        if 'rn' in effective_req_skills and 'registered nurse' not in effective_req_skills:
            effective_req_skills.append('registered nurse')
    
    # Process requested certifications (optional)
    req_certs_raw: List[str] = []
    for _c in (req.certifications or []):
        if _c is None:
            continue
        if isinstance(_c, str):
            parts = [p.strip() for p in re.split(r"[\u2022\u2023\u25E6\u2043\u2219•;|,/]+", _c) if p and p.strip()]
            req_certs_raw.extend(parts)
        else:
            req_certs_raw.append(str(_c).strip())
    def _normalize_cert_token(x: str) -> str:
        s = str(x or "").strip().lower()
        s = re.sub(r"\s+", " ", s)
        s = s.replace("-", " ")
        s = s.replace("_", " ")
        s = s.replace("&", " and ")
        s = re.sub(r"\s+", " ", s).strip()
        # Remove common issuer prefixes that should not affect matching
        s = re.sub(r"\b(aha|american heart association|red cross|arc)\b", "", s).strip()
        # Normalize common variants
        s = re.sub(r"\b(bcls)\b", "bls", s)
        s = re.sub(r"\b(nihss?)\b", "nihss", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    req_certs_lower = [_normalize_cert_token(c) for c in req_certs_raw if (c or '').strip()]
    _seen_c = set()
    req_certs_lower = [c for c in req_certs_lower if not (c in _seen_c or _seen_c.add(c))]

    logger.info(f"Processing {len(idx_sorted)} candidates with filters: role='{mapped_role or input_role}', "
                f"min_exp={min_exp_years}y, req_skills={req_skills_lower}, req_certs={req_certs_lower}")
    
    candidates_processed = 0
    candidates_matched = 0
    
    max_idx = len(candidates_snapshot)
    for idx in idx_sorted:
        if idx < 0 or idx >= max_idx:
            continue
        c = candidates_snapshot[idx]
        candidates_processed += 1
        
        # Get candidate skills and experience
        def _norm_skill(_s: str) -> str:
            _s = (_s or "").strip().lower()
            _s = re.sub(r"\s+", " ", _s)
            _s = _s.replace("-", " ")
            _s = _s.replace("_", " ")
            _s = _s.replace("&", " and ")
            return _s.strip()

        def _candidate_skill_tokens(_raw):
            if _raw is None:
                tokens = []
            elif isinstance(_raw, str):
                tokens = [t.strip() for t in re.split(r"[,;|/]+", _raw) if t and t.strip()]
            elif isinstance(_raw, (list, tuple, set)):
                tokens = [str(t).strip() for t in _raw if str(t).strip()]
            else:
                tokens = [str(_raw).strip()] if str(_raw).strip() else []

            norm = []
            for t in tokens:
                nt = _norm_skill(t)
                if not nt:
                    continue
                norm.append(nt)
                if "patient assessment" in nt or nt == "assessment":
                    norm.append("assessment")
                if "medication admin" in nt:
                    norm.append("medication administration")
                if nt in {"iv", "iv access", "iv insertion", "iv starts", "iv start"} or "iv " in nt:
                    norm.append("iv therapy")
                if "vitals" in nt or "vital" in nt:
                    norm.append("vital signs")
                if "post op" in nt or "post operative" in nt or "post-operative" in nt:
                    norm.append("post operative care")
                if "ehr" in nt or "emr" in nt:
                    norm.append("clinical documentation")
            # Dedupe while preserving order
            _seen = set()
            out = []
            for t in norm:
                if t in _seen:
                    continue
                _seen.add(t)
                out.append(t)
            return out

        c_skills_list = _candidate_skill_tokens(c.get("Skills"))
        # Also parse Certifications field and merge into skills for matching
        cert_tokens = _candidate_skill_tokens(c.get("Certifications"))
        if cert_tokens:
            # Prepend to give certifications priority in contains checks
            c_skills_list = cert_tokens + [s for s in c_skills_list if s not in cert_tokens]
        c_skills = ", ".join(c_skills_list)
        c_title = str(c.get("JobTitle") or "").lower()
        early_title_similarity = 0.0
        strong_title_match = False
        if input_role and c_title:
            early_title_similarity = difflib.SequenceMatcher(None, input_role.lower(), c_title.lower()).ratio()
            strong_title_match = (input_role.lower().strip() == c_title.lower().strip()) or (early_title_similarity >= 0.5)
        exp_years = 0
        exp_str = str(c.get("Experience") or "").lower()
        
        # Try to extract years from experience string
        match = re.search(r'(\d+)\s*(?:year|yr|yrs|\.)', exp_str)
        if match:
            exp_years = int(match.group(1))
            
        # For healthcare roles, be more flexible with experience requirements
        if is_healthcare_role:
            # Reduce experience requirement if no candidates are found
            effective_min_exp = min_exp_years
            if candidates_processed > 20 and candidates_matched == 0:
                effective_min_exp = max(1, min_exp_years - 2)  # Reduce by up to 2 years if no matches
                
            if exp_years < effective_min_exp:
                # Defer experience enforcement to the unified experience check later
                # Do not reject here to avoid double-filtering
                pass
            
            # Check for relevant certifications in skills, not experience
            if mapped_role == 'Registered Nurse':
                if not strong_title_match and not any(cert in c_skills.lower() for cert in ['rn', 'bscn', 'bsn', 'registered nurse', 'r.n.']):
                    rejection_reasons['certification'] += 1
                    continue
                    
            elif mapped_role == 'Physician':
                if not strong_title_match and not any(cert in c_skills.lower() for cert in ['md', 'do', 'mbbs', 'physician']):
                    rejection_reasons['certification'] += 1
                    continue
                
            # Add more role-specific validations as needed
                
        # For non-healthcare roles, use the original experience filter
        elif min_exp_years > 0 and exp_years < min_exp_years:
            rejection_reasons['experience'] += 1
            continue
        
        # Filter by job title if specified
        if input_role:
            if not c_title:
                continue
                
            # Check if the role appears in the title (with some flexibility)
            alias_value = aliases.get(role_lower)
            alias_terms = []
            if isinstance(alias_value, str) and alias_value:
                alias_terms = [alias_value.lower()]
            elif isinstance(alias_value, (list, tuple, set)):
                alias_terms = [str(a).lower() for a in alias_value if a]

            # Add common abbreviations/keywords for RN when mapped role is Registered Nurse
            if mapped_role and mapped_role.lower() == 'registered nurse':
                # Include 'rn' and general 'nurse' to capture titles like 'OR RN', 'Travel RN', 'Staff Nurse'
                for extra in ['rn', 'registered nurse', 'nurse']:
                    if extra not in alias_terms:
                        alias_terms.append(extra)

            title_matches = (
                input_role.lower() == c_title or
                c_title in input_role.lower() or
                input_role.lower() in c_title or
                (mapped_role and mapped_role.lower() in c_title) or
                any(term in c_title for term in alias_terms)
            )
            
            # If simple contains checks fail, allow strong title similarity to pass
            if not title_matches and not strong_title_match:
                rejection_reasons['title_similarity'] += 1
                continue
        
        # Get candidate skills
        c_skills = ", ".join(c_skills_list)
        
        # For healthcare roles, we'll be more strict with skill matching
        if is_healthcare_role:
            # Check for exact matches of role-specific skills first
            role_skill_matches = []
            for skill in role_skills_lower:
                if any(skill in s or s in skill for s in c_skills_list):
                    role_skill_matches.append(skill)
            
            # Calculate how many of the role's core skills are matched
            role_skill_score = len(role_skill_matches) / max(1, len(role_skills_lower[:5]))  # Look at top 5 skills
            
            # Check for required certifications/licensure
            certification_score = 0
            if mapped_role == 'Registered Nurse' and any(cert in c_skills for cert in ['rn', 'bscn', 'bsn', 'registered nurse']):
                certification_score = 1.0
            elif mapped_role == 'Physician' and any(cert in c_skills for cert in ['md', 'do', 'mbbs']):
                certification_score = 1.0
            # Add more role-specific certifications as needed
            
            # Calculate match for requested skills with more flexible matching
            matched_req_skills = []
            for skill in effective_req_skills:
                # Try exact match first
                if any(skill.lower() == s.lower() for s in c_skills_list):
                    matched_req_skills.append(skill)
                    continue
                    
                # Try partial match
                if any(skill.lower() in s.lower() or s.lower() in skill.lower() for s in c_skills_list):
                    matched_req_skills.append(skill)
                    continue
                    
                # Try acronym expansion (e.g., BLS -> Basic Life Support)
                if skill.lower() == 'bls' and any('basic life support' in s.lower() for s in c_skills_list):
                    matched_req_skills.append(skill)
                elif skill.lower() == 'cpr' and any('cardiopulmonary resuscitation' in s.lower() for s in c_skills_list):
                    matched_req_skills.append(skill)
            
            req_skill_score = len(matched_req_skills) / max(1, len(req_skills_lower)) if req_skills_lower else 1.0
            
            # For healthcare, we need at least some relevant skills, but allow strong title matches to pass
            if is_healthcare_role and role_skill_score < 0.3 and req_skill_score < 0.3:
                if not strong_title_match:
                    rejection_reasons['skills'] += 1
                    continue  # Skip only when title isn't a clear RN match
        
        # Track healthcare candidates for debugging
        is_healthcare_candidate = any(term in c_title.lower() or 
                                     any(term in skill for skill in c_skills_list)
                                     for term in ['nurse', 'rn', 'lpn', 'lvn', 'cna', 'physician', 'doctor'])
            
        if is_healthcare_candidate:
            healthcare_candidates.append({
                'id': c.get("_id"),
                'title': c_title,
                'skills': c_skills,
                'experience': c.get("Experience"),
                'similarity': float(sims[idx]) if idx < len(sims) else 0.0
            })
        
        # 1. Role Title Matching - More flexible for healthcare roles
        title_similarity = 0.0
        exact_title_match = False
        if input_role:
            exact_title_match = (input_role.lower().strip() == c_title.lower().strip())
            # Try multiple matching strategies
            title_similarity = max(
                difflib.SequenceMatcher(None, input_role.lower(), c_title.lower()).ratio(),
                difflib.SequenceMatcher(None, mapped_role.lower() if mapped_role else "", c_title.lower()).ratio()
            )
            
            # For healthcare roles, be more lenient with title matching
            min_title_similarity = 0.3 if is_healthcare_role else 0.4
            
            if title_similarity < min_title_similarity:
                # Check if any keyword from the input role is in the candidate's title
                input_keywords = set(input_role.lower().split())
                title_keywords = set(c_title.lower().split())
                
                if not input_keywords.intersection(title_keywords):
                    rejection_reasons['title_similarity'] += 1
                    continue
        
        # 2. Experience Filtering - More flexible for healthcare roles
        exp_years = 0
        try:
            exp_val = c.get("Experience", 0)
            if isinstance(exp_val, (int, float)):
                exp_years = int(exp_val)
            else:
                exp_str = str(exp_val or "").strip().lower()
                # Direct numeric string (e.g., "25")
                if re.fullmatch(r"\d+", exp_str):
                    exp_years = int(exp_str)
                else:
                    # Extract first number from strings like "25 yrs", "10 years", "1+ year"
                    m = re.search(r"(\d+)", exp_str)
                    if m:
                        exp_years = int(m.group(1))
                    # Handle plus notation implying at least 1 year
                    if exp_years == 0 and "+" in exp_str:
                        exp_years = 1
        except Exception as e:
            logger.debug(f"Error parsing experience for candidate {c.get('_id')}: {e}")
        
        # For healthcare roles, be slightly more lenient with experience
        exp_threshold = max(0, min_exp_years - (1 if is_healthcare_role else 0))
        # If the candidate clearly looks like RN (title strong match or RN markers), do not reject on exp
        rn_mark_in_title = any(m in c_title for m in [' rn', 'registered nurse', 'nurse'])
        rn_mark_in_skills = any(m in c_skills for m in [' rn', 'registered nurse', 'bsn', 'bscn'])
        relax_exp_for_strong_title = is_healthcare_role and (strong_title_match or rn_mark_in_title or rn_mark_in_skills)
        if relax_exp_for_strong_title:
            exp_threshold = 0
        if exp_years < exp_threshold:
            rejection_reasons['experience'] += 1
            continue
            
        # 3. Skills Matching - More flexible for healthcare roles
        matched_skills = []
        skill_match_required = len(req_skills_lower) > 0
        
        if req_skills_lower:
            # Only report matches against the JD-requested skills (not internal expansions)
            # so the frontend highlights only what was asked for.
            for skill in req_skills_lower:
                skill_lower = _norm_skill(skill)

                # Exact match
                if any(skill_lower == _norm_skill(s) for s in c_skills_list):
                    matched_skills.append(skill)
                    continue

                # Partial match
                if any(skill_lower in _norm_skill(s) or _norm_skill(s) in skill_lower for s in c_skills_list):
                    matched_skills.append(skill)
                    continue

                # Healthcare equivalencies
                if is_healthcare_role:
                    # BLS can be satisfied by CPR mentions
                    if skill_lower == 'bls' and any(t in c_skills for t in ['cpr', 'cardiopulmonary resuscitation', 'basic life support']):
                        matched_skills.append(skill)
                        continue

                    # ACLS can be satisfied by long-form mentions
                    if skill_lower == 'acls' and any(t in c_skills for t in ['acls', 'advanced cardiac life support']):
                        matched_skills.append(skill)
                        continue

                    # RN license can be satisfied by RN / Registered Nurse
                    if skill_lower in {'active rn license', 'rn license', 'rn'} and any(t in c_skills for t in ['rn', 'r.n', 'registered nurse']):
                        matched_skills.append(skill)
                        continue

            # For healthcare roles, do NOT require a percentage of a potentially long
            # skill list; instead require a small minimum number of matches.
            min_required_skills = int(os.getenv("HEALTHCARE_MIN_REQUIRED_SKILL_MATCHES", "1"))
            if len(req_skills_lower) <= 2:
                min_required_skills = 1
            min_required_skills = max(1, min_required_skills)
            # If the title is an exact match, don't block a candidate just because
            # the JD included verbose/non-skill requirements in required_skills.
            if exact_title_match:
                min_required_skills = 0
            elif title_similarity >= 0.5:
                min_required_skills = 1

            # If certifications are explicitly required and the title is already a
            # strong match, do not over-filter by skills.
            if is_healthcare_role and req_certs_lower and title_similarity >= 0.5:
                min_required_skills = 0
            
            if len(matched_skills) < min_required_skills:
                rejection_reasons['skills'] += 1
                continue
        
        # 4. Certifications Matching (optional)
        cert_match_ratio = 0.0
        if 'req_certs_lower' in locals() and req_certs_lower:
            matched_certs = []
            for cert in req_certs_lower:
                cert_l = _normalize_cert_token(cert)
                # Exact token match against merged skills+certifications tokens
                if any(cert_l == _normalize_cert_token(s) for s in c_skills_list):
                    matched_certs.append(cert)
                    continue
                # Partial match
                if any(cert_l in _normalize_cert_token(s) or _normalize_cert_token(s) in cert_l for s in c_skills_list):
                    matched_certs.append(cert)
                    continue
                # Common healthcare acronyms/long-forms
                if cert_l == 'bls' and any(t in c_skills for t in ['bls', 'basic life support', 'cpr', 'cardiopulmonary resuscitation']):
                    matched_certs.append(cert)
                    continue
                if cert_l == 'acls' and any(t in c_skills for t in ['acls', 'advanced cardiac life support']):
                    matched_certs.append(cert)
                    continue
                if cert_l == 'nrp' and any(t in c_skills for t in ['nrp', 'neonatal resuscitation program']):
                    matched_certs.append(cert)
                    continue
                if cert_l == 'fhm' and any(t in c_skills for t in ['fhm', 'fetal heart monitoring']):
                    matched_certs.append(cert)
                    continue
            cert_match_ratio = len(matched_certs) / max(1, len(req_certs_lower))

            # By default we treat certifications as a scoring signal only.
            # Some datasets are incomplete/messy about certs, and hard rejection
            # leads to 0 results in manual flow.
            hard_require_certs = os.getenv("HARD_REQUIRE_CERTIFICATIONS", "0").strip().lower() in {"1", "true", "yes"}
            # CEIPAL candidates often do not include a reliable certification field,
            # so never hard-reject in CEIPAL flow.
            if hard_require_certs and (not use_ceipal_candidates) and (not matched_certs):
                rejection_reasons['certification'] += 1
                continue

        # If we get here, the candidate matches all criteria
        candidates_matched += 1

        # Calculate score (weighted average of title similarity and skill match ratio)
        skill_match_ratio = len(matched_skills) / max(1, len(req_skills_lower)) if req_skills_lower else 1.0
        skill_match_ratio = min(1.0, max(0.0, skill_match_ratio))

        # Optional location-based adjustment when JD appears to specify a location
        location_match_score = 0.0
        if jd_has_location_hint:
            city = (c.get("City") or "").strip().lower()
            state = (c.get("State") or "").strip().lower()
            country = (c.get("Country") or "").strip().lower()

            # Prioritize city > state > country matches
            if city and city in jd_lower:
                location_match_score = 1.0
            elif state and state in jd_lower:
                location_match_score = 0.8
            elif country and country in jd_lower:
                location_match_score = 0.5

        # For healthcare roles, prioritize title > certifications > skills
        if is_healthcare_role:
            if req_certs_lower:
                score = (0.5 * title_similarity) + (0.25 * cert_match_ratio) + (0.25 * skill_match_ratio)
            else:
                score = (0.5 * title_similarity) + (0.5 * skill_match_ratio)
        else:
            if req_certs_lower:
                score = (0.6 * title_similarity) + (0.15 * cert_match_ratio) + (0.25 * skill_match_ratio)
            else:
                score = (0.6 * title_similarity) + (0.4 * skill_match_ratio)

        if exact_title_match:
            score = max(score, 0.65)
        elif title_similarity >= 0.5:
            score = max(score, 0.5)

        # Add a small location bonus when JD specifies a location and the
        # candidate's city/state/country matches that location.
        if jd_has_location_hint and location_match_score > 0:
            score += 0.15 * location_match_score
        
        # Add experience bonus (up to 0.1)
        if exp_years > min_exp_years:
            exp_bonus = min(0.1, (exp_years - min_exp_years) * 0.02)
            score += exp_bonus
            
        # Ensure score is within [0, 1]
        is_perfect_match = (skill_match_ratio >= 0.999) and (exact_title_match or title_similarity >= 0.9)
        if not is_perfect_match:
            score = min(score, 0.99)
        score = max(0.0, min(1.0, score))

        # Debug: inspect candidate with _id == '-2' (Sonali) to see phone fields
        try:
            if str(c.get("_id")) == "-2":
                logger.info("DEBUG Sonali raw candidate: %s", c)
                logger.info(
                    "DEBUG Sonali phone keys: phone=%s, Phone=%s, PhoneNumber=%s, Phone_No=%s, ContactNumber=%s",
                    c.get("phone"),
                    c.get("Phone"),
                    c.get("PhoneNumber"),
                    c.get("Phone_No"),
                    c.get("ContactNumber"),
                )
        except Exception as debug_ex:
            logger.warning("DEBUG logging for Sonali failed: %s", debug_ex)
        
        # Derive phone from any plausible phone-related key
        raw_phone = (
            c.get("phone")
            or c.get("Phone")
            or c.get("PhoneNumber")
            or c.get("Phone_No")
            or c.get("ContactNumber")
        )

        def _safe_str(v):
            if v is None:
                return None
            try:
                s = str(v).strip()
            except Exception:
                return None
            return s or None

        city_val = _safe_str(c.get("City"))
        state_val = _safe_str(c.get("State"))
        country_val = _safe_str(c.get("Country"))
        email_val = _safe_str(c.get("EmailID1") or c.get("EmailID2")) or ""

        # Add to results
        results.append(
            RankResult(
                id=str(c["_id"]),
                name=f"{c.get('FirstName', '')} {c.get('LastName', '')}".strip(),
                email=email_val,
                phone=str(raw_phone).strip() or None,
                job_title=c_title,
                city=city_val,
                state=state_val,
                country=country_val,
                experience=str(c.get("Experience")) if c.get("Experience") is not None else "",
                skills=c.get("Skills"),
                score=round(score, 4),
                matched_skills=matched_skills,
                mapped_role=mapped_role
            )
        )
        
        # Apply top_k limit if specified
        if req.top_k and len(results) >= req.top_k:
            break
    
    # Log detailed rejection reasons
    logger.info("\n" + "="*50)
    logger.info("REJECTION ANALYSIS")
    logger.info("-"*50)
    logger.info(f"Total candidates processed: {candidates_processed}")
    logger.info(f"Candidates matched: {candidates_matched}")
    logger.info("Candidates rejected by:")
    for reason, count in rejection_reasons.items():
        if count > 0:
            logger.info(f"  - {reason}: {count} ({(count/candidates_processed*100):.1f}%)")
    
    # Log healthcare candidates found
    if healthcare_candidates:
        logger.info("\nHEALTHCARE CANDIDATES FOUND:")
        for i, hc in enumerate(healthcare_candidates[:5]):  # Show top 5
            logger.info(f"  {i+1}. ID: {hc['id']}, Title: {hc['title']}")
            logger.info(f"     Experience: {hc['experience']}, Similarity: {hc['similarity']:.3f}")
            logger.info(f"     Skills: {hc['skills'][:100]}...")
    
    logger.info("="*50 + "\n")
    
    # Sort results by score in descending order
    results.sort(key=lambda x: x.score, reverse=True)
    
    logger.info(f"Processed {candidates_processed} candidates, matched {candidates_matched}")
    logger.info(f"Returning {len(results)} results")
    
    # Only apply top_k if explicitly specified in the request
    # If not specified (None) or 0, return all results
    if req.top_k is not None and req.top_k > 0:
        final_results = results[:req.top_k]
        logger.info(f"Returning top {len(final_results)} of {len(results)} candidates due to top_k limit")
    else:
        final_results = results
        logger.info(f"Returning all {len(final_results)} matching candidates")
    
    if final_results:
        logger.info(f"First candidate: {final_results[0].name} (score: {final_results[0].score:.4f}, "
                   f"title: {final_results[0].job_title}, skills: {final_results[0].matched_skills})")
        if len(final_results) > 1:
            logger.info(f"Last candidate: {final_results[-1].name} (score: {final_results[-1].score:.4f}, "
                       f"title: {final_results[-1].job_title})")
    
    # Convert to dict to ensure proper serialization
    response_data = [r.dict() for r in final_results]
    logger.info(f"Response contains {len(response_data)} candidates, "
               f"total size: {len(json.dumps(response_data))} bytes")

    if _rank_lock_acquired:
        try:
            _manual_rank_lock.release()
        except Exception:
            pass

    return response_data


@app.post("/best/add")
def best_add(req: BestListRequest):
    db = SessionLocal()
    try:
        lst = db.query(BestList).filter(BestList.name == req.list_name).first()
        if not lst:
            lst = BestList(name=req.list_name)
            db.add(lst)
            db.commit()
            db.refresh(lst)

        added = 0
        for cid in req.candidate_ids:
            existing = (
                db.query(BestListItem)
                .filter(BestListItem.list_id == lst.id, BestListItem.candidate_id == str(cid))
                .first()
            )
            if existing:
                continue
            db.add(BestListItem(list_id=lst.id, candidate_id=str(cid)))
            added += 1

        db.commit()
        return {"status": "ok", "added": added}
    finally:
        db.close()


@app.get("/best/{list_name}")
def best_get(list_name: str):
    db = SessionLocal()
    try:
        lst = db.query(BestList).filter(BestList.name == list_name).first()
        if not lst:
            return {"list": list_name, "candidates": []}

        cids = [
            r.candidate_id
            for r in (
                db.query(BestListItem)
                .filter(BestListItem.list_id == lst.id)
                .order_by(BestListItem.added_at.desc(), BestListItem.id.desc())
                .all()
            )
        ]
    finally:
        db.close()

    # Map back to candidate info
    id_to_candidate = {str(c.get("_id")): c for c in _candidates}
    items = []
    for cid in cids:
        c = id_to_candidate.get(str(cid))
        if not c:
            continue
        items.append({
            "id": str(c.get("_id")),
            "name": f"{c.get('FirstName', '')} {c.get('LastName', '')}".strip(),
            "email": (c.get("EmailID1") or c.get("EmailID2")),
            "job_title": c.get("JobTitle"),
            "skills": c.get("Skills"),
        })
    return {"list": list_name, "candidates": items}


@app.post("/email/send")
def email_send(req: EmailRequest):
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, HtmlContent, To, Email
    import os

    # Ensure we always pick up the latest templates.json without requiring a server restart.
    try:
        template_manager.reload()
    except Exception:
        pass

    # Resolve candidates
    id_to_candidate = {str(c.get("_id")): c for c in _candidates}
    selected = [id_to_candidate.get(str(cid)) for cid in req.candidate_ids]
    selected = [c for c in selected if c]

    if not selected:
        raise HTTPException(status_code=400, detail="No valid candidates provided")

    # Get SendGrid API key from environment variables
    sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("SENDGRID_FROM_EMAIL") or "noreply@example.com"
    # Inbound email address used for replies (handled by SendGrid Inbound Parse)
    # Example: interviews@inbound.radixsol.com
    reply_to_email = os.getenv("SENDGRID_INBOUND_EMAIL")
    
    if not sendgrid_api_key:
        raise HTTPException(
            status_code=400, 
            detail="SendGrid API key not configured. Set SENDGRID_API_KEY in environment variables."
        )

    sent = 0
    errors = []
    sg = SendGridAPIClient(sendgrid_api_key)

    db = SessionLocal()
    try:
        # Determine role from frontend if provided
        role_from_frontend = getattr(req, 'role', None)

        for c in selected:
            to_email = (c.get("EmailID1") or c.get("EmailID2"))
            if not to_email:
                errors.append({"id": c.get("_id"), "error": "Missing email"})
                continue
                
            # Resolve subject/body preferring templates.json first, then env FIRST_OUTREACH_*, then hardcoded.
            # If the UI sends an old default body/subject, override it with templates.json.
            req_subject = (req.subject or "").strip() if hasattr(req, "subject") else ""
            req_body = (req.body_html or "").strip() if hasattr(req, "body_html") else ""

            ui_default_detected = False
            if req_body:
                body_low = req_body.lower()
                ui_default_detected = (
                    ("noticed your experience in" in body_low)
                    or ("[specialty]" in req_body)
                    or ("[your name]" in body_low)
                    or ("many clinicians" in body_low)
                    or ("[name]" in req_body)
                    or ("[title]" in req_body)
                    or ("[company]" in req_body)
                )
            if req_subject:
                subj_low = req_subject.lower()
                ui_default_detected = ui_default_detected or ("quick question about your next move" in subj_low)

            prefer_templates = (os.getenv("PREFER_EMAIL_TEMPLATES") or "true").strip().lower() in {"1", "true", "yes", "y"}

            tpl = template_manager.get_email_template("first_outreach")
            tpl_has_content = bool((tpl.get("subject") or "").strip() or (tpl.get("body") or "").strip())

            use_templates = tpl_has_content and (prefer_templates or (not req_subject) or (not req_body) or ui_default_detected)
            if use_templates:
                logger.info("/email/send: using templates.json -> email.first_outreach")
            else:
                logger.info("/email/send: using request-provided subject/body (templates.json not applied)")

            subject_val = (
                (tpl.get("subject") if use_templates else req_subject)
                or ("" if use_templates else tpl.get("subject"))
                or os.getenv("FIRST_OUTREACH_EMAIL_SUBJECT", "[Name], quick question about your next move")
            )
            body_val = (
                (tpl.get("body") if use_templates else req_body)
                or ("" if use_templates else tpl.get("body"))
                or os.getenv(
                    "FIRST_OUTREACH_EMAIL_HTML",
                    (
                        "<p>Hi [Name],</p>"
                        "<p>Noticed your experience in [specialty] and wanted to reach out personally. Many clinicians with your background are using short contracts to increase income and still protect work–life balance.</p>"
                        "<p>Would you be open to a quick 5–10 minute call this week? If yes, reply with a good time, or share your availability date, time, and time zone (like EST) so we reach out accordingly.</p>"
                        "<p>Best,<br/>[Your Name]<br/>[Title], [Company]</p>"
                    ),
                )
            )

            # Prepare email content with template variables
            body_rendered = body_val
            body_rendered = body_rendered.replace(
                "{{name}}", 
                f"{c.get('FirstName','')} {c.get('LastName','')}".strip()
            )
            body_rendered = body_rendered.replace(
                "{{job_title}}", 
                str(c.get("JobTitle", ""))
            )
            # Also support {name} and {Role}
            name_val = f"{c.get('FirstName','')} {c.get('LastName','')}".strip()
            job_title_val = str(c.get("JobTitle") or "")
            role_val = role_from_frontend or job_title_val or os.getenv("DEFAULT_ROLE", "the role")
            body_rendered = body_rendered.replace("{name}", name_val).replace("{Role}", role_val)

            # Render placeholders in subject too
            subject_rendered = subject_val
            subject_rendered = subject_rendered.replace("{{name}}", name_val).replace("{{job_title}}", job_title_val)
            subject_rendered = subject_rendered.replace("{name}", name_val).replace("{Role}", role_val)

            # Prepare SendGrid email
            message = Mail(
                from_email=from_email,
                to_emails=to_email,
                subject=subject_rendered,
                html_content=HtmlContent(body_rendered)
            )

            # Route replies to the inbound parse address if configured
            if reply_to_email:
                try:
                    message.reply_to = Email(reply_to_email)
                except Exception:
                    # Fallback: do not block sending if reply_to is malformed
                    pass

            try:
                # Send email via SendGrid
                response = sg.send(message)
                
                # Log successful send
                db.add(
                    EmailLog(
                        candidate_id=str(c.get("_id")),
                        subject=subject_rendered,
                        body=body_rendered,
                        status="sent",
                        provider_message_id=response.headers.get('X-Message-Id', 'n/a'),
                    )
                )
                sent += 1

                # Follow-up tracker upsert for outbound email
                try:
                    now = datetime.utcnow()
                    tracker = (
                        db.query(OutreachTracker)
                        .filter(OutreachTracker.channel == "email", OutreachTracker.contact == to_email.lower())
                        .order_by(OutreachTracker.id.desc())
                        .first()
                    )
                    if not tracker:
                        tracker = OutreachTracker(
                            candidate_id=str(c.get("_id")) if c.get("_id") is not None else None,
                            contact=to_email.lower(),
                            channel="email",
                            first_contacted_at=now,
                            last_outreach_at=now,
                            followup_count=0,
                            next_followup_at=now + timedelta(hours=int(os.getenv("FOLLOWUP_HOURS", "12"))),
                            status="active",
                        )
                        db.add(tracker)
                    else:
                        tracker.last_outreach_at = now
                        if tracker.replied_at is None and tracker.status == "active":
                            tracker.next_followup_at = now + timedelta(hours=int(os.getenv("FOLLOWUP_HOURS", "12")))
                    db.commit()
                except Exception as t_ex:
                    logger.error(f"Failed to upsert email outreach tracker for {to_email}: {t_ex}", exc_info=True)
                
            except Exception as e:
                error_msg = str(e)
                if hasattr(e, 'body') and e.body:
                    try:
                        error_data = json.loads(e.body)
                        error_msg = error_data.get('errors', [{}])[0].get('message', error_msg)
                    except:
                        pass
                
                db.add(
                    EmailLog(
                        candidate_id=str(c.get("_id")),
                        subject=subject_rendered,
                        body=body_rendered,
                        status="failed",
                        provider_message_id="n/a",
                    )
                )
                errors.append({"id": c.get("_id"), "error": error_msg})
                
        db.commit()
    finally:
        db.close()

    return {"status": "sent" if not errors else "partial", "sent": sent, "errors": errors}


@app.post("/api/email-webhook")
async def handle_email_webhook(request: Request):
    """
    Webhook endpoint to handle incoming email replies from candidates.
    Extracts availability, schedules interviews, and sends confirmation.
    """
    logger.info("\n" + "="*80)
    logger.info("NEW EMAIL WEBHOOK RECEIVED")
    logger.info("="*80)
    
    try:
        # Log incoming request
        logger.info("1. Received webhook request")
        data = await request.body()
        logger.info(f"2. Raw request body: {data.decode()[:500]}...")
        
        try:
            data = json.loads(data)
            logger.info("3. Successfully parsed JSON data")
        except json.JSONDecodeError as je:
            logger.error(f"Failed to parse JSON data: {str(je)}")
            logger.error(f"Raw data: {data[:500]}")
            raise HTTPException(status_code=400, detail="Invalid JSON data")
            
        logger.debug(f"4. Parsed data: {json.dumps(data, indent=2)[:500]}...")
        
        # Extract email information (adjust based on your email provider's webhook format)
        logger.info("5. Extracting email information")
        email_data = data.get('email', {})
        
        # Try to get sender info from different possible locations
        candidate_email = (
            email_data.get('from', {}).get('email') or 
            data.get('from', {}).get('email') or
            data.get('sender', {}).get('email')
        )
        
        candidate_name = (
            email_data.get('from', {}).get('name') or 
            data.get('from', {}).get('name') or 
            'Candidate'
        )
        
        # Get email content from various possible fields
        email_content = (
            email_data.get('text') or 
            email_data.get('html', '') or 
            data.get('text', '') or 
            data.get('html', '') or
            data.get('body', '')
        )
        
        logger.info(f"6. Email from: {candidate_email} (Name: {candidate_name})")
        logger.debug(f"7. Email content (first 200 chars): {str(email_content)[:200]}...")
        
        if not candidate_email or not email_content:
            error_msg = "Missing required fields (candidate_email or email_content) in webhook payload"
            logger.error(f"{error_msg}. Full data: {json.dumps(data, indent=2)[:1000]}...")
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Get the interviewer's email from environment
        logger.info("8. Getting interviewer email from environment")
        interviewer_email = os.getenv("INTERVIEWER_EMAIL")
        if not interviewer_email:
            error_msg = "INTERVIEWER_EMAIL environment variable not set"
            logger.error(error_msg)
            raise HTTPException(status_code=500, detail=error_msg)
        
        # Process the email and schedule the interview
        logger.info("9. Processing email and extracting availability")
        try:
            result = email_processor.extract_availability(
                email_content=email_content,
                candidate_email=candidate_email,
                interviewer_email=interviewer_email
            )
            logger.info(f"10. Extracted availability result: {json.dumps(result, indent=2)}")
        except Exception as e:
            error_msg = f"Error in extract_availability: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": error_msg}
            )
        
        if result.get('status') == 'success':
            # Send confirmation email with meeting details
            try:
                # Format the meeting time
                start_time = datetime.fromisoformat(result['start_time'])
                formatted_time = start_time.strftime("%A, %B %d at %I:%M %p %Z")
                
                # Prepare email content
                subject = "Your Interview Has Been Scheduled"
                body = f"""
                <h2>Interview Confirmation</h2>
                <p>Hello {candidate_name},</p>
                <p>Your interview has been scheduled for:</p>
                <p><strong>Date & Time:</strong> {formatted_time}</p>
                <p><strong>Meeting Link:</strong> <a href="{result['meeting_link']}">Join Meeting</a></p>
                <p>We look forward to speaking with you!</p>
                <p>Best regards,<br>Interview Team</p>
                """
                
                logger.info(f"11. Preparing to send confirmation email to {candidate_email}")
                
                # In a real implementation, you would use your email service here
                # For example using smtplib or a service like SendGrid
                try:
                    # This is a placeholder - replace with your actual email sending code
                    logger.info(f"12. [MOCK] Sending email to {candidate_email}")
                    logger.info(f"13. [MOCK] Subject: {subject}")
                    logger.info(f"14. [MOCK] Body: {body[:200]}...")  # Log first 200 chars of body
                    
                    # Here you would add your actual email sending code
                    # For example, using smtplib:
                    # send_email(
                    #     to_email=candidate_email,
                    #     subject=subject,
                    #     html_content=body
                    # )
                    
                    logger.info("15. Email sent successfully")
                    
                    return JSONResponse(
                        status_code=200,
                        content={
                            "status": "success",
                            "message": "Interview scheduled and confirmation sent",
                            "scheduled": True,
                            "meeting_link": result['meeting_link'],
                            "event_id": result.get('event_id'),
                            "candidate_email": candidate_email,
                            "scheduled_time": formatted_time
                        }
                    )
                except Exception as e:
                    error_msg = f"Failed to send confirmation email: {str(e)}"
                    logger.error(error_msg, exc_info=True)
                    # Still return success for scheduling but indicate email failed
                    return JSONResponse(
                        status_code=200,
                        content={
                            "status": "partial_success",
                            "message": "Interview scheduled but failed to send confirmation email",
                            "scheduled": True,
                            "meeting_link": result['meeting_link'],
                            "event_id": result.get('event_id'),
                            "error": error_msg
                        }
                    )
                
            except Exception as e:
                error_msg = f"Error sending confirmation email: {str(e)}"
                logger.error(error_msg, exc_info=True)
                return JSONResponse(
                    status_code=200,  # Still return 200 to prevent webhook retries
                    content={
                        "status": "partial_success",
                        "message": "Interview scheduled but failed to send confirmation email",
                        "scheduled": True,
                        "error": error_msg,
                        "meeting_link": result.get('meeting_link', '')
                    }
                )
        else:
            # Handle error case
            error_msg = result.get('message', 'Failed to schedule interview')
            logger.error(f"Scheduling failed: {error_msg}")
            
            # Send error notification to admin
            try:
                admin_email = os.getenv("ADMIN_EMAIL", interviewer_email)
                error_subject = "Failed to Schedule Interview"
                error_body = f"""
                <p>Failed to schedule interview for candidate: {candidate_email}</p>
                <p>Error: {error_msg}</p>
                <p>Please schedule this interview manually.</p>
                """
                # Send error email (implement your email sending logic here)
                logger.info(f"Would send error notification to {admin_email}")
                
            except Exception as e:
                logger.error(f"Failed to send error notification: {str(e)}")
            
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": error_msg,
                    "scheduled": False,
                    "details": result.get('details', '')
                }
            )
        
    except Exception as e:
        logger.error(f"Error processing email webhook: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/schedule-interview")
async def schedule_interview(request: ScheduleRequest):
    """
    Schedule an interview with the candidate at the specified time.
    """
    try:
        # Initialize the Google Calendar scheduler
        scheduler = GoogleCalendarScheduler()
        
        # Schedule the interview
        result = scheduler.schedule_interview(request)
        
        # Check if there was an error
        if "error" in result:
            logger.error(f"Failed to schedule interview: {result['error']}")
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": result["error"],
                    "details": result.get("details", ""),
                    "scheduled": False
                }
            )
        
        # Return success response with meeting details
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": "Interview scheduled successfully",
                "scheduled": True,
                "candidate_email": request.candidate_email,
                "interviewer_email": request.interviewer_email,
                "scheduled_time": request.interview_slot.start_time.isoformat(),
                "meeting_link": result.get("meeting_link", ""),
                "event_id": result.get("event_id"),
                "html_link": result.get("html_link", ""),
                "timezone": request.interview_slot.timezone
            }
        )
        
    except Exception as e:
        logger.error(f"Error scheduling interview: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# Serve frontend files
@app.get("/")
async def serve_frontend():
    """Serve the main HTML file."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, headers={"Cache-Control": "no-store"})
    return {"message": "Frontend not found. Please build the frontend first."}

# Serve static files for the frontend
@app.get("/{file_path:path}")
async def serve_static(file_path: str):
    """Serve static files for the frontend."""
    # Allow fetching the candidate dataset as a plain JSON file (not via /api routes).
    # This keeps the frontend self-serve and enables loading from data.json.
    try:
        fp_norm = str(file_path or "").strip().lstrip("/")
        if fp_norm in {"data.json", "Applicant Data.json", "Applicant%20Data.json"}:
            data_file = (os.getenv("CANDIDATE_DATA_FILE") or "").strip() or None
            if not data_file:
                data_json = os.path.join(PROJECT_ROOT, "data.json")
                if os.path.exists(data_json):
                    data_file = data_json
            data_file = data_file or DATA_FILE
            if os.path.exists(data_file):
                return FileResponse(data_file, media_type="application/json", headers={"Cache-Control": "no-store"})
    except Exception:
        pass

    static_file = os.path.join(STATIC_DIR, file_path)
    if os.path.exists(static_file):
        return FileResponse(static_file, headers={"Cache-Control": "no-store"})
    # If file not found, try to serve index.html for SPA routing
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, headers={"Cache-Control": "no-store"})
    raise HTTPException(status_code=404, detail="File not found")
