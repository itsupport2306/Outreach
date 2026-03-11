import asyncio
import os
import json
import logging
import base64
import email
import traceback
import pytz
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List, Tuple
import re
from email.utils import parsedate_to_datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2 import service_account
import google.generativeai as genai
from pydantic import BaseModel
from pathlib import Path
import os.path
import time
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Google Calendar API scopes
SCOPES = ['https://www.googleapis.com/auth/calendar']

_alembic_running = (os.getenv("ALEMBIC_RUNNING") or "").strip() in {"1", "true", "True"}

class InterviewSlot(BaseModel):
    """Represents an available interview time slot."""
    start_time: datetime
    end_time: datetime
    timezone: str = "UTC"

class AvailabilityRequest(BaseModel):
    """Request model for checking availability."""
    candidate_email: str
    candidate_name: str
    email_content: str
    interviewer_email: str
    interview_duration_minutes: int = 60

class ScheduleRequest(BaseModel):
    """Request model for scheduling an interview."""
    candidate_email: str
    candidate_name: str
    interviewer_email: str
    interview_slot: InterviewSlot
    meeting_title: str = "Interview"
    meeting_description: str = "Interview with candidate"

class EmailProcessor:
    async def generate_response(self, prompt: str, max_tokens: int = 500) -> str:
        """Generate a response using Gemini."""
        try:
            import google.generativeai as genai

            # Configure the model
            model = genai.GenerativeModel('gemini-pro')

            # Generate content
            response = await asyncio.to_thread(
                model.generate_content,
                prompt,
                generation_config={
                    "max_output_tokens": max_tokens,
                    "temperature": 0.7,
                }
            )

            return response.text

        except Exception as e:
            logger.error(f"Error generating response: {e}")
            raise

    def __init__(self, google_api_key: str, credentials_path: str = None):
        """Initialize the email processor with Google API key."""
        genai.configure(api_key=google_api_key)
        # Use a supported model
        try:
            # List available models and log them
            available_models = genai.list_models()
            
            # Get the list of available model names
            available_model_names = []
            for model in available_models:
                model_name = model.name
                available_model_names.append(model_name)
            
            logger.info(f"Found {len(available_model_names)} available models")
            logger.debug(f"Available models: {', '.join(available_model_names)}")
            
            # Define the model we want to use
            target_model = 'models/gemini-2.5-flash-lite'
            
            # Check if the model exists in the available models
            if target_model in available_model_names:
                model_name = target_model
                logger.info(f"Using model: {model_name}")
            else:
                logger.warning(f"Model {target_model} not found in available models")
                # Try to find a similar model
                similar_models = [m for m in available_model_names if '2.5' in m and ('flash' in m or 'pro' in m)]
                if similar_models:
                    model_name = similar_models[0]  # Use the first similar model
                    logger.warning(f"Using alternative model: {model_name}")
                else:
                    # If no similar models, try to use any available model
                    if available_model_names:
                        model_name = available_model_names[0]
                        logger.warning(f"No matching model found. Using first available model: {model_name}")
                    else:
                        raise ValueError("No models available. Please check your API key and permissions.")
            
            # Initialize the model
            logger.info(f"Initializing model: {model_name}")
            self.model = genai.GenerativeModel(model_name)
            logger.info(f"Successfully initialized Gemini model: {model_name}")
            
        except Exception as e:
            logger.error(f"Failed to initialize Gemini model: {e}")
            logger.error("Please check your API key and ensure it has access to the Gemini API")
            raise
        # Initialize Google Calendar Scheduler with explicit credentials path
        calendar_creds_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.credentials', 'token.json')
        self.scheduler = (
            GoogleCalendarScheduler(credentials_path=calendar_creds_path, disabled=True)
            if _alembic_running
            else GoogleCalendarScheduler(credentials_path=calendar_creds_path)
        )
        if not self.scheduler.service:
            logger.warning("Google Calendar service not initialized. Will not be able to schedule interviews.")
            logger.warning(f"Please make sure you've run google_auth.py and that the file exists at: {calendar_creds_path}")
        self.processed_emails = set()
        self.load_processed_emails()

        # Build an email -> phone lookup from Applicant Data.json so that
        # we can schedule Twilio calls for candidates who reply by email.
        try:
            project_root = Path(__file__).resolve().parents[1]
            data_file = project_root / "Applicant Data.json"
            self.email_to_phone: Dict[str, str] = {}

            if data_file.exists():
                with data_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, list):
                    for c in data:
                        if not isinstance(c, dict):
                            continue

                        # Accept multiple possible email keys
                        emails = [
                            c.get("EmailID1"),
                            c.get("EmailID2"),
                            c.get("Email"),
                            c.get("email"),
                        ]
                        # Accept multiple possible phone keys
                        phone = (
                            c.get("phone")
                            or c.get("Phone")
                            or c.get("PhoneNumber")
                            or c.get("Phone_No")
                            or c.get("ContactNumber")
                        )

                        if phone and emails:
                            for e in emails:
                                if e and isinstance(e, str):
                                    self.email_to_phone[e.strip().lower()] = str(phone)

                    logger.info(
                        f"Initialized email_to_phone map with {len(self.email_to_phone)} entries from Applicant Data.json"
                    )
                else:
                    logger.warning("Applicant Data.json is not a list; email_to_phone map not built")
            else:
                logger.warning(f"Applicant Data.json not found at {data_file}; email_to_phone map will be empty")

        except Exception as e:
            logger.error(f"Failed to build email_to_phone map from Applicant Data.json: {e}")
            self.email_to_phone = {}
    
    def load_processed_emails(self):
        """Load set of already processed email message IDs."""
        try:
            with open('processed_emails.json', 'r') as f:
                self.processed_emails = set(json.load(f).get('message_ids', []))
        except (FileNotFoundError, json.JSONDecodeError):
            self.processed_emails = set()
    
    def save_processed_emails(self):
        """Save processed email message IDs to file."""
        with open('processed_emails.json', 'w') as f:
            json.dump({'message_ids': list(self.processed_emails)}, f)
    
    def _extract_headers(self, msg: Dict[str, Any]) -> Dict[str, str]:
        """Extract and normalize headers from message."""
        headers = {}
        for header in msg.get('payload', {}).get('headers', []):
            name = header.get('name', '').lower()
            if name:
                headers[name] = header.get('value', '')
        return headers
    
    def _extract_email_address(self, from_header: str) -> str:
        """Extract email address from 'From' header."""
        if not from_header:
            return ''
            
        # Handle formats like "Name <email@example.com>" or just "email@example.com"
        match = re.search(r'<([^>]+)>', from_header)
        if match:
            return match.group(1).lower().strip()
        return from_header.lower().strip()
    
    def extract_email_content(self, msg: Dict[str, Any]) -> Tuple[str, str, str]:
        """Extract text content and headers from email message."""
        try:
            # Get message ID from the message itself first
            message_id = msg.get('id', '')
            logger.debug(f"Starting to process message ID: {message_id}")
            
            # Extract and log headers
            headers = self._extract_headers(msg)
            
            # Extract basic info
            subject = headers.get('subject', '[No Subject]').strip()
            from_header = headers.get('from', '').strip()
            from_email = self._extract_email_address(from_header)
            
            # Prefer header message-id over the one from the message
            message_id = headers.get('message-id', message_id).strip()
            
            logger.info(f"Processing message {message_id}")
            logger.info(f"From: {from_header} (extracted: {from_email})")
            logger.info(f"Subject: {subject}")
            
            # Validate we have required fields
            if not message_id:
                logger.error("No message ID found, cannot process")
                return '', '', ''
                
            if not from_email:
                logger.error(f"No valid from email found in headers: {from_header}")
                return '', '', ''
            
            # Extract email body first, before checking if processed
            logger.debug("Extracting email body...")
            body = self._extract_body_from_payload(msg.get('payload', {}))
            
            # Only mark as processed if we successfully extracted content
            if not body or not body.strip():
                logger.warning(f"No text content found in message {message_id}")
                logger.debug(f"Message payload structure: {json.dumps(msg.get('payload', {}), indent=2, default=str)}")
                return '', from_email, subject  # Return from_email even if no body
            
            # Clean up the body text while preserving paragraphs
            body = '\n\n'.join(
                ' '.join(line.strip() for line in para.split('\n') if line.strip())
                for para in body.split('\n\n')
                if para.strip()
            )
            
            # Check if already processed (after successful extraction)
            if message_id in self.processed_emails:
                logger.info(f"Skipping already processed message: {message_id}")
                return body, from_email, subject  # Return the extracted content even if processed
            
            # Add to processed emails only after successful processing
            self.processed_emails.add(message_id)
            self.save_processed_emails()
            
            logger.debug(f"Successfully extracted content from message {message_id}")
            logger.debug(f"From: {from_email}")
            logger.debug(f"Subject: {subject}")
            logger.debug(f"Body preview: {body[:200]}...")
            
            return body, from_email, subject
            
        except Exception as e:
            logger.error(f"Error extracting email content: {e}")
            logger.error(traceback.format_exc())
            return '', '', ''
            
    def _extract_body_from_payload(self, payload: Dict[str, Any]) -> str:
        """Recursively extract text from email payload."""
        if not payload or not isinstance(payload, dict):
            logger.warning("Invalid payload received")
            return ''
            
        try:
            # Log the payload structure for debugging
            logger.debug(f"Payload keys: {list(payload.keys())}")
            
            # Get mime type for this part
            mime_type = payload.get('mimeType', '').lower()
            logger.debug(f"Processing part with mimeType: {mime_type}")
            
            # Handle multipart emails
            if mime_type.startswith('multipart/'):
                logger.debug(f"Processing multipart message: {mime_type}")
                parts = []
                for i, part in enumerate(payload.get('parts', [])):
                    logger.debug(f"Processing part {i+1} of {len(payload.get('parts', []))}")
                    part_body = self._extract_body_from_payload(part)
                    if part_body and part_body.strip():
                        parts.append(part_body.strip())
                
                # For multipart/alternative, prefer text/plain over text/html
                if mime_type == 'multipart/alternative' and len(parts) > 1:
                    plain_text = next((p for p in parts if 'html' not in p.lower()), parts[0])
                    return plain_text
                
                return '\n\n'.join(parts) if parts else ''
            
            # Get the body data
            body_data = payload.get('body', {})
            if not isinstance(body_data, dict):
                logger.debug("Body is not a dictionary, trying to use as text")
                return str(body_data) if body_data else ''
            
            # Check for inline text content
            if 'data' not in body_data and 'body' in payload:
                logger.debug("No data in body, trying payload body")
                return str(payload['body'])
                
            data = body_data.get('data')
            if not data:
                logger.debug("No data in body_data")
                return ''
            
            # Decode the data
            try:
                # Try URL-safe base64 first, then standard base64
                try:
                    # Ensure proper padding for base64
                    padding = '=' * (-len(data) % 4)
                    decoded_bytes = base64.urlsafe_b64decode(data + padding)
                except Exception as e1:
                    logger.debug(f"URL-safe base64 decode failed, trying standard base64: {e1}")
                    try:
                        decoded_bytes = base64.b64decode(data)
                    except Exception as e2:
                        logger.error(f"Failed to decode base64 data: {e2}")
                        return ''
                
                # Get charset from headers or default to utf-8
                charset = body_data.get('charset', 'utf-8')
                
                # Try to decode with detected charset, fallback to utf-8 and other common charsets
                text = ''
                for encoding in [charset, 'utf-8', 'iso-8859-1', 'ascii']:
                    try:
                        text = decoded_bytes.decode(encoding, errors='strict')
                        break
                    except (UnicodeDecodeError, LookupError) as e:
                        logger.debug(f"Failed to decode with {encoding}: {e}")
                        continue
                
                if not text:  # If all decodings failed, use replace to avoid errors
                    text = decoded_bytes.decode('utf-8', errors='replace')
                
                # If it's HTML, clean it up
                if 'html' in mime_type:
                    try:
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(text, 'html.parser')
                        
                        # Remove unwanted elements
                        for element in soup(["script", "style", "head", "title", "meta", "link", "img"]):
                            element.decompose()
                        
                        # Get text with proper line breaks
                        text = soup.get_text(separator='\n')
                        
                        # Clean up whitespace while preserving paragraphs
                        lines = (line.strip() for line in text.splitlines())
                        chunks = (phrase.strip() for line in lines 
                                for phrase in line.split("  ") if phrase.strip())
                        text = '\n'.join(chunk for chunk in chunks if chunk)
                        
                    except Exception as html_error:
                        logger.warning(f"Error parsing HTML: {html_error}")
                
                # Clean up the text while preserving paragraphs
                text = '\n\n'.join(
                    ' '.join(line.strip() for line in para.split('\n') if line.strip())
                    for para in text.split('\n\n')
                    if para.strip()
                )
                
                logger.debug(f"Successfully extracted text (first 200 chars): {text[:200]}...")
                return text.strip()
                
            except Exception as decode_error:
                logger.error(f"Error processing email part (mime: {mime_type}): {decode_error}")
                logger.error(traceback.format_exc())
                return ''
                
        except Exception as e:
            logger.error(f"Unexpected error in _extract_body_from_payload: {e}")
            logger.error(traceback.format_exc())
            return ''

    def _extract_availability(self, email_body: str) -> Dict[str, Any]:
        """Extract availability from email body using Gemini."""
        try:
            # Prepare the prompt for Gemini
            prompt = f"""You are helping schedule interviews based on an email.
            Extract the available time slots from the following email.

            Return a JSON object with:
            - 'times': list of time slot strings
            - 'timezone': the candidate's timezone **only if it is clearly stated** in the email
            - 'reschedule_intent': a boolean indicating whether the sender clearly wants to
              CHANGE an already-scheduled interview to a NEW time.

            Rules:
            - Time slots should be in the format: 'YYYY-MM-DD HH:MM AM/PM - HH:MM AM/PM TIMEZONE'.
            - ALWAYS treat short messages that clearly look like availability, such as
              "Today at 9:30 PM IST" or "Tomorrow at 10:00 AM PST", as a valid time slot and
              include them in the 'times' list.
            - If the email mentions a day and time like "today at 9:30 PM" but does NOT clearly
              specify a timezone, you must set "timezone" to "UNKNOWN".
            - Do NOT guess the timezone from your own knowledge of cities or IPs.
              Only use a timezone if the email explicitly contains a timezone, city with timezone,
              or phrasing like "IST", "PST", "Pacific Time", etc.
            - If no specific date is mentioned, you may expand to concrete dates within the next 7 days.
            - For 'reschedule_intent': set this to true ONLY if the email explicitly says they want to
              change, move, or reschedule an interview that is already scheduled (for example, phrases
              like "can we reschedule", "I need to change the time", "move the interview", etc.).
              If the email just provides availability without clearly referring to changing an existing
              booking, set 'reschedule_intent' to false.

            Email content:
            {email_body[:2000]}...  # Truncate to avoid token limits

            Example response when timezone is clearly specified:
            {{
                "times": ["2023-11-28 10:00 AM - 11:00 AM IST", "2023-11-29 2:00 PM - 3:00 PM IST"],
                "timezone": "IST",
                "reschedule_intent": false
            }}

            Example response when the email says only "today at 9:30 PM" with no timezone:
            {{
                "times": ["2023-11-28 09:30 PM - 10:30 PM UNKNOWN"],
                "timezone": "UNKNOWN",
                "reschedule_intent": false
            }}"""
            
            logger.debug("Sending prompt to Gemini for availability extraction")
            try:
                response = self.model.generate_content(prompt)
                
                # Handle the response based on the model's return type
                if hasattr(response, 'text'):
                    json_str = response.text.strip()
                elif hasattr(response, 'result'):
                    json_str = response.result.text.strip()
                else:
                    logger.error(f"Unexpected response format from Gemini: {response}")
                    return {"times": [], "timezone": "UTC"}
                
                logger.debug(f"Raw Gemini response: {json_str}")
                
                # Clean the response to extract JSON
                try:
                    # Try to parse directly first
                    data = json.loads(json_str)
                    # Ensure default keys exist
                    if 'times' not in data:
                        data['times'] = []
                    if 'timezone' not in data:
                        data['timezone'] = 'UTC'
                    if 'reschedule_intent' not in data:
                        data['reschedule_intent'] = False
                    return data
                except json.JSONDecodeError:
                    # If direct parsing fails, try to extract JSON from markdown code blocks
                    if '```json' in json_str:
                        json_str = json_str.split('```json')[1].split('```')[0].strip()
                    elif '```' in json_str:
                        json_str = json_str.split('```')[1].strip()
                    
                    # Remove any non-JSON content before and after the JSON
                    json_str = json_str[json_str.find('{'):json_str.rfind('}')+1]
                    data = json.loads(json_str)
                    if 'times' not in data:
                        data['times'] = []
                    if 'timezone' not in data:
                        data['timezone'] = 'UTC'
                    if 'reschedule_intent' not in data:
                        data['reschedule_intent'] = False
                    return data
                
            except json.JSONDecodeError as je:
                logger.error(f"Failed to parse Gemini response as JSON: {json_str}")
                logger.error(f"JSON decode error: {je}")
                return {'times': [], 'timezone': 'UTC', 'reschedule_intent': False}
                
        except Exception as e:
            logger.error(f"Error extracting availability: {e}")
            logger.error(traceback.format_exc())
            return {'times': [], 'timezone': 'UTC', 'reschedule_intent': False}

    def _parse_time_slot(self, time_slot: str, timezone: str = 'UTC') -> Optional[Tuple[datetime, datetime]]:
        """Parse a time slot string into datetime objects."""
        try:
            # Clean up the time slot string
            time_slot = time_slot.strip()
            
            # Map common timezone abbreviations to their full timezone names
            tz_abbr_map = {
                'PST': 'America/Los_Angeles',
                'PDT': 'America/Los_Angeles',
                'MST': 'America/Denver',
                'MDT': 'America/Denver',
                'CST': 'America/Chicago',
                'CDT': 'America/Chicago',
                'EST': 'America/New_York',
                'EDT': 'America/New_York',
                'IST': 'Asia/Kolkata',
                'GMT': 'GMT',
                'UTC': 'UTC'
            }
            
            # Try different time slot formats
            patterns = [
                # Format: "2024-11-27 04:00 PM - 05:00 PM PST"
                (r'(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<start_time>\d{1,2}:\d{2}\s+[AP]M)\s*-\s*(?P<end_time>\d{1,2}:\d{2}\s+[AP]M)\s+(?P<tz_abbr>[A-Z]{2,4})',
                 '%Y-%m-%d %I:%M %p'),
                # Format: "2024-11-27T16:00:00-08:00"
                (r'(?P<datetime>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2})',
                 '%Y-%m-%dT%H:%M:%S%z'),
                # Format: "2024-11-27 16:00"
                (r'(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2})',
                 '%Y-%m-%d %H:%M')
            ]

            for pattern, time_format in patterns:
                match = re.search(pattern, time_slot)
                if match:
                    if 'datetime' in match.groupdict():
                        # Handle ISO format with timezone
                        start_dt = datetime.strptime(match.group('datetime'), time_format)
                        end_dt = start_dt + timedelta(hours=1)  # Default 1 hour duration
                        return start_dt, end_dt
                    else:
                        # Handle other formats
                        date_str = match.group('date')
                        start_str = f"{date_str} {match.group('start_time')}" if 'start_time' in match.groupdict() else f"{date_str} {match.group('time')}"
                        
                        # Parse start time
                        start_dt = datetime.strptime(start_str, time_format)
                        
                        # Parse end time or use default duration
                        if 'end_time' in match.groupdict():
                            end_str = f"{date_str} {match.group('end_time')}"
                            end_dt = datetime.strptime(end_str, time_format)
                            
                            # If end time is before start time, it's likely the next day
                            if end_dt < start_dt:
                                end_dt += timedelta(days=1)
                        else:
                            end_dt = start_dt + timedelta(hours=1)
                        
                        # Handle timezone if specified
                        tz_abbr = match.group('tz_abbr') if 'tz_abbr' in match.groupdict() else timezone
                        if tz_abbr:
                            # Convert timezone abbreviation to full timezone name
                            tz_name = tz_abbr_map.get(tz_abbr.upper(), tz_abbr)
                            try:
                                tzinfo = pytz.timezone(tz_name)
                                if start_dt.tzinfo is None:
                                    start_dt = tzinfo.localize(start_dt)
                                    end_dt = tzinfo.localize(end_dt)
                            except pytz.UnknownTimeZoneError:
                                logger.warning(f"Unknown timezone: {tz_abbr}, falling back to {timezone}")
                                tzinfo = pytz.timezone(timezone)
                                if start_dt.tzinfo is None:
                                    start_dt = tzinfo.localize(start_dt)
                                    end_dt = tzinfo.localize(end_dt)
                        
                        return start_dt, end_dt

            logger.warning(f"Could not parse time slot: {time_slot}")
            return None, None

        except Exception as e:
            logger.error(f"Error parsing time slot {time_slot}: {e}")
            logger.error(traceback.format_exc())
            return None, None

    def schedule_from_email_reply(self, service, message: Dict[str, Any]) -> bool:
        """Process an email reply and schedule a meeting if possible."""
        message_id = message.get('id', 'unknown')
        try:
            logger.info(f"Processing message {message_id}")
            
            # Extract email content with detailed logging
            logger.debug(f"Extracting content from message: {message_id}")
            body, from_email, subject = self.extract_email_content(message)
            
            # Check if this is a case where the message was already processed
            if not body and not from_email:
                logger.debug(f"Message {message_id} was already processed or has no content")
                return True  # Return True to indicate no error, just nothing to do
                
            if not body:
                logger.warning(f"No body content in message {message_id}")
                return False
                
            if not from_email:
                logger.warning(f"No sender email in message {message_id}")
                return False
            
            logger.info(f"Processing email from {from_email} with subject: {subject} (Message ID: {message_id})")
            
            # Extract candidate name from email or subject with better parsing
            candidate_name = ' '.join(part.capitalize() for part in from_email.split('@')[0].replace('.', ' ').replace('_', ' ').split())
            subject_lower = subject.lower()
            
            # Try to extract name from common email subject patterns
            for prefix in ['re:', 'fwd:', 'fw:']:
                if subject_lower.startswith(prefix):
                    subject = subject[len(prefix):].strip()
                    subject_lower = subject.lower()
            
            if ' from ' in subject_lower:
                name_part = subject.split(' from ')[1].strip()
                if ' <' in name_part:  # Handle "Name <email@example.com>" format
                    name_part = name_part.split('<')[0].strip()
                candidate_name = ' '.join(part.capitalize() for part in name_part.split() if not '@' in part)
            
            logger.debug(f"Extracted candidate name: {candidate_name}")
            
            # Log the first 500 chars of the email body for debugging
            logger.debug(f"Email body preview (first 500 chars):\n{body[:500]}...")
            
            # Extract availability using Gemini with better error handling
            try:
                logger.info("Extracting availability using Gemini...")
                availability = self._extract_availability(body)
                
                if not availability or not availability.get('times'):
                    logger.warning(f"No availability data could be extracted from message {message_id}")
                    logger.debug(f"Availability response: {json.dumps(availability, indent=2)[:500]}...")
                    return False
                    
                time_slots = availability['times']
                timezone = availability.get('timezone', 'UTC')
                
                logger.info(f"Found {len(time_slots)} time slots in timezone: {timezone}")
                logger.debug(f"Available time slots: {json.dumps(time_slots, indent=2)}")
                
            except Exception as e:
                logger.error(f"Error extracting availability: {e}")
                logger.error(traceback.format_exc())
                return False
            
            # Get interviewer email from environment
            interviewer_email = os.getenv('INTERVIEWER_EMAIL')
            if not interviewer_email:
                logger.error("INTERVIEWER_EMAIL environment variable not set")
                return False
                
            # Try each time slot until we find one that works
            for i, time_slot in enumerate(time_slots, 1):
                try:
                    logger.info(f"Attempting to schedule time slot {i}/{len(time_slots)}: {time_slot}")
                    
                    # Parse the time slot with better error handling
                    start_time, end_time = self._parse_time_slot(time_slot, timezone)
                    if not start_time or not end_time:
                        logger.warning(f"Could not parse time slot: {time_slot}")
                        continue
                        
                    logger.info(f"Attempting to schedule interview for {start_time.isoformat()} to {end_time.isoformat()}")
                    
                    # Create interview slot
                    interview_slot = InterviewSlot(
                        start_time=start_time,
                        end_time=end_time,
                        timezone=timezone
                    )
                    
                    # Create schedule request
                    schedule_request = ScheduleRequest(
                        candidate_email=from_email,
                        candidate_name=candidate_name,
                        interviewer_email=interviewer_email,
                        interview_slot=interview_slot,
                        meeting_title=f"Interview with {candidate_name}",
                        meeting_description=f"Scheduled based on email reply from {from_email}"
                    )
                    
                    # Schedule the interview
                    event = self.scheduler.schedule_interview(schedule_request)
                    
                    if event and 'error' not in event:
                        logger.info(f"Successfully scheduled interview for {from_email}")
                        logger.debug(f"Event details: {json.dumps(event, indent=2, default=str)[:500]}...")
                        
                        # Send confirmation email
                        try:
                            self._send_confirmation_email(
                                service=service,
                                to_email=from_email,
                                candidate_name=candidate_name,
                                event=event
                            )
                            logger.info(f"Confirmation email sent to {from_email}")
                        except Exception as email_error:
                            logger.error(f"Error sending confirmation email: {email_error}")
                            logger.error(traceback.format_exc())
                            # Don't fail the whole process if email sending fails
                            
                        return True
                    else:
                        error_msg = event.get('error', 'Unknown error') if isinstance(event, dict) else 'Invalid event format'
                        logger.warning(f"Failed to schedule interview: {error_msg}")
                        
                except Exception as e:
                    logger.error(f"Error processing time slot {time_slot}: {e}")
                    logger.error(traceback.format_exc())
                    continue
                    
            logger.warning(f"Could not schedule any time slot for {from_email} after trying {len(time_slots)} slots")
            return False
            
        except Exception as e:
            logger.error(f"Unexpected error in schedule_from_email_reply: {e}")
            logger.error(traceback.format_exc())
            return False
    
    async def schedule_from_plain_email(self, candidate_email: str, candidate_name: str, email_content: str) -> Dict[str, Any]:
        """Process a plain email reply (e.g. from a webhook) and schedule a meeting & call if possible.

        This reuses the same availability extraction and scheduling logic as schedule_from_email_reply,
        but works directly with raw email content and addresses instead of a Gmail API message.
        It also mirrors the messaging feature by storing the schedule and scheduling a Twilio call
        using MessagingService when a phone number is available for the candidate.
        """
        try:
            logger.info(f"Processing plain email from {candidate_email} for scheduling")

            if not email_content or not email_content.strip():
                logger.warning("Email content is empty, cannot extract availability")
                return {
                    "status": "error",
                    "message": "Empty email content, could not extract availability",
                }

            # Extract availability using Gemini
            try:
                logger.info("Extracting availability from plain email using Gemini...")
                availability = self._extract_availability(email_content)

                if not availability or not availability.get("times"):
                    logger.warning("No availability data could be extracted from plain email")
                    logger.debug(f"Availability response: {json.dumps(availability, indent=2)[:500]}...")

                    # When we cannot detect any times at all (e.g. reply is just
                    # "Yes, I am available today"), send a clarification email
                    # asking the candidate to provide day, time, and timezone.
                    try:
                        from sendgrid import SendGridAPIClient
                        from sendgrid.helpers.mail import Mail, HtmlContent, Email

                        sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
                        from_email = os.getenv("SENDGRID_FROM_EMAIL") or "noreply@example.com"
                        reply_to_email = os.getenv("SENDGRID_INBOUND_EMAIL")

                        if not sendgrid_api_key:
                            logger.error(
                                "SENDGRID_API_KEY not configured; cannot send availability clarification email."
                            )
                        else:
                            sg = SendGridAPIClient(sendgrid_api_key)

                            subject = "Clarification on your interview availability"
                            # Include a short preview of what they wrote
                            preview = (email_content or "").strip()[:300]
                            html_body = f"""
                            <p>Hi,</p>
                            <p>Thank you for your reply about your availability.</p>
                            <p>To schedule your interview, could you please reply with:</p>
                            <ul>
                              <li><b>Day/Date</b> (for example: 24 Dec 2025 or tomorrow)</li>
                              <li><b>Time</b> (for example: 10:00 PM)</li>
                              <li><b>Time zone</b> (for example: IST, PST, or your city)</li>
                            </ul>
                            <p>Once we have these three details, we'll schedule the interview and send you a confirmation.</p
                            <p>Best regards,<br/>Talent Team</p>
                            """

                            message = Mail(
                                from_email=from_email,
                                to_emails=candidate_email,
                                subject=subject,
                                html_content=HtmlContent(html_body),
                            )

                            # Route replies from this clarification email back to the inbound webhook
                            if reply_to_email:
                                try:
                                    message.reply_to = Email(reply_to_email)
                                except Exception:
                                    logger.warning("Failed to set reply_to for availability clarification email")

                            try:
                                sg.send(message)
                                logger.info(
                                    f"Sent availability clarification email (no times detected) to {candidate_email}."
                                )
                            except Exception as send_ex:
                                logger.error(
                                    f"Failed to send availability clarification email to {candidate_email}: {send_ex}"
                                )

                    except Exception as clar_ex:
                        logger.error(
                            f"Error while handling missing availability times for {candidate_email}: {clar_ex}"
                        )

                    return {
                        "status": "error",
                        "message": "Could not detect any clear availability in the email; sent clarification request",
                        "details": availability,
                    }

                time_slots = availability["times"]
                timezone = (availability.get("timezone") or "").strip() or "UNKNOWN"

                logger.info(f"Found {len(time_slots)} time slots in timezone: {timezone}")
                logger.debug(f"Available time slots: {json.dumps(time_slots, indent=2)}")

                # Extra safety: ensure the raw email actually contains explicit
                # day/date, time, and timezone. If any of these are missing,
                # treat it as ambiguous and send a clarification email instead
                # of trusting a possibly hallucinated Gemini response.
                try:
                    body_lower = (email_content or "").lower()

                    # Day/date: today/tomorrow, weekday, or a numeric date pattern
                    has_day_or_date = bool(
                        re.search(r"\b(today|tomorrow|mon(day)?|tue(sday)?|wed(nesday)?|thu(rsday)?|fri(day)?|sat(urday)?|sun(day)?)\b",
                                  body_lower)
                        or re.search(r"\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
                                     body_lower)
                        or re.search(r"\b\d{4}-\d{2}-\d{2}\b", body_lower)
                    )

                    # Time: 10 PM, 10:30 PM, or 24h style 16:00
                    has_time = bool(
                        re.search(r"\b\d{1,2}:\d{2}\s*(am|pm)\b", body_lower)
                        or re.search(r"\b\d{1,2}\s*(am|pm)\b", body_lower)
                        or re.search(r"\b\d{1,2}:\d{2}\b", body_lower)
                    )

                    # Timezone: common abbreviations or words like 'time zone'
                    has_tz = bool(
                        re.search(r"\b(ist|pst|pdt|est|edt|cst|cdt|mst|mdt|gmt|utc|cet|bst)\b", body_lower)
                        or "time zone" in body_lower
                        or "timezone" in body_lower
                    )

                    if not (has_day_or_date and has_time and has_tz):
                        logger.info(
                            "Email does not explicitly contain all of day/date, time, and timezone; "
                            "sending clarification email instead of scheduling."
                        )

                        # Reuse the same clarification email logic as the no-times case
                        try:
                            from sendgrid import SendGridAPIClient
                            from sendgrid.helpers.mail import Mail, HtmlContent, Email

                            sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
                            from_email = os.getenv("SENDGRID_FROM_EMAIL") or "noreply@example.com"
                            reply_to_email = os.getenv("SENDGRID_INBOUND_EMAIL")

                            if not sendgrid_api_key:
                                logger.error(
                                    "SENDGRID_API_KEY not configured; cannot send availability clarification email "
                                    "for missing fields."
                                )
                            else:
                                sg = SendGridAPIClient(sendgrid_api_key)

                                subject = "Clarification on your interview availability"
                                preview = (email_content or "").strip()[:300]
                                html_body = f"""
                                <p>Hi,</p>
                                <p>Thank you for your reply about your availability.</p>
                                <p>To schedule your interview, could you please reply with:</p>
                                <ul>
                                  <li><b>Day/Date</b> (for example: 24 Dec 2025 or tomorrow)</li>
                                  <li><b>Time</b> (for example: 10:00 PM)</li>
                                  <li><b>Time zone</b> (for example: IST, PST, or your city)</li>
                                </ul>
                                <p>Once we have these three details, we'll schedule the interview and send you a confirmation.</p>
                                <p>Best regards,<br/>Talent Team</p>
                                """

                                message = Mail(
                                    from_email=from_email,
                                    to_emails=candidate_email,
                                    subject=subject,
                                    html_content=HtmlContent(html_body),
                                )

                                if reply_to_email:
                                    try:
                                        message.reply_to = Email(reply_to_email)
                                    except Exception:
                                        logger.warning(
                                            "Failed to set reply_to for availability clarification email (missing fields)"
                                        )

                                try:
                                    sg.send(message)
                                    logger.info(
                                        f"Sent availability clarification email (missing day/time/timezone) to {candidate_email}."
                                    )
                                except Exception as send_ex:
                                    logger.error(
                                        f"Failed to send availability clarification email to {candidate_email}: {send_ex}"
                                    )

                        except Exception as clar_ex:
                            logger.error(
                                f"Error while handling missing explicit availability fields for {candidate_email}: {clar_ex}"
                            )

                        return {
                            "status": "error",
                            "message": "Email did not clearly specify day, time, and timezone; sent clarification request",
                            "details": {
                                "availability": availability,
                                "has_day_or_date": has_day_or_date,
                                "has_time": has_time,
                                "has_tz": has_tz,
                            },
                        }

                except Exception as explicit_check_ex:
                    logger.warning(f"Failed to validate explicit availability fields: {explicit_check_ex}")

            except Exception as e:
                logger.error(f"Error extracting availability from plain email: {e}")
                logger.error(traceback.format_exc())
                return {
                    "status": "error",
                    "message": "Error while extracting availability from the email",
                }

            # Get interviewer email from environment
            interviewer_email = os.getenv("INTERVIEWER_EMAIL")
            if not interviewer_email:
                logger.error("INTERVIEWER_EMAIL environment variable not set")
                return {
                    "status": "error",
                    "message": "INTERVIEWER_EMAIL environment variable not set",
                }

            # If timezone is missing or unknown, send a clarification email instead of scheduling
            if timezone.upper() in {"", "UNKNOWN", "UNK", "N/A"}:
                try:
                    logger.info(
                        "Timezone was not clearly provided in the email; sending clarification email instead of scheduling."
                    )

                    # Build a simple summary of proposed times (without timezone) for the email body
                    preview_times = "\n".join(str(t) for t in time_slots[:3])

                    from sendgrid import SendGridAPIClient
                    from sendgrid.helpers.mail import Mail, HtmlContent, Email

                    sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
                    from_email = os.getenv("SENDGRID_FROM_EMAIL") or "noreply@example.com"
                    reply_to_email = os.getenv("SENDGRID_INBOUND_EMAIL")

                    if not sendgrid_api_key:
                        logger.error(
                            "SENDGRID_API_KEY not configured; cannot send timezone clarification email."
                        )
                    else:
                        sg = SendGridAPIClient(sendgrid_api_key)

                        subject = "Quick clarification on your interview availability"
                        html_body = f"""
                        <p>Hi {candidate_name},</p>
                        <p>Thank you for sharing your availability. We saw the following time options:</p>
                        <pre>{preview_times}</pre>
                        <p>Could you please confirm your <b>time zone</b> (for example, IST, PST, or your city)?<br>
                        Once we have your time zone, we'll schedule the interview and send you a calendar invite.</p>
                        <p>Best regards,<br/>Talent Team</p>
                        """

                        message = Mail(
                            from_email=from_email,
                            to_emails=candidate_email,
                            subject=subject,
                            html_content=HtmlContent(html_body),
                        )

                        # Route replies from this timezone clarification email back to the inbound webhook
                        if reply_to_email:
                            try:
                                message.reply_to = Email(reply_to_email)
                            except Exception:
                                logger.warning("Failed to set reply_to for timezone clarification email")

                        try:
                            sg.send(message)
                            logger.info(
                                f"Sent timezone clarification email to {candidate_email} instead of scheduling without timezone."
                            )
                        except Exception as send_ex:
                            logger.error(
                                f"Failed to send timezone clarification email to {candidate_email}: {send_ex}"
                            )

                except Exception as clar_ex:
                    logger.error(
                        f"Error while handling missing/unknown timezone for {candidate_email}: {clar_ex}"
                    )

                return {
                    "status": "error",
                    "message": "Timezone not provided; sent clarification email instead of scheduling",
                    "details": {"times": time_slots, "timezone": timezone},
                }

            # Resolve candidate phone (for Twilio call + DB) from Applicant Data.json map
            candidate_phone = None
            try:
                if hasattr(self, "email_to_phone") and self.email_to_phone:
                    candidate_phone = self.email_to_phone.get(candidate_email.strip().lower())
                if not candidate_phone:
                    logger.warning(
                        f"No phone number found for candidate email {candidate_email}; "
                        "will still schedule calendar + confirmation email but cannot schedule call."
                    )
            except Exception as phone_ex:
                logger.error(f"Error looking up phone for {candidate_email}: {phone_ex}")

            # Normalize phone number to E.164-like format so Twilio can call it
            if candidate_phone:
                try:
                    phone_str = str(candidate_phone).strip()
                    if phone_str.startswith("00"):
                        phone_str = "+" + phone_str[2:]
                    elif not phone_str.startswith("+"):
                        phone_str = "+" + phone_str
                    candidate_phone = phone_str
                except Exception as norm_ex:
                    logger.warning(f"Failed to normalize phone number '{candidate_phone}': {norm_ex}")

            # If we have a phone number, handle any existing interviews for this candidate.
            if candidate_phone:
                try:
                    from .models import InterviewSchedule, SessionLocal
                    from datetime import datetime as dt_datetime
                    import pytz

                    db = SessionLocal()
                    try:
                        email_lower = (email_content or "").lower()
                        wants_reschedule_global = bool(availability.get("reschedule_intent")) or (
                            "reschedul" in email_lower
                        )

                        # If this email clearly expresses reschedule intent, cancel all active
                        # interviews for this phone so the new time can be scheduled.
                        if wants_reschedule_global:
                            try:
                                existing_all = (
                                    db.query(InterviewSchedule)
                                    .filter(
                                        InterviewSchedule.candidate_phone == candidate_phone,
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
                                            "Email reschedule: cancelled existing interview for %s at %s (%s).",
                                            candidate_phone,
                                            row.scheduled_datetime,
                                            row.timezone or "UTC",
                                        )
                                    except Exception as _row_ex:
                                        logger.warning(
                                            f"Email reschedule: failed to cancel existing interview row for {candidate_phone}: {_row_ex}"
                                        )

                                db.commit()
                            except Exception as _cancel_ex:
                                logger.error(
                                    f"Email reschedule: error while cancelling existing interviews for {candidate_phone}: {_cancel_ex}"
                                )

                        else:
                            # No reschedule intent: check if a future interview already exists and,
                            # if so, send an 'already scheduled' notification.
                            existing = (
                                db.query(InterviewSchedule)
                                .filter(
                                    InterviewSchedule.candidate_phone == candidate_phone,
                                    InterviewSchedule.status.in_(["scheduled", "in_progress"]),
                                )
                                .order_by(InterviewSchedule.scheduled_datetime.asc())
                                .first()
                            )

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

                                    now_local = dt_datetime.utcnow().replace(tzinfo=pytz.utc).astimezone(tzinfo)
                                    now_local_naive = now_local.replace(tzinfo=None)

                                    # existing.scheduled_datetime is stored as naive local time
                                    if existing.scheduled_datetime < now_local_naive:
                                        logger.info(
                                            "Email pre-check: existing interview for %s at %s (%s) is in the past; "
                                            "ignoring it for 'already scheduled' logic.",
                                            candidate_phone,
                                            existing.scheduled_datetime,
                                            existing.timezone or "UTC",
                                        )
                                        existing = None
                                except Exception as _tz_ex0:
                                    logger.warning(
                                        f"Email pre-check: failed to compare existing interview time for {candidate_phone}: {_tz_ex0}"
                                    )

                            if existing:
                                logger.info(
                                    f"Candidate phone {candidate_phone} already has a scheduled interview at "
                                    f"{existing.scheduled_datetime} ({existing.timezone}); skipping new scheduling."
                                )

                                try:
                                    from sendgrid import SendGridAPIClient
                                    from sendgrid.helpers.mail import Mail, HtmlContent, Email

                                    sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
                                    from_email = os.getenv("SENDGRID_FROM_EMAIL") or "noreply@example.com"
                                    reply_to_email = os.getenv("SENDGRID_INBOUND_EMAIL")

                                    if sendgrid_api_key:
                                        sg = SendGridAPIClient(sendgrid_api_key)

                                        scheduled_str = existing.scheduled_datetime.strftime("%Y-%m-%d %I:%M %p")
                                        tz_label = existing.timezone or "UTC"

                                        subject = "Your interview is already scheduled"
                                        html_body = f"""
                                        <p>Hi {candidate_name},</p>
                                        <p>Your interview is already scheduled.</p>
                                        <p><b>Date/Time:</b> {scheduled_str} ({tz_label})</p>
                                        <p>If you need to reschedule, please reply with your new availability.</p>
                                        <p>Best regards,<br/>Talent Team</p>
                                        """

                                        message = Mail(
                                            from_email=from_email,
                                            to_emails=candidate_email,
                                            subject=subject,
                                            html_content=HtmlContent(html_body),
                                        )

                                        if reply_to_email:
                                            try:
                                                message.reply_to = Email(reply_to_email)
                                            except Exception:
                                                logger.warning(
                                                    "Failed to set reply_to for already-scheduled notification email"
                                                )

                                        try:
                                            sg.send(message)
                                            logger.info(
                                                f"Sent 'already scheduled' notification email to {candidate_email} "
                                                f"for phone {candidate_phone}."
                                            )
                                        except Exception as send_ex:
                                            logger.error(
                                                f"Failed to send 'already scheduled' email to {candidate_email}: {send_ex}"
                                            )

                                except Exception as notify_ex:
                                    logger.error(
                                        f"Error while notifying candidate {candidate_email} about existing schedule: {notify_ex}"
                                    )

                                return {
                                    "status": "already_scheduled",
                                    "scheduled_time": existing.scheduled_datetime.isoformat(),
                                    "timezone": existing.timezone,
                                }

                    finally:
                        db.close()

                except Exception as db_ex:
                    logger.error(
                        f"Error while checking for existing interview schedule for {candidate_phone}: {db_ex}"
                    )

            # Try each time slot until we find one that we can parse; do NOT use Google Calendar.
            for i, time_slot in enumerate(time_slots, 1):
                try:
                    logger.info(
                        f"Attempting to use time slot {i}/{len(time_slots)} from plain email: {time_slot}"
                    )

                    start_time, end_time = self._parse_time_slot(time_slot, timezone)
                    if not start_time or not end_time:
                        logger.warning(f"Could not parse time slot from plain email: {time_slot}")
                        continue

                    # If the email uses relative terms like "today" or "tomorrow", trust those
                    # words over Gemini's inferred date. Mirror the realtime messaging behavior:
                    #
                    # - "today"  => use today's date in the candidate's timezone
                    # - "tomorrow" => use tomorrow's date in the candidate's timezone
                    try:
                        email_lower = email_content.lower()
                        if any(word in email_lower for word in ["today", "tomorrow"]):
                            import datetime as dt_module
                            from datetime import timezone as dt_timezone
                            from datetime import timedelta as dt_timedelta
                            import pytz

                            # Map common timezone abbreviations to pytz zones (keep in sync with _parse_time_slot)
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

                            now_utc = dt_module.datetime.now(dt_timezone.utc)
                            tz_name = tz_abbr_map.get(timezone.upper(), timezone or "UTC")
                            try:
                                tzinfo = pytz.timezone(tz_name)
                            except pytz.UnknownTimeZoneError:
                                tzinfo = pytz.timezone("UTC")

                            now_local = now_utc.astimezone(tzinfo)
                            candidate_local = start_time.astimezone(tzinfo)

                            # Decide base date purely from the explicit word, ignoring whatever
                            # date Gemini inferred.
                            base_date = now_local.date()
                            if "tomorrow" in email_lower and "today" not in email_lower:
                                base_date = base_date + dt_timedelta(days=1)

                            # Preserve the local time-of-day from the parsed slot
                            local_time = candidate_local.time()
                            new_start_naive = dt_module.datetime.combine(base_date, local_time)
                            new_start = tzinfo.localize(new_start_naive)
                            new_end = new_start + dt_timedelta(hours=1)

                            logger.info(
                                "Adjusted relative time expression from Gemini date %s to %s based on '%s' (%s)",
                                start_time.isoformat(),
                                new_start.isoformat(),
                                "tomorrow" if "tomorrow" in email_lower and "today" not in email_lower else "today",
                                timezone,
                            )
                            start_time, end_time = new_start, new_end
                    except Exception as adjust_ex:
                        logger.warning(f"Failed to adjust relative date for email time slot: {adjust_ex}")

                    logger.info(
                        f"Using parsed time for {candidate_email}: {start_time.isoformat()} to {end_time.isoformat()} ({timezone})"
                    )

                    # Schedule Twilio call + DB record using MessagingService if we have a phone number.
                    # As an extra safety, re-check the database *right before* scheduling to avoid
                    # double-booking if, for any reason, a previous check missed an existing schedule.
                    if candidate_phone:
                        try:
                            from .models import InterviewSchedule, SessionLocal
                            from datetime import datetime as dt_datetime
                            import pytz

                            db = SessionLocal()
                            try:
                                existing_final = (
                                    db.query(InterviewSchedule)
                                    .filter(
                                        InterviewSchedule.candidate_phone == candidate_phone,
                                        InterviewSchedule.status.in_(["scheduled", "in_progress"]),
                                    )
                                    .order_by(InterviewSchedule.scheduled_datetime.asc())
                                    .first()
                                )

                                # Ensure any existing schedule is actually in the future
                                if existing_final and existing_final.scheduled_datetime is not None:
                                    try:
                                        tz_label = (existing_final.timezone or "UTC").upper()
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

                                        now_local = dt_datetime.utcnow().replace(tzinfo=pytz.utc).astimezone(tzinfo)
                                        now_local_naive = now_local.replace(tzinfo=None)

                                        if existing_final.scheduled_datetime < now_local_naive:
                                            existing_final = None
                                    except Exception as _tz_ex2:
                                        logger.warning(
                                            f"Final pre-schedule comparison failed for {candidate_phone}: {_tz_ex2}"
                                        )

                                if existing_final:
                                    email_lower = (email_content or "").lower()
                                    wants_reschedule_final = bool(availability.get("reschedule_intent")) or (
                                        "reschedul" in email_lower
                                    )

                                    if wants_reschedule_final:
                                        # As a safety net, cancel this existing future interview
                                        # so that the central guard will permit the new time.
                                        try:
                                            existing_final.status = "cancelled"
                                            if hasattr(existing_final, "call_pickup"):
                                                existing_final.call_pickup = "No"
                                            db.commit()
                                            logger.info(
                                                f"Final pre-schedule: cancelled existing interview for {candidate_phone} "
                                                f"at {existing_final.scheduled_datetime} ({existing_final.timezone}) due to reschedule request."
                                            )
                                            existing_final = None
                                        except Exception as _final_cancel_ex:
                                            logger.error(
                                                f"Error cancelling existing_final interview for {candidate_phone}: {_final_cancel_ex}"
                                            )
                                    else:
                                        # Do not create a new schedule; instead, remind the candidate.
                                        logger.info(
                                            f"Pre-schedule check: candidate phone {candidate_phone} already has an "
                                            f"interview at {existing_final.scheduled_datetime} ({existing_final.timezone}); "
                                            "skipping new scheduling."
                                        )

                                        try:
                                            from sendgrid import SendGridAPIClient
                                            from sendgrid.helpers.mail import Mail, HtmlContent, Email

                                            sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
                                            from_email = os.getenv("SENDGRID_FROM_EMAIL") or "noreply@example.com"
                                            reply_to_email = os.getenv("SENDGRID_INBOUND_EMAIL")

                                            if sendgrid_api_key:
                                                sg = SendGridAPIClient(sendgrid_api_key)

                                                scheduled_str = existing_final.scheduled_datetime.strftime(
                                                    "%Y-%m-%d %I:%M %p"
                                                )
                                                tz_label = existing_final.timezone or "UTC"

                                                subject = "Your interview is already scheduled"
                                                html_body = f"""
                                                <p>Hi {candidate_name},</p>
                                                <p>Your interview is already scheduled.</p>
                                                <p><b>Date/Time:</b> {scheduled_str} ({tz_label})</p>
                                                <p>If you need to reschedule, please reply with your new availability.</p>
                                                <p>Best regards,<br/>Talent Team</p>
                                                """

                                                message = Mail(
                                                    from_email=from_email,
                                                    to_emails=candidate_email,
                                                    subject=subject,
                                                    html_content=HtmlContent(html_body),
                                                )

                                                if reply_to_email:
                                                    try:
                                                        message.reply_to = Email(reply_to_email)
                                                    except Exception:
                                                        logger.warning(
                                                            "Failed to set reply_to for already-scheduled notification email (final check)"
                                                        )

                                                try:
                                                    sg.send(message)
                                                    logger.info(
                                                        f"Final pre-schedule 'already scheduled' email sent to {candidate_email} "
                                                        f"for phone {candidate_phone}."
                                                    )
                                                except Exception as send_ex:
                                                    logger.error(
                                                        f"Failed to send final 'already scheduled' email to {candidate_email}: {send_ex}"
                                                    )

                                        except Exception as notify_ex2:
                                            logger.error(
                                                f"Error while sending final 'already scheduled' notice to {candidate_email}: {notify_ex2}"
                                            )

                                        return {
                                            "status": "already_scheduled",
                                            "scheduled_time": existing_final.scheduled_datetime.isoformat(),
                                            "timezone": existing_final.timezone,
                                        }

                                # No conflicting future interview; proceed to schedule the call.
                                from app.messaging_service import messaging_service

                                date_str = start_time.strftime("%Y-%m-%d")
                                time_str = start_time.strftime("%I:%M %p")

                                logger.info(
                                    f"Scheduling interview call for phone {candidate_phone} on {date_str} at {time_str} {timezone}"
                                )
                                scheduled_call = await messaging_service.schedule_interview_call(
                                    candidate_phone, date_str, time_str, timezone
                                )
                                if not scheduled_call:
                                    # Central guard (or another check) refused the call – do NOT
                                    # send a 'scheduled' confirmation. Instead, send a short
                                    # clarification/failure email and return a non-success status.
                                    logger.warning(
                                        f"Failed to schedule Twilio call for {candidate_phone} even though time was parsed."
                                    )

                                    try:
                                        from sendgrid import SendGridAPIClient
                                        from sendgrid.helpers.mail import Mail, HtmlContent, Email

                                        sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
                                        from_email = os.getenv("SENDGRID_FROM_EMAIL") or "noreply@example.com"
                                        reply_to_email = os.getenv("SENDGRID_INBOUND_EMAIL")

                                        if sendgrid_api_key:
                                            sg = SendGridAPIClient(sendgrid_api_key)

                                            subject = "We couldn't schedule your interview for that time"
                                            html_body = f"""
                                            <p>Hi {candidate_name},</p>
                                            <p>Thank you for sharing your availability.</p>
                                            <p>We weren't able to schedule a new interview for the time you mentioned. This can
                                            happen if you already have an interview booked or if the time has already passed.</p>
                                            <p>If you'd like to reschedule, please reply with a new future date, time, and timezone.</p>
                                            <p>Best regards,<br/>Talent Team</p>
                                            """

                                            message = Mail(
                                                from_email=from_email,
                                                to_emails=candidate_email,
                                                subject=subject,
                                                html_content=HtmlContent(html_body),
                                            )

                                            if reply_to_email:
                                                try:
                                                    message.reply_to = Email(reply_to_email)
                                                except Exception:
                                                    logger.warning(
                                                        "Failed to set reply_to for 'could not schedule' notification email"
                                                    )

                                            try:
                                                sg.send(message)
                                                logger.info(
                                                    f"Sent 'could not schedule' notification email to {candidate_email} "
                                                    f"for phone {candidate_phone}."
                                                )
                                            except Exception as send_ex:
                                                logger.error(
                                                    f"Failed to send 'could not schedule' email to {candidate_email}: {send_ex}"
                                                )

                                    except Exception as notify_failed_ex:
                                        logger.error(
                                            f"Error while notifying candidate {candidate_email} about failed scheduling: {notify_failed_ex}"
                                        )

                                    return {
                                        "status": "call_not_scheduled",
                                        "message": "Twilio call was not scheduled (likely due to existing future interview or past time)",
                                        "start_time": start_time.isoformat(),
                                        "timezone": timezone,
                                    }
                            finally:
                                db.close()

                        except Exception as call_ex:
                            logger.error(
                                f"Error scheduling Twilio call for {candidate_phone} from email flow: {call_ex}"
                            )

                    # At this point either there is no phone number (so we can only
                    # send email) or the Twilio call was successfully scheduled.
                    # Build a simple "event" object for the confirmation email
                    # (no Google Calendar) and return success.
                    event = {
                        "start_time": start_time.isoformat(),
                        "end_time": end_time.isoformat(),
                        "meeting_link": "",  # no video link since we're not using Calendar here
                        "event_id": None,
                    }

                    # Send confirmation email via SendGrid
                    try:
                        self._send_confirmation_email(
                            service=None,
                            to_email=candidate_email,
                            candidate_name=candidate_name,
                            event=event,
                        )
                        logger.info(
                            f"Confirmation email (no Google Calendar) sent to {candidate_email} from plain email flow"
                        )
                    except Exception as email_error:
                        logger.error(
                            f"Error sending confirmation email (plain email flow, no Calendar): {email_error}"
                        )
                        logger.error(traceback.format_exc())

                    # Return success with the chosen time
                    try:
                        # Mark outreach completed so we do not contact this candidate for other jobs.
                        from app.models import OutreachTracker
                        from app.database import SessionLocal

                        db2 = SessionLocal()
                        try:
                            # Email channel tracker
                            rows = (
                                db2.query(OutreachTracker)
                                .filter(
                                    OutreachTracker.channel == "email",
                                    OutreachTracker.contact == candidate_email.strip().lower(),
                                    OutreachTracker.status == "active",
                                )
                                .all()
                            )
                            # Also complete any SMS trackers for the resolved phone, if any
                            if candidate_phone:
                                rows += (
                                    db2.query(OutreachTracker)
                                    .filter(
                                        OutreachTracker.channel == "sms",
                                        OutreachTracker.contact == str(candidate_phone).strip(),
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
                                db2.commit()
                        finally:
                            db2.close()
                    except Exception as tracker_ex:
                        logger.error(
                            f"Failed to mark OutreachTracker completed after email scheduling for {candidate_email}: {tracker_ex}",
                            exc_info=True,
                        )

                    return {
                        "status": "success",
                        "start_time": event["start_time"],
                        "meeting_link": event["meeting_link"],
                        "event_id": event["event_id"],
                        "timezone": timezone,
                    }

                except Exception as e:
                    logger.error(f"Error processing time slot from plain email {time_slot}: {e}")
                    logger.error(traceback.format_exc())
                    continue

            logger.warning(
                f"Could not parse any time slot for {candidate_email} after trying {len(time_slots)} slots from plain email"
            )
            return {
                "status": "error",
                "message": "Could not parse any of the proposed time slots",
            }

        except Exception as e:
            logger.error(f"Unexpected error in schedule_from_plain_email: {e}")
            logger.error(traceback.format_exc())
            return {
                "status": "error",
                "message": "Unexpected error while processing email reply",
            }
    
    def _send_confirmation_email(self, service, to_email: str, candidate_name: str, event: Dict[str, Any]):
        """Send confirmation email with meeting details using SendGrid."""
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail, HtmlContent, Email
            import os
            
            # Extract start and end times from the event
            if 'start' in event and 'end' in event:
                # Handle case where event has 'start' and 'end' keys
                start = event['start'].get('dateTime') or event['start'].get('date')
                end = event['end'].get('dateTime') or event['end'].get('date')
                html_link = event.get('htmlLink', '#')
                meeting_link = event.get('meeting_link', html_link)
            else:
                # Handle case where event has direct datetime fields
                start = event.get('start_time', 'Not specified')
                end = event.get('end_time', 'Not specified')
                meeting_link = event.get('meeting_link', event.get('htmlLink', '#'))
            
            # Get SendGrid API key from environment variables
            sendgrid_api_key = os.getenv("SENDGRID_API_KEY")
            from_email = os.getenv("SENDGRID_FROM_EMAIL") or "noreply@example.com"
            reply_to_email = os.getenv("SENDGRID_INBOUND_EMAIL")
            
            if not sendgrid_api_key:
                logger.error("SENDGRID_API_KEY environment variable not set")
                raise ValueError("SendGrid API key not configured")
            
            # Format the message with proper indentation
            html_content = f"""<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ color: #1a73e8; }}
        .button {{ 
            background-color: #1a73e8; 
            color: white; 
            padding: 10px 20px; 
            text-decoration: none; 
            border-radius: 4px;
            display: inline-block;
            margin: 10px 0;
        }}
        .footer {{ margin-top: 20px; font-size: 0.9em; color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <h1 class="header">Interview Scheduled</h1>
        <p>Hello {candidate_name},</p>
        <p>Thank you for your availability. We have scheduled your interview as follows:</p>
        <p><strong>Date/Time:</strong> {start} to {end}</p>
        <div>
            <a href="{meeting_link}" class="button">Join Meeting</a>
        </div>
        <p>Please let us know if you need to reschedule or have any questions.</p>
        <div class="footer">
            <p>Best regards,<br>Interview Team</p>
        </div>
    </div>
</body>
</html>"""

            # Create SendGrid message
            message = Mail(
                from_email=from_email,
                to_emails=to_email,
                subject='Interview Scheduled',
                html_content=HtmlContent(html_content)
            )

            # Ensure replies to confirmation emails go back through Inbound Parse
            if reply_to_email:
                try:
                    message.reply_to = Email(reply_to_email)
                except Exception:
                    logger.warning("Failed to set reply_to for confirmation email")
            
            # Send the email
            sg = SendGridAPIClient(sendgrid_api_key)
            response = sg.send(message)
            
            logger.info(f"Confirmation email sent to {to_email} (Message ID: {response.headers.get('X-Message-Id', 'unknown')})")
            
        except Exception as e:
            error_msg = str(e)
            if hasattr(e, 'body') and e.body:
                try:
                    error_data = json.loads(e.body)
                    error_msg = error_data.get('errors', [{}])[0].get('message', error_msg)
                except:
                    pass
            logger.error(f"Error sending confirmation email to {to_email}: {error_msg}")
            logger.error(f"Event data: {json.dumps(event, default=str, indent=2)}")
            logger.error(traceback.format_exc())
            raise  # Re-raise the exception to be handled by the caller

    def extract_availability(self, email_content: str, candidate_email: str, interviewer_email: str) -> Dict[str, Any]:
        """
        Extract available time slots from email content and schedule the interview.
        
        Args:
            email_content: The email content to analyze
            candidate_email: Email of the candidate
            interviewer_email: Email of the interviewer
            
        Returns:
            Dictionary with scheduling result or error message
        """
        logger.info(f"Starting availability extraction for candidate: {candidate_email}")
        logger.debug(f"Email content: {email_content[:200]}...")  # Log first 200 chars of email
        
        try:
            logger.info("Initializing Gemini model for processing email")
            system_prompt = """Extract available time slots from the email text. 
            Return ONLY a JSON array of objects with 'date' (YYYY-MM-DD) and 'time_range' (HH:MM-HH:MM 24h format) keys. 
            If no clear availability is found, return an empty array [].
            Example: [{"date": "2025-12-01", "time_range": "14:00-15:00"}]
            """
            
            # First, extract the availability
            response = self.model.generate_content(
                [system_prompt, email_content],
                generation_config={
                    "temperature": 0.3,
                    "max_output_tokens": 500,
                }
            )
            
            # Extract and parse the JSON response
            content = response.text.strip()
            
            # Clean up the response to ensure it's valid JSON
            content = content.strip().strip('`').strip()
            if content.startswith('json'):
                content = content[4:].strip()
                
            # Try to parse the JSON
            try:
                slots = json.loads(content)
                if not slots or not isinstance(slots, list):
                    logger.warning("No valid time slots found in the email")
                    return {"status": "error", "message": "No valid time slots found in the email"}
                
                # For now, pick the first available slot
                # In a real app, you might want to implement slot selection logic
                selected_slot = slots[0]
                
                # Parse the date and time
                date_str = selected_slot.get('date')
                time_range = selected_slot.get('time_range', '').split('-')
                
                if len(time_range) != 2:
                    return {"status": "error", "message": "Invalid time range format"}
                
                start_time = datetime.strptime(f"{date_str} {time_range[0].strip()}", "%Y-%m-%d %H:%M")
                end_time = datetime.strptime(f"{date_str} {time_range[1].strip()}", "%Y-%m-%d %H:%M")
                
                # Schedule the interview
                schedule_request = ScheduleRequest(
                    candidate_email=candidate_email,
                    candidate_name=candidate_email.split('@')[0],  # Use email prefix as name
                    interviewer_email=interviewer_email,
                    interview_slot=InterviewSlot(
                        start_time=start_time,
                        end_time=end_time,
                        timezone="Asia/Kolkata"  # Adjust timezone as needed
                    ),
                    meeting_title="Interview Scheduled",
                    meeting_description="Thank you for your availability. We have scheduled your interview."
                )
                
                # Schedule the interview
                result = self.scheduler.schedule_interview(schedule_request)
                
                if "error" in result:
                    return {"status": "error", "message": result["error"], "details": result.get("details", "")}
                
                return {
                    "status": "success",
                    "meeting_link": result.get("meeting_link", ""),
                    "event_id": result.get("event_id"),
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat()
                }
            except json.JSONDecodeError as e:
                error_msg = f"Failed to parse availability from email: {e}\nRaw response: {content}"
                logger.error(error_msg)
                return {"status": "error", "message": "Could not understand the available time slots in your email."}
            
        except Exception as e:
            error_msg = f"Error processing email: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {"status": "error", "message": f"An error occurred while processing your email: {str(e)}"}

    def format_slots_for_display(self, slots: List[Dict[str, str]]) -> str:
        """Format available slots for display in the UI."""
        if not slots:
            return "No available slots found in the email."
            
        formatted = []
        for slot in slots:
            formatted.append(f"- {slot.get('date')} {slot.get('time_range', '')}")
        return "\n".join(formatted)

class GoogleCalendarScheduler:
    def __init__(self, credentials_path: str = None, disabled: bool = False):
        """Initialize the Google Calendar scheduler."""
        if credentials_path is None:
            # Default to .credentials/token.json in the project root
            self.credentials_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                '.credentials',
                'token.json'
            )
        else:
            self.credentials_path = credentials_path
            
        # Ensure the directory exists
        os.makedirs(os.path.dirname(self.credentials_path), exist_ok=True)

        if disabled or _alembic_running:
            self.creds = None
            self.service = None
            return

        self.creds = self._get_credentials()
        self.service = build('calendar', 'v3', credentials=self.creds) if self.creds else None

    def _get_credentials(self):
        """Get valid user credentials from storage or prompt for login."""
        creds = None
        
        # Check if credentials file exists and is accessible
        if os.path.exists(self.credentials_path):
            try:
                with open(self.credentials_path, 'r') as token_file:
                    token_data = json.load(token_file)
                    
                # Check if the token contains required fields
                if 'refresh_token' not in token_data:
                    logger.warning("No refresh token found in credentials file. Please re-authenticate.")
                    return None
                    
                creds = Credentials.from_authorized_user_file(self.credentials_path, SCOPES)
                logger.info("Successfully loaded credentials from file")
                
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in credentials file: {e}")
                return None
            except Exception as e:
                logger.error(f"Error loading credentials: {e}")
                return None
        else:
            logger.warning(f"Credentials file not found at: {self.credentials_path}")
            logger.warning("Please run google_auth.py to set up Google Calendar access")
            return None
        
        # If there are no (valid) credentials available
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing expired credentials...")
                try:
                    creds.refresh(Request())
                    logger.info("Successfully refreshed credentials")
                    
                    # Save the refreshed credentials
                    try:
                        with open(self.credentials_path, 'w') as token:
                            token.write(creds.to_json())
                        logger.info("Saved refreshed credentials")
                    except Exception as e:
                        logger.error(f"Error saving refreshed credentials: {e}")
                        
                except Exception as e:
                    logger.error(f"Error refreshing credentials: {e}")
                    logger.error("Please re-authenticate by running google_auth.py")
                    return None
            else:
                logger.warning("No valid credentials available. Please run google_auth.py to authenticate.")
                return None
                
        return creds

    def schedule_interview(self, request: ScheduleRequest) -> Dict[str, Any]:
        """
        Schedule an interview on Google Calendar.
        
        Args:
            request: ScheduleRequest with interview details
            
        Returns:
            Dictionary with event details or error message
        """
        if not self.service:
            error_msg = "Google Calendar service not initialized. Check credentials.\n"
            error_msg += "Please make sure you've run google_auth.py to set up OAuth2."
            return {"error": error_msg}
            
        try:
            # Format the time in RFC3339 format required by Google Calendar
            start_time = request.interview_slot.start_time.isoformat()
            end_time = request.interview_slot.end_time.isoformat()
            timezone = request.interview_slot.timezone
            
            event = {
                'summary': f"{request.meeting_title}: {request.candidate_name}",
                'description': request.meeting_description,
                'start': {
                    'dateTime': start_time,
                    'timeZone': timezone,
                },
                'end': {
                    'dateTime': end_time,
                    'timeZone': timezone,
                },
                'attendees': [
                    {'email': request.candidate_email},
                    {'email': request.interviewer_email},
                ],
                'reminders': {
                    'useDefault': True,
                },
                'conferenceData': {
                    'createRequest': {
                        'requestId': f"interview-{request.candidate_email}-{int(datetime.now().timestamp())}",
                        'conferenceSolutionKey': {'type': 'hangoutsMeet'}
                    }
                }
            }

            logger.info(f"Creating calendar event: {event}")
            
            event = self.service.events().insert(
                calendarId='primary',
                conferenceDataVersion=1,
                body=event,
                sendUpdates='all'
            ).execute()
            
            logger.info(f"Successfully created event: {event.get('id')}")
            
            return {
                "success": True,
                "meeting_link": event.get('hangoutLink', ''),
                "event_id": event.get('id'),
                "html_link": event.get('htmlLink', ''),
                "meeting_id": event.get('id'),
                "start_time": start_time,
                "end_time": end_time,
                "timezone": timezone
            }
            
        except Exception as e:
            logger.error(f"Error scheduling interview: {str(e)}", exc_info=True)
            return {
                "error": f"Failed to schedule interview: {str(e)}",
                "details": str(e)
            }

def parse_datetime(date_str: str, time_str: str) -> datetime:
    """Parse date and time strings into a datetime object."""
    try:
        # Handle different date formats
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                date = datetime.strptime(date_str, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Date format not recognized: {date_str}")
            
        # Handle time range like "14:00-15:00" or single time "14:00"
        if '-' in time_str:
            start_time_str = time_str.split('-')[0].strip()
        else:
            start_time_str = time_str.strip()
            
        # Parse time
        time = datetime.strptime(start_time_str, "%H:%M").time()
        
        # Combine date and time
        return datetime.combine(date.date(), time)
        
    except Exception as e:
        logger.error(f"Error parsing datetime: {str(e)}")
        raise
