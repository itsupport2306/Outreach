import os
import re
import hashlib
import logging
from typing import Dict, Optional
from twilio.rest import Client
from dateparser import parse
from datetime import datetime
import requests
import asyncio

logger = logging.getLogger(__name__)

class MessagingService:
    def __init__(self):
        """Initialize Twilio SMS; AI candidate conversations are disabled."""
        # Initialize Twilio client
        self.twilio_account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        self.twilio_auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        self.twilio_phone_number = os.getenv('TWILIO_PHONE_NUMBER')
        
        # Spreadsheet outreach only: candidate replies are forwarded by email,
        # never processed by Gemini or answered by an automated SMS.
        self.gemini = None
        
        # Initialize Twilio client if credentials are available
        if all([self.twilio_account_sid, self.twilio_auth_token, self.twilio_phone_number]):
            self.twilio_client = Client(self.twilio_account_sid, self.twilio_auth_token)
        else:
            logger.warning("Twilio credentials not found. SMS functionality will be disabled.")
            self.twilio_client = None
        
        # Conversation state tracking
        self.conversation_states: Dict[str, Dict] = {}

        # Reuse HTTP connection(s) for lower latency during TTS prewarm
        self._http = requests.Session()

        # Current job description used for this messaging context; can be updated
        # from the main app when a new JD is ranked from the frontend.
        self.job_description: str = os.getenv("DEFAULT_JOB_DESCRIPTION", "")
        
        # System prompt for Gemini
        self.system_prompt = """You are James Chandler, a healthcare recruiter at Radixsol.
        The candidate has already received an initial message about 
        scheduling a call for the role described in the job description. Your MAIN GOAL is to successfully schedule an interview call with the candidate.

        Identity rules (critical):
        - Never say you are an AI, a bot, an assistant, or a language model.
        - If asked who you are, you must say: "I’m James Chandler from Radixsol."
        - If asked how you work/what system you are, give a recruiter-appropriate answer (e.g., you’re reaching out from Radixsol) and pivot back to scheduling.
        
        Primary objective:
        - Collect the candidate's availability with THREE key pieces of information:
          (1) date, (2) time, and (3) timezone.
        - Do not consider the scheduling task complete until you have all three.
        
        Core behavior:
        - Always gently steer the conversation toward finding a concrete date, time, and timezone for the call.
        - The frontend provides you with a JOB DESCRIPTION for the specific role. This job description is your ONLY source of truth about the role, responsibilities, requirements, and benefits.
        - If the candidate asks about the role, position, responsibilities, requirements, salary, benefits, company, or anything related to the job, you MUST:
          1) Answer ONLY using information that is explicitly present or clearly implied in the provided job description.
          2) NEVER invent or guess details that are not supported by the job description.
          3) If the job description does NOT contain enough information to answer their question, clearly say that someone from our side will inform them.
             Use language like: "I'm not sure about that specific detail. Someone from our team will inform you about this."
          4) After answering (or saying a team member will follow up), you MUST always follow up with this exact scheduling question (or a very close paraphrase):
             "To schedule our call, could you please let me know your availability with a specific date, time, and timezone?"
             Keep the follow-up in the same message so the candidate always sees the role answer and the availability question together.
        - Keep responses short and SMS-friendly (1-2 sentences, occasionally 3 if needed).

        Timezone handling:
        - Never guess or invent a timezone.
        - If the candidate provides a date and time (e.g., "today at 3:00 PM") but does NOT clearly mention a timezone (e.g., IST, PST, EST),
        you MUST explicitly ask them to confirm their timezone instead of assuming a default.
        Example: "Thanks! Could you please confirm your timezone? (e.g., EST, PST, IST)"
        - If the candidate says they are available "now" or "right now" and you don't yet know their timezone, first ask for timezone and
          then acknowledge that you can call them now.

        Calendar invites:
        - Do NOT mention calendar invites, meeting links, Google Calendar, Outlook, or sending any invite.
        - Simply confirm the scheduled call time and that we will call them.

        Follow these steps:
        1. If the candidate provides their availability, confirm the details and ask for timezone if not provided.
        2. If they suggest a time, acknowledge it and confirm the timezone if it is missing or unclear.
        3. If their availability is unclear, ask specific questions to clarify (e.g., which day, what time, and which timezone).
        4. Once you have all details (date, time, timezone), provide a clear confirmation message.

        Guidelines:
        - Be friendly, professional, and helpful.
        - When answering questions about the role, keep the explanation concise and then pivot back to scheduling.
        - If you do not know the answer to a role-related question from the job description, explicitly say that someone from our side will inform the candidate and DO NOT make anything up.
        - Do NOT get pulled into long, off-topic conversations; always bring the focus back to scheduling the call.
        - Do NOT make up exact times on your own; use or refine the times provided by the candidate.
        - End with a confirmation message that includes the scheduled time, date, and timezone when a call is successfully scheduled.

        Example flows:
        Candidate: I'm available tomorrow at 2pm
        You: That works! Could you please confirm your timezone?
        
        Candidate: How about next week?
        You: Sure! Could you let me know which day and time next week works best for you, and your timezone?

        Candidate: Before we schedule, can you tell me more about the role?
        You: [Briefly describe the role strictly based on the job description.] Then add a follow-up like: "Regarding the call, when would be a good time for you to chat?"

        Candidate: Is there a relocation allowance?
        You: If the job description mentions relocation allowance, answer from it and then ask for availability.
             If the job description does NOT mention this, reply: "I'm not sure about that specific detail. Someone from our team will inform you about this. In the meantime, when would be a good time for you to chat?"
        """

    def update_job_description(self, job_description: str) -> None:
        """Update the current job description used in AI conversations."""
        if job_description and isinstance(job_description, str):
            self.job_description = job_description

    async def _extract_availability(self, message: str, state: Dict) -> Dict:
        """Extract and validate availability information from the message."""
        if 'availability' not in state:
            state['availability'] = {}

        message_lower = message.lower().strip()

        # Relative time scheduling ("in an hour", "in 30 minutes").
        # We preserve this intent in state so a subsequent timezone-only reply can
        # complete scheduling.
        rel_match = re.search(r"\bin\s+(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b", message_lower)
        rel_hour_match = None
        if not rel_match:
            rel_hour_match = re.search(r"\bin\s+an\s+hour\b", message_lower)
        if rel_match or rel_hour_match:
            try:
                if rel_hour_match:
                    delta_minutes = 60
                else:
                    qty = int(rel_match.group(1))
                    unit = (rel_match.group(2) or "").lower()
                    if unit.startswith("hour") or unit in {"hr", "hrs"}:
                        delta_minutes = qty * 60
                    else:
                        delta_minutes = qty
                delta_minutes = max(1, min(int(delta_minutes), 24 * 60))
            except Exception:
                delta_minutes = 60

            # Store pending relative schedule intent in state (separate from availability).
            # Also clear any previously extracted absolute availability so reschedules
            # don't accidentally reuse the old date/time.
            try:
                if 'availability' not in state or not isinstance(state.get('availability'), dict):
                    state['availability'] = {}
                state['availability'].pop('time', None)
                state['availability'].pop('date', None)
            except Exception:
                pass
            state['pending_relative_schedule'] = {
                'delta_minutes': int(delta_minutes),
                'raw_message': message,
            }
            state['availability']['raw_message'] = message
            return state

        # If the user replies with only AM/PM (or a short phrase containing it)
        # and we already captured a time without AM/PM, apply it to the existing
        # time instead of treating it as a standalone message.
        ampm_match = re.search(r"\b(am|pm)\b", message_lower)
        if ampm_match:
            existing_time = (state.get('availability', {}).get('time') or '').strip().lower()
            if existing_time and ('am' not in existing_time) and ('pm' not in existing_time):
                if not re.search(r"\d", message_lower) and len(message_lower) <= 20:
                    state['availability']['time'] = f"{existing_time} {ampm_match.group(1)}"
                    state['availability']['raw_message'] = message
                    return state

        def _has_explicit_date_signal(txt: str) -> bool:
            txt = (txt or "").lower()
            if any(w in txt for w in [
                "today", "tomorrow", "tonight",
                "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june",
                "jul", "july", "aug", "august", "sep", "sept", "september", "oct", "october", "nov", "november",
                "dec", "december",
            ]):
                return True
            # Numeric date patterns like 10/02, 2026-02-10, 10-02-2026
            if re.search(r"\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b", txt):
                return True
            if re.search(r"\b\d{4}-\d{2}-\d{2}\b", txt):
                return True
            return False

        explicit_date_signal = _has_explicit_date_signal(message_lower)

        # If the user replies with only a day word (e.g., "today" / "tomorrow") and
        # we already have a time (and maybe timezone) from earlier messages, treat
        # this as providing the missing date, not a new request for time.
        if message_lower in ["today", "tomorrow"]:
            if state.get('availability', {}).get('time') and not state.get('availability', {}).get('date'):
                from datetime import datetime, timedelta
                import pytz

                tz_str = (state.get('availability', {}).get('timezone') or 'UTC').strip().upper()
                tz_mapping = {
                    'EST': 'US/Eastern', 'EDT': 'US/Eastern',
                    'PST': 'US/Pacific', 'PDT': 'US/Pacific',
                    'CST': 'US/Central', 'CDT': 'US/Central',
                    'MST': 'US/Mountain', 'MDT': 'US/Mountain',
                    'UTC': 'UTC', 'GMT': 'GMT', 'IST': 'Asia/Kolkata'
                }

                tz_name = tz_mapping.get(tz_str, 'UTC')
                try:
                    tz = pytz.timezone(tz_name)
                except Exception:
                    tz = pytz.timezone('UTC')

                now_local = datetime.now(tz)
                if message_lower == "tomorrow":
                    target_date = (now_local + timedelta(days=1)).date()
                else:
                    target_date = now_local.date()

                state['availability']['date'] = target_date.strftime('%Y-%m-%d')
                state['availability']['raw_message'] = message
                return state

        # If the user replies with only a timezone (e.g., "IST"), do NOT let
        # dateparser infer a date/time. Store the timezone and ask for date/time.
        tz_only_match = re.fullmatch(r"(est|edt|pst|pdt|cst|cdt|mst|mdt|utc|gmt|ist)", message_lower)
        if tz_only_match:
            state['availability']['timezone'] = tz_only_match.group(1).upper()
            state['availability']['raw_message'] = message
            return state

        # Handle immediate-call style messages explicitly. Be conservative so we
        # only start a call on very clear instructions from the candidate.
        # NOTE: Do NOT match the substring "now" because it appears inside words
        # like "know" (e.g., "I want to know about pay scale").
        immediate_call_patterns = [
            r"\bcall\s+me\s+(right\s+)?now\b",
            r"\bcall\s+now\b",
            r"\bstart\s+(the\s+)?call\s+(right\s+)?now\b",
            r"\bcan\s+you\s+call\s+(me\s+)?(right\s+)?now\b",
            r"\bconnect\s+(right\s+)?now\b",
            r"\bconnect\s+me\s+(right\s+)?now\b",
            r"\bcall\s+me\s+asap\b",
            r"\bcall\s+me\s+immediately\b",
            r"\bcall\s+me\s+right\s+away\b",
            r"\bphone\s+me\s+(right\s+)?now\b",
            r"\bring\s+me\s+(right\s+)?now\b",
            r"\bgive\s+me\s+a\s+call\s+(right\s+)?now\b",
            r"\bcan\s+we\s+talk\s+(right\s+)?now\b",
            r"\bcan\s+we\s+speak\s+(right\s+)?now\b",
            r"\b(i\s*am\s+)?available\s+(right\s+)?now\b",
            r"\b(i\s*am\s+)?free\s+(right\s+)?now\b",
            r"\b(i\s*am\s+)?ready\s+(right\s+)?now\b",
            r"\b(i\s*am\s+)?available\s+(right\s+)?now\s+(to\s+)?(talk|speak)\b",
            r"\b(i\s*am\s+)?free\s+(right\s+)?now\s+(to\s+)?(talk|speak)\b",
            r"\b(i\s*am\s+)?ready\s+(right\s+)?now\s+(to\s+)?(talk|speak)\b",
        ]
        if any(re.search(p, message_lower) for p in immediate_call_patterns):
            state['call_immediately'] = True
            # Still record what the user said for context
            state['availability']['raw_message'] = message
            return state

        # Try to parse common time patterns first (e.g., "6:30 PM UTC" or "12.45 PM EST")
        time_pattern = r'(\d{1,2}[:.]?\d{0,2}\s*[ap]\.?m\.?|\d{1,2}[:.]?\d{2})\s*([a-z]+)?(?:\s+([a-z]+(?:\s+[a-z]+)*))?'
        match = re.search(time_pattern, message_lower, re.IGNORECASE)
        
        if match:
            time_part = match.group(1).strip().upper()
            # Extract AM/PM from the time part if present
            ampm_part = ''
            if 'A' in time_part.upper() or 'P' in time_part.upper():
                ampm_part = 'AM' if 'A' in time_part.upper() else 'PM'
                # Remove AM/PM from time_part for further processing
                time_part = re.sub(r'[AP]\.?M?\.?', '', time_part).strip()
            
            # Normalize dots to colons and clean up the time string
            time_part = time_part.replace('.', ':')
            time_str = re.sub(r'[^0-9:]', '', time_part)
            
            # Handle compact times (e.g., 945 -> 9:45) and add minutes if missing
            if ':' not in time_str and len(time_str) > 2:
                time_str = f"{time_str[:-2]}:{time_str[-2:]}"
            elif ':' not in time_str:
                time_str = f"{time_str}:00"
                
            # Add AM/PM back if it was present in the original
            if ampm_part:
                time_str = f"{time_str} {ampm_part}"

            # Normalize to a valid 12-hour clock representation: '00' hour is not
            # valid for '%I', so convert '00:MM' to '12:MM'.
            parts = time_str.split()
            if parts:
                hm = parts[0]
                suffix = ' '.join(parts[1:])
                if ':' in hm:
                    hour_str, minute_str = hm.split(':', 1)
                    if hour_str == '00':
                        hour_str = '12'
                        hm = f"{hour_str}:{minute_str}"
                        time_str = (hm + (' ' + suffix if suffix else '')).strip()
            
            # Clean up time string (remove double spaces, etc.)
            time_str = ' '.join(time_str.split())
            
            # Extract timezone if present
            tz_part = match.group(2) or match.group(3) or ''
            tz_mapping = {
                'est': 'EST', 'pst': 'PST', 'cst': 'CST', 'mst': 'MST',
                'edt': 'EDT', 'pdt': 'PDT', 'cdt': 'CDT', 'mdt': 'MDT',
                'utc': 'UTC', 'gmt': 'GMT', 'ist': 'IST'
            }
            
            tz_found = None
            for tz_key, tz_name in tz_mapping.items():
                if tz_key in tz_part.lower():
                    tz_found = tz_name
                    break
            
            if tz_found:
                state['availability']['timezone'] = tz_found
            
            state['availability']['time'] = time_str
            
            # IMPORTANT: do not implicitly assume a date when the candidate only
            # provides a time/timezone. Only set a date when there's an explicit
            # date signal in the message (today/tomorrow/day name/explicit date).
            if explicit_date_signal:
                if 'date' not in state['availability']:
                    from datetime import datetime, timedelta, timezone
                    import pytz
                    
                    # Get the candidate's timezone, default to UTC if not specified
                    tz_str = state['availability'].get('timezone', 'UTC')
                    
                    # Map timezone abbreviations to pytz timezones
                    tz_mapping = {
                        'EST': 'US/Eastern', 'EDT': 'US/Eastern',
                        'PST': 'US/Pacific', 'PDT': 'US/Pacific',
                        'CST': 'US/Central', 'CDT': 'US/Central',
                        'MST': 'US/Mountain', 'MDT': 'US/Mountain',
                        'UTC': 'UTC', 'GMT': 'GMT', 'IST': 'Asia/Kolkata'
                    }
                    
                    try:
                        tz_name = tz_mapping.get(tz_str.upper(), 'UTC')
                        candidate_tz = pytz.timezone(tz_name)
                        now_candidate = datetime.now(candidate_tz)

                        if 'tomorrow' in message_lower:
                            target_date = (now_candidate + timedelta(days=1)).date()
                            logger.info(f"Message contains 'tomorrow'; using date {target_date} in {tz_name}")
                        elif 'today' in message_lower:
                            target_date = now_candidate.date()
                            logger.info(f"Message contains 'today'; using date {target_date} in {tz_name}")
                        else:
                            # For explicit numeric dates/day names, dateparser fallback below
                            target_date = None

                        if target_date is not None:
                            parsed_dt = parse(time_str, settings={
                                "PREFER_DATES_FROM": "future",
                                "TIMEZONE": tz_name,
                                "TO_TIMEZONE": tz_name
                            })
                            if parsed_dt:
                                local_time = parsed_dt.astimezone(candidate_tz).time()
                                combined = datetime.combine(target_date, local_time)
                                now_candidate_naive = now_candidate.replace(tzinfo=None)
                                if combined < now_candidate_naive and 'today' not in message_lower:
                                    combined = combined + timedelta(days=1)
                                    logger.info(
                                        f"Combined datetime {target_date} {local_time} in {tz_name} is past; "
                                        "bumping to next day."
                                    )
                                state['availability']['date'] = combined.strftime('%Y-%m-%d')
                            else:
                                state['availability']['date'] = target_date.strftime('%Y-%m-%d')
                                logger.warning(
                                    f"Could not parse time '{time_str}', defaulting to {state['availability']['date']} in {tz_name}"
                                )
                    except Exception as e:
                        now_utc = datetime.now(timezone.utc)
                        state['availability']['date'] = now_utc.strftime('%Y-%m-%d')
                        logger.warning(f"Error processing timezone '{tz_str}': {e}, defaulting to UTC")
        
        # Fall back to dateparser for more complex date/time parsing, but ONLY if
        # the message actually contains date/time signals. This avoids cases like
        # "IST" being parsed as the current time.
        if ('time' not in state['availability'] or (('date' not in state['availability']) and explicit_date_signal)):
            has_time_signal = bool(re.search(r"\d", message_lower)) or any(
                kw in message_lower for kw in [
                    'today', 'tomorrow', 'tonight', 'am', 'pm', 'morning', 'afternoon',
                    'evening', 'monday', 'tuesday', 'wednesday', 'thursday',
                    'friday', 'saturday', 'sunday'
                ]
            )
            if has_time_signal:
                parsed_date = parse(message)
                if parsed_date:
                    # Store the parsed date and time
                    time_str = parsed_date.strftime('%I:%M %p').lstrip('0').lower()
                    state['availability']['time'] = time_str
                    if explicit_date_signal:
                        state['availability']['date'] = parsed_date.strftime('%Y-%m-%d')
                    # Only set timezone if an explicit timezone was parsed
                    tzname = parsed_date.tzname()
                    if tzname and 'timezone' not in state['availability']:
                        state['availability']['timezone'] = tzname

        # Store the raw message as well
        state['availability']['raw_message'] = message
        return state

    async def _generate_confirmation(self, state: Dict, phone_number: str) -> str:
        """Generate a confirmation message and schedule the interview call."""
        availability = state.get('availability', {})

        # Check if we have all required information
        time = availability.get('time')
        date = availability.get('date')
        timezone = availability.get('timezone')

        # If time/date are missing but we have the raw availability message,
        # try one more time to parse it before giving up.
        if not all([time, date]):
            raw_msg = availability.get('raw_message', '')
            if raw_msg:
                parsed = parse(raw_msg)
                if parsed:
                    # Use Windows-compatible formatting
                    time_str = parsed.strftime('%I:%M %p').lstrip('0').lower()
                    availability['time'] = time_str
                    availability['date'] = parsed.strftime('%Y-%m-%d')
                    time = availability['time']
                    date = availability['date']

        if not all([time, date]):
            logger.warning(
                f"Missing time/date in availability for {phone_number}: "
                f"time={time}, date={date}, raw={availability.get('raw_message')}"
            )
            return (
                "Please reply with your availability including a date, time, and timezone. "
                "Example: 'tomorrow 9:30 PM IST' or '2026-01-15 2:00 PM EST'."
            )

        if not timezone:
            logger.info(
                f"Missing timezone in availability for {phone_number}: "
                f"time={time}, date={date}, raw={availability.get('raw_message')}"
            )
            return (
                "Thanks! Please confirm your timezone (e.g., IST, EST, PST). "
                "Example: 'IST'."
            )

        # Normalize phone number to a consistent E.164-like format for DB lookups and storage
        normalized_phone = phone_number.strip()
        if normalized_phone.startswith("00"):
            normalized_phone = "+" + normalized_phone[2:]
        elif not normalized_phone.startswith("+"):
            normalized_phone = "+" + normalized_phone

        # Before scheduling, check if there is already a future interview for this phone
        try:
            from .models import InterviewSchedule, SessionLocal
            from datetime import datetime
            import pytz

            db = SessionLocal()
            try:
                existing = (
                    db.query(InterviewSchedule)
                    .filter(
                        InterviewSchedule.candidate_phone == normalized_phone,
                        InterviewSchedule.status.in_(["scheduled", "in_progress"]),
                    )
                    .order_by(InterviewSchedule.scheduled_datetime.asc())
                    .first()
                )

                # If we found an interview, make sure it is still in the future when
                # interpreted in its own timezone. If it is already in the past, ignore
                # it so the candidate can schedule a new one.
                if existing and existing.scheduled_datetime is not None:
                    try:
                        tz_label = (existing.timezone or "UTC").upper()
                        tz_abbr_map = {
                            "PST": "America/Los_Angeles",
                            "PDT": "America/Los_Angeles",
                            "MST": "America/Denver",
                            "MDT": "America/Denver",
                            "CST": "America/Chicago",
                            "CDT": "America/Chicago",
                            "EST": "America/New_York",
                            "EDT": "America/New_York",
                            "IST": "Asia/Kolkata",
                            "GMT": "GMT",
                            "UTC": "UTC",
                        }

                        tz_name = tz_abbr_map.get(tz_label, tz_label)
                        try:
                            tzinfo = pytz.timezone(tz_name)
                        except pytz.UnknownTimeZoneError:
                            tzinfo = pytz.timezone("UTC")

                        now_local = datetime.utcnow().replace(tzinfo=pytz.utc).astimezone(tzinfo)
                        now_local_naive = now_local.replace(tzinfo=None)

                        # existing.scheduled_datetime is stored as naive local time
                        if existing.scheduled_datetime < now_local_naive:
                            existing = None
                    except Exception as _tz_ex:
                        logger.warning(
                            f"Failed to compare existing interview time for {normalized_phone}: {_tz_ex}"
                        )

                # If we already have a scheduled interview and we're not in reschedule mode,
                # inform the candidate and ask if they want to reschedule.
                if existing and not state.get("reschedule_mode"):
                    scheduled_str = existing.scheduled_datetime.strftime("%Y-%m-%d %I:%M %p")
                    tz_label = existing.timezone or "UTC"

                    state["reschedule_mode"] = True

                    return (
                        f"You already have an interview scheduled on {scheduled_str} {tz_label}. "
                        "If you want to reschedule, please reply with a new date, time, and timezone."
                    )

                # If we are in reschedule mode, cancel all active interviews for this
                # phone so the new schedule can replace them. This mirrors the email
                # reschedule behavior and ensures the central guard will allow the
                # new time.
                if state.get("reschedule_mode"):
                    try:
                        existing_all = (
                            db.query(InterviewSchedule)
                            .filter(
                                InterviewSchedule.candidate_phone == normalized_phone,
                                InterviewSchedule.status.in_(["scheduled", "in_progress"]),
                            )
                            .order_by(InterviewSchedule.scheduled_datetime.asc())
                            .all()
                        )

                        for row in existing_all:
                            if row.scheduled_datetime is None:
                                continue

                            try:
                                # Mark the interview as cancelled. The call_pickup column is a
                                # short String(3) (e.g. 'Yes'/'No'), so avoid writing long
                                # strings like 'Cancelled' which cause MySQL DataError.
                                row.status = "cancelled"
                                if hasattr(row, "call_pickup"):
                                    # Either leave it as-is or set a short value like 'No'.
                                    row.call_pickup = "No"
                                logger.info(
                                    "SMS reschedule: cancelled existing interview for %s at %s (%s).",
                                    normalized_phone,
                                    row.scheduled_datetime,
                                    row.timezone or "UTC",
                                )
                            except Exception as _row_ex:
                                logger.warning(
                                    f"SMS reschedule: failed to cancel existing interview row for {normalized_phone}: {_row_ex}"
                                )

                        db.commit()
                    except Exception as _cancel_ex:
                        logger.error(
                            f"SMS reschedule: error while cancelling existing interviews for {normalized_phone}: {_cancel_ex}"
                        )

            finally:
                db.close()

        except Exception as check_ex:
            logger.error(
                f"Error while checking existing interview schedule for {normalized_phone}: {check_ex}"
            )

        # Try to schedule the interview
        try:
            logger.info(
                f"Attempting to schedule interview call for {normalized_phone} on {date} at {time} {timezone}"
            )
            scheduled = await self.schedule_interview_call(normalized_phone, date, time, timezone)

            if scheduled:
                return (
                    f"Great! I've scheduled your interview for {date} at {time} {timezone}. "
                    "We'll call you at that time. Looking forward to speaking with you!"
                )
            else:
                # Do not claim the interview was scheduled if schedule_interview_call
                # returned False. Clearly inform the candidate and ask for a new
                # future time slot.
                return (
                    "I couldn't schedule your interview at that time. It may already be in the past "
                    "or you might already have an interview booked. Please reply with a new future "
                    "date, time, and timezone (for example: 'tomorrow at 9:30 PM IST')."
                )
        except Exception as e:
            logger.error(f"Error in _generate_confirmation: {str(e)}")
            return "I'm sorry, I encountered an error while scheduling your interview. Please try again with a specific date and time."

    async def process_message(self, from_number: str, message: str) -> str:
        """Disabled legacy AI conversation; never generate an SMS response."""
        logger.info("Automated AI SMS conversation is disabled; no reply generated for %s", from_number)
        return ""

        # Legacy conversation flow is unreachable in spreadsheet-only mode.
        logger.info(f"Processing message from {from_number}: {message}")
        
        # Get or initialize conversation state
        if from_number not in self.conversation_states:
            self.conversation_states[from_number] = {
                'history': [
                    {
                        'role': 'assistant', 
                        'content': "Hi! We'd like to schedule a call to discuss the role. When are you available (date, time, and timezone)?"
                    }
                ],
                'stage': 'awaiting_availability',
                'data': {}
            }
        
        state = self.conversation_states[from_number]
        
        # Update conversation history with user's message
        state['history'].append({'role': 'user', 'content': message})

        # If the candidate previously indicated they want an immediate call but we
        # asked for timezone first, trigger the call as soon as they reply with a
        # timezone (or a simple confirmation).
        message_lower_global = (message or "").lower().strip()
        if state.get('pending_immediate_call'):
            tz_only_match = re.fullmatch(r"(est|edt|pst|pdt|cst|cdt|mst|mdt|utc|gmt|ist)", message_lower_global)
            yes_match = re.fullmatch(r"(yes|y|yeah|yep|ok|okay|sure)", message_lower_global)
            if tz_only_match or yes_match:
                try:
                    scheduled = await self.initiate_interview_call(from_number)
                    state['pending_immediate_call'] = False
                    if scheduled:
                        return "Great! I’m calling you right away. Please be ready to answer."
                    return (
                        "I tried to start the call right now but ran into an issue. "
                        "Please try again in a moment or share a specific future time (date, time, timezone)."
                    )
                except Exception as call_err:
                    state['pending_immediate_call'] = False
                    logger.error(f"Error initiating pending immediate call to {from_number}: {call_err}")
                    return (
                        "I tried to start the call right now but hit an error. "
                        "Please try again shortly or share a specific future time (date, time, timezone)."
                    )

        # Global immediate-call intent handling. Do this before any stage logic so
        # a clear "call me now" request never falls through to Gemini and turns
        # into a suggested (possibly past) schedule time.
        immediate_call_patterns = [
            r"\bcall\s+(me\s+)?(right\s+)?now\b",
            r"\bcan\s+you\s+call\s+(me\s+)?(right\s+)?now\b",
            r"\bcall\s+me\s+asap\b",
            r"\bcall\s+me\s+immediately\b",
            r"\bcall\s+me\s+right\s+away\b",
            r"\bconnect\s+(right\s+)?now\b",
            r"\bconnect\s+me\s+(right\s+)?now\b",
            r"\bphone\s+me\s+(right\s+)?now\b",
            r"\bring\s+me\s+(right\s+)?now\b",
            r"\bgive\s+me\s+a\s+call\s+(right\s+)?now\b",
            r"\bcan\s+we\s+talk\s+(right\s+)?now\b",
            r"\bcan\s+we\s+speak\s+(right\s+)?now\b",
            r"\b(i\s*am\s+)?available\s+(right\s+)?now\b",
            r"\b(i\s*am\s+)?free\s+(right\s+)?now\b",
            r"\b(i\s*am\s+)?ready\s+(right\s+)?now\b",
            r"\b(i\s*am\s+)?available\s+(right\s+)?now\s+(to\s+)?(talk|speak)\b",
            r"\b(i\s*am\s+)?free\s+(right\s+)?now\s+(to\s+)?(talk|speak)\b",
            r"\b(i\s*am\s+)?ready\s+(right\s+)?now\s+(to\s+)?(talk|speak)\b",
        ]
        if any(re.search(p, message_lower_global) for p in immediate_call_patterns):
            try:
                scheduled = await self.initiate_interview_call(from_number)
                if scheduled:
                    return "Great! I’ll call you right away. Please be ready to answer."
                return (
                    "I tried to start the call right now but ran into an issue. "
                    "Please try again in a moment or share a specific future time (date, time, timezone)."
                )
            except Exception as call_err:
                logger.error(f"Error initiating immediate call to {from_number}: {call_err}")
                return (
                    "I tried to start the call right now but hit an error. "
                    "Please try again shortly or share a specific future time (date, time, timezone)."
                )
        
        try:
            # Enhanced availability detection with more time-related keywords
            availability_keywords = [
                'morning', 'afternoon', 'evening', 'night', 'o\'clock',
                'today', 'tomorrow', 'tonight', 'at', 'on', 'available',
                'minute', 'minutes', 'min', 'mins', 'hour', 'hours', 'hr', 'hrs',
                'january', 'february', 'march', 'april', 'may', 'june', 'july',
                'august', 'september', 'october', 'november', 'december',
                'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'
            ]
            
            # Check if this looks like an availability message
            msg_l = message.lower()
            is_availability_msg = any(word in msg_l for word in availability_keywords) or bool(re.search(r"\d", msg_l))
            
            # If we're in confirmation stage but get what looks like a new availability,
            # reset to scheduling flow to process the new availability
            if is_availability_msg and state.get('stage') == 'confirmation':
                logger.info(f"Detected new availability after confirmation from {from_number}; resetting to scheduling flow")
                state['stage'] = 'awaiting_availability'
                state['availability'] = {}  # Clear previous availability
                
            # If we detect availability and we're not already in the scheduling flow,
            # reset to the beginning of the scheduling flow
            if is_availability_msg and state.get('stage') not in ['awaiting_availability', 'awaiting_timezone']:
                logger.info(
                    f"Detected availability-style message for {from_number}; "
                    f"resetting stage from {state.get('stage')} to 'awaiting_availability'"
                )
                state['stage'] = 'awaiting_availability'

            # If we're in the scheduling flow, try to extract availability
            if state['stage'] == 'awaiting_availability':
                # If the candidate explicitly mentions rescheduling, enable
                # reschedule_mode so that any existing future interview can be
                # cancelled before creating a new one in _generate_confirmation.
                msg_lower = message.lower()
                if 'reschedule' in msg_lower or 'rescheduling' in msg_lower:
                    state['reschedule_mode'] = True

                state = await self._extract_availability(message, state)

                # If the user asked for a relative call time ("in an hour"), keep that
                # intent and drive the stage machine. If timezone isn't known yet,
                # ask for it. If timezone is known, compute an absolute time and
                # proceed to confirmation.
                pending_rel = state.get('pending_relative_schedule')
                if pending_rel:
                    tz_existing = (state.get('availability', {}).get('timezone') or '').strip().upper()
                    if not tz_existing:
                        state['stage'] = 'awaiting_timezone'
                        return "Thanks! Could you please confirm your timezone so I can schedule that for you?"
                    try:
                        import pytz
                        from datetime import timedelta

                        tz_mapping = {
                            'EST': 'US/Eastern', 'EDT': 'US/Eastern',
                            'PST': 'US/Pacific', 'PDT': 'US/Pacific',
                            'CST': 'US/Central', 'CDT': 'US/Central',
                            'MST': 'US/Mountain', 'MDT': 'US/Mountain',
                            'UTC': 'UTC', 'GMT': 'GMT', 'IST': 'Asia/Kolkata'
                        }
                        tz_name = tz_mapping.get(tz_existing, 'UTC')
                        try:
                            tz = pytz.timezone(tz_name)
                        except Exception:
                            tz = pytz.timezone('UTC')

                        now_local = datetime.now(tz)
                        delta_minutes = int(pending_rel.get('delta_minutes') or 60)
                        target = now_local + timedelta(minutes=max(1, min(delta_minutes, 24 * 60)))

                        state.setdefault('availability', {})
                        state['availability']['date'] = target.strftime('%Y-%m-%d')
                        state['availability']['time'] = target.strftime('%I:%M %p').lstrip('0').lower()
                        state['availability']['timezone'] = tz_existing
                        state['pending_relative_schedule'] = None
                        state['stage'] = 'confirmation'
                        return await self._generate_confirmation(state, from_number)
                    except Exception:
                        # If anything goes wrong, fall back to asking for explicit availability.
                        state['pending_relative_schedule'] = None
                        return (
                            "Thanks! Please share a specific date, time, and timezone for the call. "
                            "Example: 'today 9:30 PM IST'."
                        )

                # Immediate-call path (e.g., user explicitly says "call me now")
                if state.get('call_immediately'):
                    try:
                        scheduled = await self.initiate_interview_call(from_number)
                        # Clear the flag so it doesn't repeatedly trigger on
                        # subsequent messages from the same candidate.
                        state['call_immediately'] = False
                        if scheduled:
                            return "Great! I'll call you right away. Please be ready to answer."
                        else:
                            return (
                                "I tried to start the call right now but ran into an issue. "
                                "Please try again in a moment or share a specific time.")
                    except Exception as call_err:
                        logger.error(f"Error initiating immediate call to {from_number}: {call_err}")
                        state['call_immediately'] = False
                        return (
                            "I tried to start the call right now but hit an error. "
                            "Please try again shortly or share a specific time.")

                # Check if we can extract time/date from the message. If the message
                # looks like availability but we don't yet have a timezone, decide
                # whether we are missing just the timezone or both time and timezone.
                message_lower = message.lower().strip()
                has_time_or_date_signal = (
                    bool(re.search(r"\d", message_lower))
                    or bool(re.search(r"\b(am|pm)\b", message_lower) and re.search(r"\d", message_lower))
                    or any(day in message_lower for day in [
                        'tomorrow', 'today',
                        'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'
                    ])
                )

                if has_time_or_date_signal:
                    availability_state = state.get('availability', {})
                    has_time = 'time' in availability_state and availability_state.get('time')
                    has_timezone = 'timezone' in availability_state and availability_state.get('timezone')
                    has_date = 'date' in availability_state and availability_state.get('date')
                    time_value = (availability_state.get('time') or '').strip().lower()
                    time_has_ampm = ('am' in time_value) or ('pm' in time_value)

                    # If the user replied with just 'today' or 'tomorrow', ask only for
                    # missing pieces (do not forget earlier time/timezone).
                    if message_lower in ['today', 'tomorrow']:
                        day_word = message_lower

                        if has_time and has_timezone:
                            # _extract_availability will set the missing date when it sees
                            # a day-word reply with an existing time.
                            if all(k in state.get('availability', {}) for k in ['time', 'date', 'timezone']):
                                state['stage'] = 'confirmation'
                                return await self._generate_confirmation(state, from_number)
                            return (
                                "Thanks! Just to confirm—what date should we schedule it for? "
                                "You can reply 'today' or 'tomorrow'."
                            )

                        if has_time and not has_timezone:
                            state['stage'] = 'awaiting_timezone'
                            return (
                                "Thanks! Could you please confirm your timezone? "
                                "Example: 'EST', 'PST', or 'IST'."
                            )

                        # No time yet
                        return (
                            "Thanks! Could you please provide the exact time and your timezone "
                            f"for {day_word}? For example: '{day_word} at 9:30 PM IST'."
                        )

                    # If we have a time but no timezone, ensure the time is complete.
                    # If AM/PM is missing (e.g., "10:20"), ask for AM/PM + timezone.
                    if has_time and not has_timezone:
                        if not time_has_ampm:
                            state['stage'] = 'awaiting_timezone'
                            return (
                                "Thanks! Please confirm whether that time is AM or PM and your timezone. "
                                "Example: '10:20 PM IST'."
                            )
                        state['stage'] = 'awaiting_timezone'
                        return "Thanks! Could you please confirm your timezone? (e.g., EST, PST, IST)"

                    # If we have a time (and maybe timezone) but no explicit date,
                    # do NOT assume today. Ask for the date.
                    if has_time and not has_date:
                        return (
                            "Thanks! Which date should we schedule the call for? "
                            "You can reply with 'today', 'tomorrow', a weekday, or a date like '10/02'."
                        )

                    # If we don't even have a time yet (e.g., complex message containing
                    # 'today' or 'tomorrow' but we couldn't parse a time), ask explicitly
                    # for both time and timezone.
                    if not has_time:
                        day_word = 'tomorrow' if 'tomorrow' in message_lower else 'today'
                        return (
                            "Thanks! Could you please provide the exact time and your timezone "
                            f"for {day_word}? For example: '{day_word} at 9:30 PM IST'."
                        )

                # If we have all required info, generate confirmation
                if all(k in state.get('availability', {}) for k in ['time', 'date', 'timezone']):
                    state['stage'] = 'confirmation'
                    return await self._generate_confirmation(state, from_number)
            
            # If we're waiting for timezone and get a response
            elif state['stage'] == 'awaiting_timezone':
                state = await self._extract_availability(message, state)

                availability = state.get('availability', {})
                timezone = (availability.get('timezone') or '').strip().upper()

                if not timezone:
                    m = re.search(r"\b(UTC|GMT|IST|EST|EDT|CST|CDT|MST|MDT|PST|PDT)\b", message, re.IGNORECASE)
                    if m:
                        timezone = m.group(1).upper()

                if not timezone:
                    m = re.search(r"\b(?:UTC|GMT)\s*([+-]\d{1,2})(?::?(\d{2}))?\b", message, re.IGNORECASE)
                    if m:
                        hour = int(m.group(1))
                        minute = int(m.group(2) or '0')
                        sign = '+' if hour >= 0 else '-'
                        timezone = f"GMT{sign}{abs(hour):02d}:{minute:02d}"

                if not timezone:
                    m = re.search(r"\b([+-]\d{1,2})(?::(\d{2}))\b", message)
                    if m:
                        hour = int(m.group(1))
                        minute = int(m.group(2) or '0')
                        sign = '+' if hour >= 0 else '-'
                        timezone = f"GMT{sign}{abs(hour):02d}:{minute:02d}"

                if not timezone:
                    return "I couldn't recognize your timezone. Please reply with something like 'IST', 'EST', or 'GMT+05:30'."

                if 'availability' not in state:
                    state['availability'] = {}
                state['availability']['timezone'] = timezone

                # If we have a pending relative schedule intent (e.g. "in an hour"),
                # compute the concrete date/time now that timezone is known.
                pending_rel = state.get('pending_relative_schedule')
                if pending_rel:
                    try:
                        import pytz
                        from datetime import timedelta

                        tz_str = (timezone or 'UTC').strip().upper()
                        tz_mapping = {
                            'EST': 'US/Eastern', 'EDT': 'US/Eastern',
                            'PST': 'US/Pacific', 'PDT': 'US/Pacific',
                            'CST': 'US/Central', 'CDT': 'US/Central',
                            'MST': 'US/Mountain', 'MDT': 'US/Mountain',
                            'UTC': 'UTC', 'GMT': 'GMT', 'IST': 'Asia/Kolkata'
                        }
                        tz_name = tz_mapping.get(tz_str, 'UTC')
                        try:
                            tz = pytz.timezone(tz_name)
                        except Exception:
                            tz = pytz.timezone('UTC')

                        now_local = datetime.now(tz)
                        delta_minutes = int(pending_rel.get('delta_minutes') or 60)
                        target = now_local + timedelta(minutes=max(1, min(delta_minutes, 24 * 60)))

                        # Overwrite any prior absolute availability (e.g., an older scheduled time)
                        # because relative-time intent always means "from now".
                        state['availability']['date'] = target.strftime('%Y-%m-%d')
                        state['availability']['time'] = target.strftime('%I:%M %p').lstrip('0').lower()
                        # Clear pending intent once applied
                        state['pending_relative_schedule'] = None
                    except Exception:
                        pass

                time_value = (state.get('availability', {}).get('time') or '').strip().lower()
                if time_value and (('am' not in time_value) and ('pm' not in time_value)):
                    return (
                        "Please confirm if the time is AM or PM (and include your timezone). "
                        "Example: '10:25 PM IST'."
                    )

                if not state.get('availability', {}).get('date') or not state.get('availability', {}).get('time'):
                    return (
                        "Please reply with your availability including date, time, and timezone. "
                        "Example: 'tomorrow 10:25 PM IST'."
                    )

                state['stage'] = 'confirmation'
                return await self._generate_confirmation(state, from_number)
            
            # Build a system prompt that includes the latest job description so
            # Gemini can answer role/position related questions accurately.
            jd_snippet = (self.job_description or "").strip()
            if jd_snippet:
                system_prompt = (
                    f"{self.system_prompt}\n\nCurrent job description for this candidate:\n{jd_snippet}\n"
                )
            else:
                system_prompt = self.system_prompt

            # Use Gemini to generate a response based on the conversation history
            response = await self.gemini.generate_response(
                conversation_id=from_number,
                message=message,
                system_prompt=system_prompt,
                conversation_history=state['history']
            )
            
            # Update conversation history with AI response
            state['history'].append({'role': 'assistant', 'content': response})

            # Guardrail: never mention calendar invites in outbound SMS.
            try:
                resp_lc = (response or '').lower()
                if 'calendar invite' in resp_lc or 'calendar invitation' in resp_lc or 'google calendar' in resp_lc or 'outlook' in resp_lc:
                    response = re.sub(
                        r"\b(i\s*'?(?:ll|will)\s*send\s*(?:over\s*)?(?:a\s*)?calendar\s+invit(?:e|ation)[^\.]*(\.|$)",
                        "",
                        response,
                        flags=re.I,
                    ).strip()
                    response = re.sub(r"\s{2,}", " ", response).strip()
            except Exception:
                pass

            return response
            
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            return "I'm sorry, I encountered an error processing your request. Please try again later."
    
    def send_sms_with_sid(self, to_number: str, message: str) -> Optional[str]:
        """Send an SMS message and return its Twilio message SID when accepted."""
        if not self.twilio_client:
            logger.warning("Twilio client not initialized. Cannot send SMS.")
            return None
            
        try:
            # Truncate message if too long for SMS
            if len(message) > 1500:
                message = message[:1497] + "..."
                
            result = self.twilio_client.messages.create(
                body=message,
                from_=self.twilio_phone_number,
                to=to_number
            )
            logger.info(f"Sent SMS to {to_number}")
            return str(result.sid)
        except Exception as e:
            logger.error(f"Error sending SMS to {to_number}: {e}")
            return None

    def send_sms(self, to_number: str, message: str) -> bool:
        """Send an SMS and report whether Twilio accepted it."""
        return bool(self.send_sms_with_sid(to_number, message))

    def get_conversation_history(self, phone_number: str) -> list:
        """Get conversation history for a phone number."""
        return self.conversation_states.get(phone_number, {}).get('conversation_history', [])
    
    def clear_conversation(self, phone_number: str) -> None:
        """Clear conversation history for a phone number."""
        if phone_number in self.conversation_states:
            del self.conversation_states[phone_number]
    
    async def _store_interview_schedule(self, phone_number: str, interview_date: str, 
                                     interview_time: str, timezone: str) -> None:
        """Store interview schedule in the database."""
        try:
            from .models import InterviewSchedule, SessionLocal
            from datetime import datetime
            
            # Parse the scheduled time
            scheduled_datetime = datetime.strptime(f"{interview_date} {interview_time}", "%Y-%m-%d %I:%M %p")
            
            # Create a database record
            db = SessionLocal()
            try:
                interview = InterviewSchedule(
                    candidate_phone=phone_number,
                    scheduled_datetime=scheduled_datetime,
                    timezone=timezone,
                    status='scheduled'
                )
                db.add(interview)
                db.commit()
                logger.info(f"Stored interview schedule for {phone_number} at {scheduled_datetime}")
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"Error storing interview schedule: {e}")
            # Don't fail the whole process if storage fails

    async def schedule_interview_call(self, phone_number: str, interview_date: str,
                                    interview_time: str, timezone: str) -> bool:
        """Schedule a call for the interview time in the candidate's timezone."""
        try:
            from datetime import datetime, timezone as dt_timezone
            import pytz

            # Before doing any heavy parsing, perform a centralized double-booking
            # check. If there is already an active *future* interview scheduled
            # for this phone_number (status scheduled or in_progress), we will
            # NOT create another one here. Reschedule flows (SMS reschedule_mode
            # and email reschedule_intent) cancel the old row before calling
            # this method, so they are still allowed.
            try:
                from .models import InterviewSchedule, SessionLocal
                from .database import engine

                logger.info(f"[central-check] Using DB URL: {engine.url}")

                db = SessionLocal()
                try:
                    existing_list = (
                        db.query(InterviewSchedule)
                        .filter(
                            InterviewSchedule.candidate_phone == phone_number,
                            InterviewSchedule.status.in_(["scheduled", "in_progress"]),
                        )
                        .order_by(InterviewSchedule.scheduled_datetime.asc())
                        .all()
                    )

                    if existing_list:
                        logger.info(
                            f"Central check: found {len(existing_list)} active interview(s) for {phone_number}; "
                            "checking for any that are still in the future."
                        )

                        # Map timezone abbreviations to pytz zones
                        tz_abbr_map = {
                            "PST": "America/Los_Angeles",
                            "PDT": "America/Los_Angeles",
                            "MST": "America/Denver",
                            "MDT": "America/Denver",
                            "CST": "America/Chicago",
                            "CDT": "America/Chicago",
                            "EST": "America/New_York",
                            "EDT": "America/New_York",
                            "IST": "Asia/Kolkata",
                            "GMT": "GMT",
                            "UTC": "UTC",
                        }

                        # We'll treat an interview as blocking if its scheduled_datetime
                        # is still in the future when interpreted in its own timezone.
                        for existing in existing_list:
                            if existing.scheduled_datetime is None:
                                continue

                            try:
                                tz_label = (existing.timezone or "UTC").upper()
                                tz_name = tz_abbr_map.get(tz_label, tz_label)
                                try:
                                    tzinfo = pytz.timezone(tz_name)
                                except pytz.UnknownTimeZoneError:
                                    tzinfo = pytz.timezone("UTC")

                                now_local = datetime.utcnow().replace(tzinfo=pytz.utc).astimezone(tzinfo)
                                now_local_naive = now_local.replace(tzinfo=None)

                                if existing.scheduled_datetime >= now_local_naive:
                                    scheduled_str = existing.scheduled_datetime.strftime("%Y-%m-%d %I:%M %p")
                                    logger.info(
                                        "Central check: active future interview for %s already exists at %s %s (status=%s); "
                                        "refusing to create a second one.",
                                        phone_number,
                                        scheduled_str,
                                        existing.timezone or "UTC",
                                        existing.status,
                                    )
                                    return False
                            except Exception as _tz_ex:
                                logger.warning(
                                    f"Central schedule_interview_call check failed for {phone_number}: {_tz_ex}"
                                )

                        # If we reach here, all active interviews for this phone are in the past.
                        logger.info(
                            f"Central check: all existing active interviews for {phone_number} are in the past; "
                            "allowing new schedule."
                        )
                    else:
                        logger.info(
                            f"Central check: no existing active interviews found for {phone_number}; proceeding to schedule."
                        )
                finally:
                    db.close()
            except Exception as central_check_ex:
                logger.error(
                    f"Error during central double-booking check in schedule_interview_call for {phone_number}: {central_check_ex}"
                )

            # Normalize timezone string to be compatible with pytz
            tz_mapping = {
                'EST': 'US/Eastern',
                'EDT': 'US/Eastern',
                'PST': 'US/Pacific',
                'PDT': 'US/Pacific',
                'CST': 'US/Central',
                'CDT': 'US/Central',
                'MST': 'US/Mountain',
                'MDT': 'US/Mountain',
                'UTC': 'UTC',
                'GMT': 'GMT',
                'IST': 'Asia/Kolkata'
            }
            
            # Get the timezone object
            tz_str = tz_mapping.get(timezone.upper(), 'UTC')
            target_tz = pytz.timezone(tz_str)
            
            # Parse the date and time into a datetime object
            try:
                # Parse the date and time separately
                dt_naive = datetime.strptime(f"{interview_date} {interview_time}", "%Y-%m-%d %I:%M %p")
                # Localize to the target timezone
                dt_local = target_tz.localize(dt_naive, is_dst=None)
                # Convert to UTC for scheduling
                parsed_utc = dt_local.astimezone(dt_timezone.utc)
            except ValueError as e:
                logger.error(f"Error parsing date/time: {e}")
                return False

            now_utc = datetime.now(dt_timezone.utc)
            delay_seconds = (parsed_utc - now_utc).total_seconds()

            logger.info(
                f"Scheduled interview for {phone_number}: "
                f"Local: {dt_local.strftime('%Y-%m-%d %I:%M %p')} {timezone} | "
                f"UTC: {parsed_utc.strftime('%Y-%m-%d %H:%M %Z')} | "
                f"Now UTC: {now_utc.strftime('%Y-%m-%d %H:%M %Z')} | "
                f"Delay: {delay_seconds:.1f}s"
            )

            if delay_seconds < 0:
                logger.warning(
                    f"Interview time {parsed_utc} (candidate tz label={timezone}) "
                    f"is in the past relative to now_utc={now_utc} (delay_seconds={delay_seconds})"
                )
                return False

            # Store the interview schedule (local naive datetime is fine for record-keeping)
            await self._store_interview_schedule(phone_number, interview_date, interview_time, timezone)

            # Mark outreach as completed in the shared DB tracker so we don't send
            # other job outreach to the same candidate later.
            try:
                from .models import OutreachTracker, SessionLocal

                normalized_phone = phone_number.strip()
                if normalized_phone.startswith("00"):
                    normalized_phone = "+" + normalized_phone[2:]
                elif not normalized_phone.startswith("+"):
                    normalized_phone = "+" + normalized_phone

                db = SessionLocal()
                try:
                    rows = (
                        db.query(OutreachTracker)
                        .filter(
                            OutreachTracker.channel == "sms",
                            OutreachTracker.contact == normalized_phone,
                            OutreachTracker.status == "active",
                        )
                        .all()
                    )
                    for t in rows:
                        try:
                            t.status = "completed"
                            t.next_followup_at = None
                        except Exception:
                            pass
                    if rows:
                        db.commit()
                finally:
                    db.close()
            except Exception as tracker_ex:
                logger.error(
                    f"Failed to mark OutreachTracker completed for scheduled interview ({phone_number}): {tracker_ex}",
                    exc_info=True,
                )

            # Schedule the call using a background task
            import asyncio
            logger.info(
                f"Scheduling delayed interview call for {phone_number} in {delay_seconds:.1f} seconds"
            )
            asyncio.create_task(self._delayed_call(phone_number, delay_seconds))

            return True

        except Exception as e:
            logger.error(f"Error scheduling call: {e}")
            return False

    async def _delayed_call(self, phone_number: str, delay_seconds: float) -> None:
        """Make a delayed call to the candidate."""
        import asyncio
        logger.info(f"Delayed call task started for {phone_number}; sleeping {delay_seconds:.1f} seconds")
        await asyncio.sleep(delay_seconds)
        
        try:
            # Initiate the call
            logger.info(f"Waking delayed call task for {phone_number}; initiating interview call")
            await self.initiate_interview_call(phone_number)
        except Exception as e:
            logger.error(f"Error in delayed call to {phone_number}: {e}")
            
    async def initiate_interview_call(self, to_number: str) -> bool:
        """Initiate a voice call for the interview."""
        logger.info("Voice calling is disabled in spreadsheet outreach mode; refusing outbound call")
        return False

        # Legacy implementation retained below; unreachable while voice is disabled.
        if not self.twilio_client:
            logger.warning("Twilio client not initialized. Cannot make call.")
            return False
            
        try:
            # Get the base URL from environment or use a default
            raw_base_url = os.getenv('BASE_URL', 'http://your-server-address')
            # Support inline comments in .env and avoid malformed URLs
            base_url = raw_base_url.split('#', 1)[0].strip().rstrip('/')
            twiml_url = f"{base_url}/api/voice"
            status_callback_url = f"{base_url}/api/call/status"

            # Outbound calls: prewarm the MP3s BEFORE placing the call.
            # This ensures Twilio can <Play> immediately after the call is answered.
            try:
                from app.interview_service import InterviewService  # only for consistent script text

                api_key = (os.getenv('ELEVENLABS_API_KEY') or '').strip()
                voice_id = (os.getenv('ELEVENLABS_VOICE_ID') or '').strip()
                public_base = base_url
                jd = (getattr(self, 'job_description', '') or '').strip()
                if not jd:
                    # Fallback: load latest persisted JobConfig description
                    try:
                        from app.main import JobConfig, SessionLocal

                        _db = SessionLocal()
                        try:
                            cfg = _db.query(JobConfig).order_by(JobConfig.id.desc()).first()
                            if cfg and isinstance(cfg.description, str) and cfg.description.strip():
                                jd = cfg.description.strip()
                        finally:
                            _db.close()
                    except Exception as _jd_ex:
                        logger.warning(f"Failed to load JobConfig for outbound TTS prewarm: {_jd_ex}")

                if api_key and voice_id and public_base.startswith(('http://', 'https://')) and jd:
                    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    tts_dir = os.path.join(project_root, 'tts')
                    os.makedirs(tts_dir, exist_ok=True)

                    # Build scripted prompts using the same logic as InterviewService.
                    # We avoid instantiating InterviewService (needs DB) by calling its helpers.
                    extractor = InterviewService.__dict__.get('_extract_jd_fields')
                    builder = InterviewService.__dict__.get('_scripted_questions')
                    prompts = []
                    greeting = (
                        "Hi, this is Allyson calling from Radixsol. "
                        "Thanks so much for taking my call today—how are you?"
                    )
                    prompts.append(greeting)
                    try:
                        # Create a tiny shim object to call instance methods without DB.
                        class _Shim:
                            pass
                        shim = _Shim()
                        shim._extract_jd_fields = extractor.__get__(shim, _Shim)  # type: ignore
                        shim._scripted_questions = builder.__get__(shim, _Shim)  # type: ignore
                        prompts.extend(list(shim._scripted_questions(jd) or []))
                    except Exception:
                        # Worst case: just warm the greeting.
                        pass

                    # Ack/repeat lines used during the call.
                    prompts.extend([
                        "Before we start, just a quick note: if you’d like me to repeat a question, simply say ‘repeat’. And please take your time—there are no right or wrong answers.",
                        "Of course—happy to repeat that.",
                        "Thank you.",
                        "Got it—thank you.",
                        "That makes sense.",
                        "Okay, thank you.",
                        "Thanks for sharing that.",
                        "Appreciate that.",
                        "Perfect—thanks.",
                        "That’s helpful, thank you.",
                        "Understood.",
                        "No problem.",
                        "Glad to hear that.",
                        "Thanks for letting me know.",
                    ])

                    # Deduplicate while preserving order
                    seen = set()
                    unique_prompts = []
                    for t in prompts:
                        t = (t or '').strip()
                        if not t:
                            continue
                        k = re.sub(r"\s+", " ", t).strip().lower()
                        if k in seen:
                            continue
                        seen.add(k)
                        unique_prompts.append(re.sub(r"\s+", " ", t).strip())

                    configured_model = (os.getenv('ELEVENLABS_MODEL_ID') or '').strip()
                    default_model = 'eleven_flash_v2_5'
                    model_candidates = [configured_model] if configured_model else [default_model]
                    fallback_models = [
                        'eleven_flash_v2_5',
                        'eleven_turbo_v2_5',
                        'eleven_turbo_v2',
                        'eleven_multilingual_v2',
                    ]
                    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
                    headers = {
                        'xi-api-key': api_key,
                        'accept': 'audio/mpeg',
                        'content-type': 'application/json',
                    }

                    optimize_latency = int((os.getenv('ELEVENLABS_OPTIMIZE_STREAMING_LATENCY') or '3').strip() or '3')
                    optimize_latency = max(0, min(4, optimize_latency))
                    output_format = (os.getenv('ELEVENLABS_OUTPUT_FORMAT') or 'mp3_22050_32').strip() or 'mp3_22050_32'
                    params = {
                        'optimize_streaming_latency': optimize_latency,
                        'output_format': output_format,
                    }

                    configured_seed = (os.getenv('ELEVENLABS_SEED') or '').strip()
                    seed = None
                    if configured_seed:
                        try:
                            seed = int(configured_seed)
                        except Exception:
                            seed = None

                    def _normalize_tts_text(text: str) -> str:
                        t = (text or '').strip()
                        t = (
                            t.replace("\u2019", "'")
                             .replace("\u2018", "'")
                             .replace("\u201c", '"')
                             .replace("\u201d", '"')
                             .replace("\u2014", "-")
                             .replace("\u2013", "-")
                        )
                        t = re.sub(r"\s+", " ", t).strip()
                        return t

                    def _ensure_one(text: str) -> None:
                        normalized = _normalize_tts_text(text)
                        key = f"{voice_id}|{normalized}".encode('utf-8')
                        digest = hashlib.sha256(key).hexdigest()
                        target_path = os.path.join(tts_dir, f"{digest}.mp3")
                        if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                            logger.info(f"TTS prewarm cache hit: {os.path.basename(target_path)}")
                            return
                        for model_id in model_candidates:
                            payload = {
                                'text': text,
                                'model_id': model_id,
                                'voice_settings': {
                                    'stability': float(os.getenv('ELEVENLABS_STABILITY', '0.45')),
                                    'similarity_boost': float(os.getenv('ELEVENLABS_SIMILARITY_BOOST', '0.75')),
                                },
                            }
                            if seed is not None:
                                payload['seed'] = seed
                            r = self._http.post(url, params=params, headers=headers, json=payload, timeout=12)
                            if r.status_code == 200 and r.content:
                                tmp_path = target_path + '.tmp'
                                with open(tmp_path, 'wb') as f:
                                    f.write(r.content)
                                os.replace(tmp_path, target_path)
                                logger.info(f"TTS prewarm generated: {os.path.basename(target_path)} (model_id={model_id})")
                                return
                            if 'model_deprecated' in (r.text or '') or 'not available on the free tier' in (r.text or ''):
                                for fm in fallback_models:
                                    if fm not in model_candidates:
                                        model_candidates.append(fm)
                                continue
                            return

                    # Warm the first few prompts before dialing; warm the rest in background.
                    first_batch = unique_prompts[:3]
                    rest_batch = unique_prompts[3:]

                    for t in first_batch:
                        await asyncio.to_thread(_ensure_one, t)

                    async def _warm_rest() -> None:
                        try:
                            sem = asyncio.Semaphore(3)

                            async def _one(t: str) -> None:
                                async with sem:
                                    await asyncio.to_thread(_ensure_one, t)

                            await asyncio.gather(*[_one(t) for t in rest_batch])
                        except Exception as e:
                            logger.error(f"Failed warming remaining TTS prompts: {e}")

                    if rest_batch:
                        asyncio.create_task(_warm_rest())

                    logger.info(f"Outbound TTS prewarm complete (first_batch={len(first_batch)}, total={len(unique_prompts)})")
            except Exception as warm_err:
                logger.error(f"Failed prewarming outbound ElevenLabs TTS: {warm_err}")
            
            # Twilio answering machine detection (AMD) can add several seconds
            # before TwiML executes. Prefer faster settings by default.
            amd_mode = (os.getenv("TWILIO_MACHINE_DETECTION") or "").strip()  # e.g. Enable / DetectMessageEnd / empty
            amd_timeout = int((os.getenv("TWILIO_MACHINE_DETECTION_TIMEOUT") or "3").strip() or "3")

            call_kwargs = {}
            # If AMD isn't explicitly configured, default to DetectMessageEnd so voicemail
            # does NOT receive the full interview flow.
            effective_amd = amd_mode or "DetectMessageEnd"
            if effective_amd:
                call_kwargs["machine_detection"] = effective_amd
                call_kwargs["machine_detection_timeout"] = max(1, min(10, amd_timeout))

            # Ensure we receive call status events so missed calls trigger reschedule SMS.
            if base_url.startswith(('http://', 'https://')):
                call_kwargs["status_callback"] = status_callback_url
                call_kwargs["status_callback_method"] = "POST"
                call_kwargs["status_callback_event"] = [
                    "initiated",
                    "ringing",
                    "answered",
                    "completed",
                ]

            call = self.twilio_client.calls.create(
                to=to_number,
                from_=self.twilio_phone_number,
                url=twiml_url,
                method='POST',
                record=True,
                **call_kwargs,
            )
            logger.info(f"Initiated interview call to {to_number}, SID: {call.sid}")
            return True
            
        except Exception as e:
            logger.error(f"Error initiating call to {to_number}: {e}")
            return False

# Create a singleton instance of MessagingService
messaging_service = MessagingService()
