import os
import logging
import json
import re
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List, Any, Union
from dateutil import parser, tz
from sqlalchemy.orm import Session

import requests
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
import google.generativeai as genai
from dotenv import load_dotenv
import pytz

# Local imports
from .transcript_manager import TranscriptManager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MessagingService:
    def __init__(self, db: Session):
        """Initialize the messaging service with database and required services."""
        self.db = db
        
        # Initialize conversation state storage
        self.conversation_state = {}  # Maps phone numbers to conversation states
        self.conversation_histories = {}  # Maps phone numbers to conversation histories
        self.scheduled_calls = {}  # Track scheduled calls
        self.conversations = {}  # Track active conversations
        
        # Default job description
        self.default_job_description = """
        Position: Senior Software Engineer (Python/Django)
        
        Key Responsibilities:
        - Develop and maintain high-quality software solutions
        - Collaborate with cross-functional teams
        - Write clean, maintainable, and efficient code
        - Participate in code reviews and team meetings
        
        Requirements:
        - 5+ years of experience with Python and Django
        - Strong problem-solving skills
        - Experience with database design and optimization
        - Excellent communication skills
        """
        
        # Load environment variables
        # Render/system environment variables take precedence over local .env.
        load_dotenv()
        
        # Initialize Twilio client
        self.twilio_account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        self.twilio_auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        self.twilio_phone_number = os.getenv('TWILIO_PHONE_NUMBER')
        
        # Initialize Twilio client if credentials are available
        if not all([self.twilio_account_sid, self.twilio_auth_token, self.twilio_phone_number]):
            logger.warning("Twilio credentials not found in environment variables. SMS and voice call functionality will be disabled.")
            self.twilio_client = None
        else:
            self.twilio_client = Client(self.twilio_account_sid, self.twilio_auth_token)
        
        # Get and clean the BASE_URL
        self.base_url = os.getenv('BASE_URL', 'http://localhost:8000').strip()
        if not self.base_url.startswith(('http://', 'https://')):
            self.base_url = f'https://{self.base_url}'
        self.base_url = self.base_url.rstrip('/')
        logger.info(f"Initialized with BASE_URL: {self.base_url}")
        
        # Initialize Gemini
        self.gemini_api_key = os.getenv('GOOGLE_API_KEY')
        self.gemini_model = None
        self.initialize_gemini()
    
    def initialize_gemini(self):
        """Initialize the Gemini model with the API key."""
        if not self.gemini_api_key:
            logger.warning("Gemini API key not found in environment variables. AI responses will be disabled.")
            return
            
        try:
            genai.configure(api_key=self.gemini_api_key)
            
            # Try to use gemini-2.5-flash-lite first
            try:
                self.gemini_model = genai.GenerativeModel('gemini-2.5-flash-lite')
                logger.info("Successfully initialized Gemini with model: gemini-2.5-flash-lite")
                return
            except Exception as e:
                logger.warning(f"Failed to initialize gemini-2.5-flash-lite: {e}. Trying other models...")
            
            # Fallback to other models if available
            for model_name in ['gemini-2.5-flash', 'gemini-pro']:
                try:
                    self.gemini_model = genai.GenerativeModel(model_name)
                    logger.info(f"Successfully initialized Gemini with model: {model_name}")
                    return
                except Exception as e:
                    logger.warning(f"Failed to initialize {model_name}: {e}")
            
            logger.error("Failed to initialize any Gemini model. AI responses will be disabled.")
            
        except Exception as e:
            logger.error(f"Error initializing Gemini: {e}")
            self.gemini_model = None

    def get_job_description(self) -> str:
        """Get the job description for the interview."""
        return """
        Position: Senior Software Engineer (Python/Django)
        
        Key Points:
        - Backend development with Python/Django
        - 5+ years experience required
        - Full-time position
        - Technical interview includes coding assessment
        """

    def process_incoming_message(self, from_number: str, message: str) -> str:
        """
        Process an incoming SMS message and generate a response.
        
        Args:
            from_number: The sender's phone number in E.164 format
            message: The message content
            
        Returns:
            str: The response message to send back
        """
        try:
            # Clean the message
            message = message.strip()
            
            # Get or initialize conversation state
            if from_number not in self.conversation_state:
                self.conversation_state[from_number] = {
                    'stage': 'greeting',
                    'timezone': None,
                    'availability': None,
                    'name': None,
                    'role': 'candidate',
                    'interview_type': 'technical',
                    'job_description': self.get_job_description()
                }
                
            state = self.conversation_state[from_number]
            
            # Initialize conversation history if needed
            if from_number not in self.conversation_histories:
                self.conversation_histories[from_number] = []
            
            # Add user message to history
            self.conversation_histories[from_number].append({
                'role': 'user',
                'content': message,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
            
            # Generate response using Gemini
            if self.gemini_model:
                try:
                    # Prepare conversation context
                    context = {
                        'current_stage': state['stage'],
                        'previous_messages': self.conversation_histories[from_number][-5:],  # Last 5 messages
                        'user_info': {
                            'phone': from_number,
                            'timezone': state.get('timezone'),
                            'name': state.get('name'),
                            'role': state.get('role')
                        }
                    }
                    
                    # Generate response using Gemini
                    # Get job description and format it
                    job_desc = state.get('job_description', 'No job description available')
                    
                    # For initial greeting, use a specific message if this is the first message in the conversation
                    if state['stage'] == 'greeting' and len(self.conversation_histories[from_number]) == 1:
                        # Enhanced time pattern matching
                        time_patterns = [
                            r'\d{1,2}\s*(?:am|pm|AM|PM)',  # Matches times like 9 PM, 10am
                            r'\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)?',  # Matches times like 9:00, 10:30 PM
                            r'\b(at|around|by|before|after)\s+\d{1,2}',  # Matches "at 9", "by 10"
                            r'\b(morning|afternoon|evening|night)\b',
                            r'\b(today|tomorrow|tonight)\b',
                            r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
                            r'available\s+(at|on|for)'
                        ]
                        
                        # Check if the message looks like an availability response
                        has_time_info = any(re.search(pattern, message.lower()) for pattern in time_patterns)
                        
                        # Also check for common time phrases
                        time_phrases = [
                            'can do', 'am free', "i'm free", "i'll be free", 
                            'works for me', 'is good', 'is okay', 'sounds good'
                        ]
                        has_time_info = has_time_info or any(phrase in message.lower() for phrase in time_phrases)
                        
                        # Look for timezone in the message
                        timezone_match = re.search(r'\b(EST|PST|CST|MST|IST|GMT|UTC\+[0-9]+|UTC-[0-9]+)\b', message.upper())
                        
                        if has_time_info or 'available' in message.lower():
                            # If time is mentioned, handle it
                            if timezone_match:
                                state['timezone'] = timezone_match.group(1)
                                state['proposed_time'] = message.strip()
                                response_text = f"Got it! You're available at {state['proposed_time']}. Is that correct?"
                                state['stage'] = 'confirming_time'
                            else:
                                # If time is mentioned but no timezone, store the time and ask for timezone
                                state['proposed_time'] = message.strip()
                                response_text = "Thanks! Could you please include your timezone? (e.g., 'EST', 'PST')"
                                state['stage'] = 'getting_timezone'
                        state['stage'] = 'getting_availability'
                    else:
                        # First, check if message contains a timezone
                        timezone_match = re.search(r'\b(EST|PST|CST|MST|IST|GMT|UTC\+[0-9]+|UTC-[0-9]+)\b', message.upper())
                        
                        if timezone_match:
                            state['timezone'] = timezone_match.group(1)
                            
                        # If we're in getting_availability stage
                        if state['stage'] == 'getting_availability':
                            # If they're asking about the role instead of providing availability
                            if any(phrase in message.lower() for phrase in ['what is the role', 'tell me about the role', 'role details', 'about the position']):
                                response_text = "I'll have someone from our team contact you with those details. Could you please share your availability for a call? (e.g., 'tomorrow at 2 PM EST')"
                                self.conversation_histories[from_number].append({
                                    'role': 'assistant',
                                    'content': response_text,
                                    'timestamp': datetime.now(timezone.utc).isoformat()
                                })
                                return response_text
                                
                            # If we already have availability and timezone, confirm the time
                            if state.get('proposed_time') and state.get('timezone'):
                                response_text = f"Got it! You're available {state['proposed_time']} {state['timezone']}. Is that correct?"
                                state['stage'] = 'confirming_time'
                                self.conversation_histories[from_number].append({
                                    'role': 'assistant',
                                    'content': response_text,
                                    'timestamp': datetime.now(timezone.utc).isoformat()
                                })
                                return response_text
                            # If we have a proposed time from a previous message, use it
                            if state.get('proposed_time') and not state.get('timezone'):
                                if timezone_match:
                                    state['timezone'] = timezone_match.group(1)
                                    response_text = f"Got it! You're available at {state['proposed_time']} {state['timezone']}. Is that correct?"
                                    state['stage'] = 'confirming_time'
                            else:
                                # If no timezone in message, ask for it
                                response_text = "Thanks! Could you please include your timezone? (e.g., 'tomorrow at 9 PM EST')"
                                state['stage'] = 'getting_timezone'
                            # If they ask about the role at any point
                            if any(phrase in message.lower() for phrase in ['what is the role', 'tell me about the role', 'role details', 'about the position']):
                                if state.get('proposed_time') and state.get('timezone'):
                                    response_text = "I'll have someone from our team contact you with those details. Are you available at the time we discussed?"
                                else:
                                    response_text = "I'll have someone from our team contact you with those details. Could you please share your availability for a call? (e.g., 'tomorrow at 2 PM EST')"
                                self.conversation_histories[from_number].append({
                                    'role': 'assistant',
                                    'content': response_text,
                                    'timestamp': datetime.now(timezone.utc).isoformat()
                                })
                                return response_text
                                
                            # If we're confirming the time
                            elif state['stage'] == 'confirming_time':
                                if any(word in message.lower() for word in ['yes', 'correct', 'right']):
                                    # User confirmed the time, initiate the interview call
                                    call_initiated = self.initiate_interview_call(from_number, state.get('name', 'there'))
                                    if call_initiated:
                                        response_text = "Great! I'll be calling you shortly for your interview. Please answer the call from this number."
                                    else:
                                        response_text = "I apologize, but I'm having trouble initiating the call. Please try again later or contact support."
                                    state['stage'] = 'completed'
                            else:
                                # User didn't confirm, ask for a different time
                                response_text = "No problem! When would you prefer to have the call?"
                                state['stage'] = 'getting_availability'
                        # Use Gemini for other stages
                        else:
                            job_desc = state.get('job_description', 'Senior Software Engineer (Python/Django)')
                            prompt = f"""
                            You are James Chandler, a recruiter at Radixsol. Your only task is to collect availability and schedule interviews.

                            Identity rules (critical):
                            - Never say you are an AI, a bot, an assistant, or a language model.
                            - If asked who you are, say: "I’m James Chandler from Radixsol."
                            
                            STRICT RULES TO FOLLOW:
                            1. NEVER provide any details about the role, company, or position
                            2. If asked ANYTHING about the role, ALWAYS respond with: "I'll have someone from our team contact you with those details. Could you please share your availability for a call?"
                            3. Only discuss scheduling and availability
                            4. Keep responses under 2 sentences
                            5. Never mention any specific job titles, technologies, or requirements
                            6. If the user asks about anything other than scheduling, politely redirect to scheduling
                            
                            Current conversation stage: {state['stage']}
                            
                            Previous messages:
                            {json.dumps(context['previous_messages'], indent=2)}
                            
                            Candidate's latest message: {message}
                            
                            Your response should ONLY be about scheduling. If they ask about anything else, say you'll have someone contact them with details.
                            """
                            response = self.gemini_model.generate_content(prompt)
                            response_text = response.text.strip()
                    
                    # Update conversation state based on response and user input
                    if 'timezone' in response_text.lower() and '?' in response_text:
                        state['stage'] = 'getting_timezone'
                    elif 'available' in response_text.lower() and '?' in response_text:
                        state['stage'] = 'getting_availability'
                    
                    # If we've just confirmed a date, update the state
                    if any(confirm_word in message.lower() for confirm_word in ['yes', 'correct', 'right', 'that works']):
                        if 'december 13' in response_text.lower() and '9 pm' in response_text.lower():
                            state['confirmed_slot'] = 'December 13, 2025 at 9 PM EST'
                            response_text = "Great! I've scheduled your interview for December 13, 2025 at 9 PM EST. You'll receive a confirmation shortly. Looking forward to speaking with you!"
                            state['stage'] = 'completed'
                    
                    # Add assistant response to history
                    self.conversation_histories[from_number].append({
                        'role': 'assistant',
                        'content': response_text,
                        'timestamp': datetime.now(timezone.utc).isoformat()
                    })
                    
                    return response_text
                    
                except Exception as e:
                    logger.error(f"Error generating AI response: {e}")
                    return "I'm sorry, I encountered an error processing your message. Please try again later."
            else:
                return "I'm sorry, the AI service is not available right now. Please try again later."
                
        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
            return "I'm sorry, I encountered an error. Please try again later."
    
    def send_sms(self, to_number: str, message: str) -> bool:
        """
        Send an SMS message using Twilio.
        
        Args:
            to_number: The recipient's phone number in E.164 format (e.g., "+1234567890")
            message: The message to send
            
        Returns:
            bool: True if message was sent successfully, False otherwise
        """
        if not self.twilio_client:
            logger.warning("Twilio client not initialized. SMS sending is disabled.")
            return False

        try:
            message = self.twilio_client.messages.create(
                body=message,
                from_=self.twilio_phone_number,
                to=to_number
            )
            logger.info(f"SMS sent to {to_number}, SID: {message.sid}")
            return True
        except Exception as e:
            logger.error(f"Error sending SMS to {to_number}: {e}")
            return False
            
    def initiate_interview_call(self, to_number: str, candidate_name: str = "Candidate") -> bool:
        """Initiate an AI-powered interview call."""
        logger.info("Voice calling is disabled in spreadsheet outreach mode; refusing outbound call")
        return False

        # Legacy implementation retained below; unreachable while voice is disabled.
        if not self.twilio_client:
            logger.error("Twilio client not initialized. Cannot make call.")
            return False
        
        try:
            base_url = os.getenv('BASE_URL', 'http://localhost:8000').rstrip('/')
            call = self.twilio_client.calls.create(
                twiml=f"""<?xml version="1.0" encoding="UTF-8"?>
                <Response>
                    <Say voice="woman">
                        Hello {candidate_name}, thank you for joining us today. 
                        This is your AI-powered interview. 
                        You'll be asked several questions about your experience and skills.
                        Please wait while we connect you to the interview.
                    </Say>
                    <Redirect method="POST">{base_url}/api/interview/start</Redirect>
                </Response>""",
                to=to_number,
                from_=self.twilio_phone_number,
                record=True,
                status_callback=f"{base_url}/api/call/status",
                status_events=['initiated', 'ringing', 'answered', 'completed']
            )
            logger.info(f"Initiated interview call to {to_number}, Call SID: {call.sid}")
            return True
        except Exception as e:
            logger.error(f"Error initiating interview call: {e}")
            return False
    
    def generate_ai_response(self, prompt: str, context: str = "") -> str:
        """
        Generate a response using Gemini AI.
        
        Args:
            prompt: The user's message or prompt
            context: Additional context for the AI (optional)
            
        Returns:
            str: The AI-generated response
        """
        if not self.gemini_model:
            logger.error("Gemini model not initialized. Cannot generate AI response.")
            return "I'm sorry, the AI service is currently unavailable."
        
        try:
            full_prompt = f"{context}\n\nUser: {prompt}\n\nAssistant:"
            response = self.gemini_model.generate_content(full_prompt)
            return response.text
        except Exception as e:
            logger.error(f"Failed to generate AI response: {str(e)}")
            return "I'm sorry, I encountered an error while processing your request."

    def _parse_time_expression(self, text: str) -> Optional[Dict]:
        """
        Parse natural language time expressions into datetime objects with timezone support.
        Handles various formats like:
        - '3 PM today', 'at 5pm', 'around 2:30', '14:30'
        - 'tomorrow at 10am', 'next monday at 2pm'
        - 'this friday at 3pm', 'on tuesday at 9:30 AM'
        - 'in 2 days at 4pm', 'next week on wednesday at 11'
        - Timezone-aware: '2pm EST', '10:30 AM Pacific Time'
        """
        try:
            try:
                import pytz
                from dateutil import parser, relativedelta
                from dateutil.tz import gettz
            except ImportError as e:
                logger.error(f"Required package not found: {e}")
                return None
            
            # Common time expressions to handle
            time_expr = text.lower().strip()
            time_expr_original = time_expr  # Keep original for comparison
            local_tz = pytz.timezone('America/New_York')  # Default timezone, can be made configurable
            now = datetime.now(local_tz)
            logger.debug(f"Parsing time expression: {time_expr}")
            
            # Clean up the input text
            time_expr = re.sub(r'\b(?:at|around|about|near|by|on|@)\s+', ' ', time_expr)  # Remove common time prepositions
            time_expr = re.sub(r'\s+', ' ', time_expr).strip()  # Normalize whitespace
            
            # Handle relative day patterns
            relative_day_match = re.search(r'\b(today|tonight|tomorrow|yesterday|now|tonite|morn(?:ing)?|afternoon|evening|night|noon|midnight)\b', time_expr, re.IGNORECASE)
            
            # Handle "this [day] at [time]" pattern (e.g., "this friday at 2 pm")
            day_match = re.search(r'\b(this|next)?\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+(morning|afternoon|evening|night))?\s*(?:at|@)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?', time_expr, re.IGNORECASE)
            # Handle day of week patterns (e.g., "this friday at 2 pm" or "next monday")
            if day_match:
                try:
                    prefix = (day_match.group(1) or '').lower()
                    day_name = day_match.group(2).capitalize()
                    time_of_day = (day_match.group(3) or '').lower()
                    time_part = (day_match.group(4) or '').strip()
                    
                    logger.debug(f"Detected day pattern: {prefix} {day_name} {time_of_day} at {time_part}")
                    
                    # Calculate the target date
                    days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
                    target_weekday = days.index(day_name.lower())
                    today_weekday = now.weekday()
                    
                    # Calculate days ahead (0 = today, 1 = tomorrow, etc.)
                    days_ahead = (target_weekday - today_weekday) % 7
                    
                    # If it's 'next [day]' or if the day has already passed this week, go to next week
                    if prefix == 'next' or (days_ahead == 0 and not time_of_day and not time_part):
                        days_ahead = (days_ahead + 7) % 14  # Ensure we go to next week
                    
                    target_date = now + timedelta(days=days_ahead)
                    
                    # Handle time of day keywords if no specific time provided
                    if not time_part and time_of_day:
                        if time_of_day == 'morning':
                            time_part = '9:00 am'
                        elif time_of_day == 'afternoon':
                            time_part = '2:00 pm'
                        elif time_of_day in ['evening', 'night']:
                            time_part = '7:00 pm'
                        else:
                            time_part = '12:00 pm'  # Default to noon for other cases
                    
                    # Add default time if none provided
                    if not time_part:
                        time_part = '12:00 pm'  # Default to noon if no time specified
                    
                    # Add AM/PM if not specified
                    if 'am' not in time_part.lower() and 'pm' not in time_part.lower():
                        # If it's a 12-hour format without AM/PM, default to PM for single digits, AM for 12
                        if ':' in time_part:
                            hour = int(time_part.split(':')[0])
                            time_part += ' pm' if hour < 12 else ' am'
                        else:
                            time_part += ' pm'  # Default to PM if not specified
                    
                    # Parse the time
                    time_obj = parser.parse(time_part, fuzzy=True).time()
                    target_dt = datetime.combine(target_date.date(), time_obj)
                    
                    # Apply timezone
                    target_dt = local_tz.localize(target_dt)
                    
                    logger.debug(f"Parsed datetime: {target_dt}")
                    return {
                        'date': target_dt.strftime('%Y-%m-%d'),
                        'start_time': target_dt.strftime('%H:%M'),
                        'timezone': str(local_tz),
                        'raw_text': text
                    }
                except Exception as e:
                    logger.warning(f"Error parsing day pattern: {e}")
                    # Continue with normal parsing if this fails
            
            # Get the timezone mapping from the centralized method
            tz_mapping = self._get_tz_mapping()
            
            # Debug: Print detected timezone matches for testing
            if os.environ.get('DEBUG_TIMEZONE'):
                test_cases = [
                    "2pm EST",
                    "10:30 AM EST",
                    "tomorrow at 3pm EST",
                    "today 5:30pm EST",
                    "2pm Eastern",
                    "11:30am ET",
                    "9am America/New_York",
                    "4:30pm IST",
                    "1:00 PM PST",
                    "3:30pm GMT+5:30"
                ]
                
                print("\n=== Timezone Detection Test ===")
                for test in test_cases:
                    # Create pattern for testing
                    test_pattern = r'\b(' + '|'.join(re.escape(tz) for tz in tz_mapping.keys()) + r')\b'
                    tz_match = re.search(test_pattern, test, re.IGNORECASE)
                    if tz_match:
                        tz_abbr = tz_match.group(1).lower()
                        tz_name = tz_mapping.get(tz_abbr, 'Unknown')
                        print(f"Input: {test:20} | Detected: {tz_abbr.upper():5} | Resolved to: {tz_name}")
                    else:
                        print(f"Input: {test:20} | No timezone detected")
                print("==============================\n")
            
            # Extract timezone from text if present
            detected_tz = None
            tz_mapping = self._get_tz_mapping()
            
            # Enhanced timezone pattern that handles both abbreviations and common names
            tz_pattern = r'\b(' + '|'.join(
                re.escape(tz) + 
                (f'|{re.escape(tz.lower())}|{re.escape(tz.upper())}|{re.escape(tz.capitalize())}' if tz.isalpha() else '')
                for tz in tz_mapping.keys()
            ) + r')\b'
            
            # First try to find timezone at the end of the string (most common case)
            tz_match = re.search(f"({tz_pattern})\s*$", time_expr, re.IGNORECASE)
            if not tz_match:
                # If not at the end, try to find it anywhere in the string
                tz_match = re.search(tz_pattern, time_expr, re.IGNORECASE)
            
            # Check for GMT/UTC offsets (e.g., GMT+5:30, UTC-8)
            gmt_match = re.search(r'\b(GMT|UTC)([+-]\d{1,2}:?\d{0,2})\b', time_expr, re.IGNORECASE)
            if gmt_match:
                offset = gmt_match.group(2).replace(':', '')
                if offset == '+530' or offset == '+5:30' or offset == '+0530':
                    tz_abbr = 'IST'
                    tz_match = True  # Override any previous match
                elif offset == '+800' or offset == '+8:00' or offset == '+0800':
                    tz_abbr = 'SGT'  # Singapore Time
                    tz_match = True
                # Add more offset mappings as needed
                
            # Check for full timezone names (e.g., America/New_York)
            if not tz_match and '/' in time_expr:
                tz_parts = time_expr.lower().split()
                for part in tz_parts:
                    if '/' in part and part.count('/') == 1:
                        tz_abbr = part.replace('/', '_').lower()
                        if tz_abbr in tz_mapping:
                            tz_match = True
                            break
            
            # If we found a timezone, make sure it's a valid timezone name
            if tz_match and 'tz_abbr' not in locals():
                tz_abbr = tz_match.group(1).lower()
                
            # Handle common timezone variations
            if 'tz_abbr' in locals() and tz_abbr not in tz_mapping:
                # Try common variations
                tz_variations = {
                    'edt': 'est', 'eastern time': 'est', 'et': 'est',
                    'pdt': 'pst', 'pacific time': 'pst', 'pt': 'pst',
                    'cdt': 'cst', 'central time': 'cst', 'ct': 'cst',
                    'mdt': 'mst', 'mountain time': 'mst', 'mt': 'mst'
                }
                tz_abbr = tz_variations.get(tz_abbr, tz_abbr)
                
            if 'tz_abbr' in locals() and tz_abbr not in tz_mapping:
                # If the matched timezone still isn't in our mapping, log it and continue without timezone
                logger.warning(f"Unrecognized timezone abbreviation: {tz_abbr}")
                tz_match = None
            
            original_tz_abbr = None
            if tz_match or 'tz_abbr' in locals():
                if 'tz_abbr' not in locals():
                    tz_abbr = tz_match.group(1).lower()
                    original_tz_abbr = tz_match.group(1).upper()  # Save the original abbreviation
                try:
                    detected_tz = pytz.timezone(tz_mapping[tz_abbr])
                    # Remove timezone from text to avoid confusion in parsing
                    if tz_match and hasattr(tz_match, 'group'):
                        time_expr = re.sub(f"{re.escape(tz_match.group(1))}\s*$", '', time_expr, flags=re.IGNORECASE).strip()
                        if time_expr == time_expr_original:  # If no change, try removing from anywhere
                            time_expr = re.sub(re.escape(tz_match.group(1)), '', time_expr, flags=re.IGNORECASE).strip()
                except Exception as e:
                    logger.warning(f"Error processing timezone {tz_abbr}: {e}")
                    # If we can't process the timezone, continue without it
            
            # Handle relative time expressions (today, tomorrow, etc.)
            if relative_day_match:
                try:
                    day_keyword = relative_day_match.group(1).lower()
                    logger.debug(f"Detected relative day: {day_keyword}")
                    
                    # Calculate target date based on keyword
                    if day_keyword in ['today', 'tonight', 'tonite']:
                        target_date = now.date()
                        if not time_part:  # If no time specified, set based on time of day
                            if day_keyword in ['tonight', 'tonite']:
                                time_part = '7:00 pm'
                            else:
                                time_part = '2:00 pm'  # Default to afternoon for 'today'
                    elif day_keyword == 'tomorrow':
                        target_date = now.date() + timedelta(days=1)
                        time_part = time_part or '2:00 pm'  # Default to afternoon
                    elif day_keyword == 'yesterday':
                        target_date = now.date() - timedelta(days=1)
                        time_part = time_part or '2:00 pm'
                    elif day_keyword in ['morning', 'morn']:
                        target_date = now.date()
                        time_part = '9:00 am'
                    elif day_keyword == 'afternoon':
                        target_date = now.date()
                        time_part = '2:00 pm'
                    elif day_keyword in ['evening', 'night']:
                        target_date = now.date()
                        time_part = '7:00 pm'
                    elif day_keyword == 'noon':
                        target_date = now.date()
                        time_part = '12:00 pm'
                    elif day_keyword == 'midnight':
                        target_date = now.date()
                        time_part = '12:00 am'
                    else:
                        target_date = now.date()  # Default to today
                        time_part = time_part or '2:00 pm'
                    
                    # If we have a time part in the text, use it
                    time_match = re.search(r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)', time_expr, re.IGNORECASE)
                    if time_match and not time_part:
                        time_part = time_match.group(1).strip()
                    
                    # Add AM/PM if not specified
                    if time_part and 'am' not in time_part.lower() and 'pm' not in time_part.lower():
                        if ':' in time_part:
                            hour = int(time_part.split(':')[0])
                            time_part += ' pm' if hour < 12 else ' am'
                        else:
                            time_part += ' pm'  # Default to PM if not specified
                    
                    # Parse the time
                    time_obj = parser.parse(time_part, fuzzy=True).time()
                    target_dt = datetime.combine(target_date, time_obj)
                    
                    # Apply timezone
                    target_dt = local_tz.localize(target_dt)
                    
                    return {
                        'date': target_dt.strftime('%Y-%m-%d'),
                        'start_time': target_dt.strftime('%H:%M'),
                        'timezone': str(local_tz),
                        'raw_text': text
                    }
                except Exception as e:
                    logger.warning(f"Error parsing relative day: {e}")
                    # Continue with normal parsing
                
            # Handle standalone time patterns (e.g., "2pm", "3:30", "10 am")
            standalone_time = re.search(r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b', time_expr, re.IGNORECASE)
            if standalone_time and not any(x in time_expr.lower() for x in ['today', 'tomorrow', 'yesterday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']):
                try:
                    time_str = standalone_time.group(1).strip()
                    
                    # Add AM/PM if not specified
                    if 'am' not in time_str.lower() and 'pm' not in time_str.lower():
                        if ':' in time_str:
                            hour = int(time_str.split(':')[0])
                            time_str += ' pm' if hour < 12 else ' am'
                        else:
                            time_str += ' pm'  # Default to PM if not specified
                    
                    time_obj = parser.parse(time_str, fuzzy=True).time()
                    
                    # Assume today's date if not specified
                    target_dt = datetime.combine(now.date(), time_obj)
                    
                    # If the time has already passed today, assume tomorrow
                    if target_dt < now and (now - target_dt).total_seconds() > 3600:  # 1 hour buffer
                        target_dt += timedelta(days=1)
                    
                    target_dt = local_tz.localize(target_dt)
                    
                    return {
                        'date': target_dt.strftime('%Y-%m-%d'),
                        'start_time': target_dt.strftime('%H:%M'),
                        'timezone': str(local_tz),
                        'raw_text': text
                    }
                except Exception as e:
                    logger.warning(f"Error parsing standalone time: {e}")
                    # Continue with normal parsing
                
                try:
                    # Try to parse the time part with timezone
                    parsed_time = parser.parse(time_part, fuzzy=True, default=now)
                    if detected_tz:
                        parsed_time = detected_tz.localize(parsed_time.replace(tzinfo=None))
                    
                    return {
                        "date": parsed_time.strftime("%Y-%m-%d"),
                        "start_time": parsed_time.strftime("%H:%M"),
                        "end_time": (parsed_time + timedelta(hours=1)).strftime("%H:%M"),
                        "timezone": str(parsed_time.tzinfo) if parsed_time.tzinfo else (detected_tz.zone if detected_tz else local_tz.zone),
                        "raw_text": text
                    }
                except Exception as e:
                    logger.warning(f"Error parsing time expression: {e}")
                    pass
            
            # Handle "tomorrow at X" patterns
            if "tomorrow" in time_expr:
                time_part = time_expr.replace("tomorrow", "").strip()
                # Calculate tomorrow's date in the target timezone
                target_tz = detected_tz if detected_tz else local_tz
                
                # Get current time in the target timezone
                if hasattr(datetime, 'now') and target_tz:
                    now_in_tz = datetime.now(target_tz)
                else:
                    now_in_tz = datetime.now()
                    if target_tz:
                        now_in_tz = target_tz.localize(now_in_tz)
                
                # Calculate tomorrow's date in the target timezone
                tomorrow = (now_in_tz + timedelta(days=1)).date()
                
                if not time_part:
                    # Get timezone abbreviation with original timezone if available
                    tz_abbr = self._get_tz_abbreviation(
                        target_tz.zone if hasattr(target_tz, 'zone') else str(target_tz),
                        original_abbr=original_tz_abbr if 'original_tz_abbr' in locals() else None
                    )
                    
                    return {
                        "date": tomorrow.strftime("%Y-%m-%d"),
                        "start_time": "09:00",
                        "end_time": "17:00",
                        "timezone": target_tz.zone if hasattr(target_tz, 'zone') else str(target_tz),
                        "timezone_abbr": tz_abbr,
                        "raw_text": text
                    }
                    
                try:
                    # Parse the time part
                    parsed_time = parser.parse(time_part, fuzzy=True)
                    
                    # Create a timezone-aware datetime for the target time
                    target_dt = datetime.combine(tomorrow, parsed_time.time())
                    
                    # Localize the datetime to the target timezone
                    if target_tz:
                        target_dt = target_tz.localize(target_dt)
                    
                    # If no specific time was provided, use default business hours
                    if parsed_time.time() == datetime.min.time():
                        target_dt = target_dt.replace(hour=9, minute=0)
                        end_time = target_dt.replace(hour=17, minute=0)
                    else:
                        end_time = target_dt + timedelta(hours=1)
                    
                    # Get timezone abbreviation with original timezone if available
                    tz_abbr = self._get_tz_abbreviation(
                        target_tz.zone if hasattr(target_tz, 'zone') else str(target_tz),
                        original_abbr=original_tz_abbr if 'original_tz_abbr' in locals() else None
                    )
                    
                    return {
                        "date": target_dt.strftime("%Y-%m-%d"),
                        "start_time": target_dt.strftime("%H:%M"),
                        "end_time": end_time.strftime("%H:%M"),
                        "timezone": target_tz.zone if hasattr(target_tz, 'zone') else str(target_tz),
                        "timezone_abbr": tz_abbr,
                        "raw_text": text
                    }
                except Exception as e:
                    logger.warning(f"Error parsing tomorrow time expression: {e}")
                    pass
                
            # Handle specific times like "2 p.m." with timezone support
            time_patterns = [
                (r'(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)\s*([a-z]+)?', '%I %p'),  # 2pm, 2:30pm, 2 p.m.
                (r'(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?\s*([a-z]+)?', '%I:%M %p')  # 2:30 p.m. ET or 14:30 PST
            ]

            for pattern, time_format in time_patterns:
                match = re.search(pattern, time_expr, re.IGNORECASE)
                if match:
                    groups = [g for g in match.groups() if g]
                    time_str = ' '.join(groups[:-1]) if len(groups) > 1 and groups[-1].lower() in tz_mapping else ' '.join(groups)
                    
                    # Check if timezone was in the match
                    tz_abbr = None
                    if len(groups) > 1 and groups[-1].lower() in tz_mapping:
                        tz_abbr = groups[-1].lower()
                    
                    try:
                        parsed_time = datetime.strptime(time_str, time_format)
                        
                        # Handle AM/PM
                        if 'p' in time_str.lower() and parsed_time.hour < 12:
                            parsed_time = parsed_time.replace(hour=parsed_time.hour + 12)
                        
                        # Set timezone if specified
                        if tz_abbr:
                            tz = pytz.timezone(tz_mapping[tz_abbr])
                            parsed_time = tz.localize(parsed_time.replace(tzinfo=None))
                            target_tz = tz  # Store the target timezone
                        elif detected_tz:
                            parsed_time = detected_tz.localize(parsed_time.replace(tzinfo=None))
                            target_tz = detected_tz
                        else:
                            target_tz = local_tz
                        
                        # If no date is specified, assume today if time is in the future, else tomorrow
                        target_date = now.astimezone(target_tz) if now.tzinfo else target_tz.localize(now)
                        if parsed_time.time() < target_date.time():
                            target_date = target_date + timedelta(days=1)
                        
                        # Combine the target date with the parsed time
                        parsed_time = target_tz.localize(datetime.combine(
                            target_date.date(), 
                            parsed_time.time()
                        ))
                        
                        # Create time slot with original timezone
                        timezone_str = target_tz.zone if hasattr(target_tz, 'zone') else str(target_tz)
                        return {
                            "date": parsed_time.strftime("%Y-%m-%d"),
                            "start_time": parsed_time.strftime("%H:%M"),
                            "end_time": (parsed_time + timedelta(hours=1)).strftime("%H:%M"),
                            "timezone": timezone_str,
                            "timezone_abbr": self._get_tz_abbreviation(timezone_str),
                            "raw_text": text
                        }
                    except Exception as e:
                        logger.warning(f"Error parsing time pattern: {e}")
                        continue

            return None

        except Exception as e:
            logger.error(f"Error in _parse_time_expression: {e}")
            return None

    def extract_availability(self, message: str) -> Dict:
        """Extract availability information from a free-text message using Gemini.
        
        Uses Gemini to understand and extract time/date information from natural language.
        
        Returns a dictionary with the following structure:
        {
          "has_availability": bool,
          "needs_clarification": bool,
          "clarification_question": str or None,
          "slots": [
            {
              "date": "YYYY-MM-DD" or None,
              "start_time": "HH:MM" or None,
              "end_time": "HH:MM" or None,
              "timezone": str or None,
              "raw_text": str,
              "confidence": float
            }
          ]
        }
        """
        try:
            if not self.gemini_model:
                logger.warning("Gemini model not available for availability extraction")
                return {
                    "has_availability": False,
                    "needs_clarification": False,
                    "slots": []
                }
        except Exception as e:
            logger.error(f"Error in extract_availability: {e}")
            return {
                "has_availability": False,
                "needs_clarification": True,
                "slots": []
            }
            
            # Create the instruction prompt for Gemini
            instruction = (
                "You are an assistant that extracts interview availability from SMS messages. "
                "Read the candidate's message and infer any concrete time windows when they are available "
                "for a 30-60 minute interview. "
                "Current date and time: " + datetime.now().strftime("%Y-%m-%d %H:%M %Z") + "\n\n"
                "Respond in JSON format with these fields:\n"
                "{\n"
                "  \"has_availability\": boolean,\n"
                "  \"needs_clarification\": boolean,\n"
                "  \"clarification_question\": string or null,"
            "  \"slots\": [\n"
            "    {\n"
            "      \"date\": \"YYYY-MM-DD\" or null,\n"
            "      \"start_time\": \"HH:MM\" or null,\n"
            "      \"end_time\": \"HH:MM\" or null,\n"
            "      \"timezone\": string or null,\n"
            "      \"raw_text\": string,\n"
            "      \"confidence\": number between 0 and 1\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            "If the message contains relative times like 'today', 'tomorrow', or 'next week', "
            "convert them to specific dates. If no time is specified, assume a 1-hour slot. "
            "If the message is ambiguous or needs clarification, set needs_clarification to true and "
            "provide a clarification_question. If you cannot find any clear availability, set "
            "has_availability to false and slots to []"
        )

        # Format the full prompt with the message and current context
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M %Z")
        full_prompt = (
            f"{instruction}\n\n"
            f"Candidate message: {message}\n\n"
            f"Current time: {current_time}"
        )
        
        # Log the request
        logger.info(f"Sending to Gemini: {full_prompt}")
        
        # Generate the response
        response = self.gemini_model.generate_content(full_prompt)
        # Get and clean the response
        text = (response.text or "").strip()
        logger.info(f"Gemini raw response: {text}")
        
        # Clean up common response artifacts
        text = text.strip('`').lstrip('json\n').strip()
        
        # Sometimes Gemini includes markdown code blocks
        if text.startswith('```json'):
            text = text[7:]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()
        
        # Parse the JSON response
        try:
            data = json.loads(text)
            
            # Validate the response structure
            if not isinstance(data, dict):
                raise ValueError("Response is not a JSON object")
                
            # Set default values
            data.setdefault("has_availability", False)
            data.setdefault("needs_clarification", False)
            
        except Exception as e:
            logger.error(f"Error parsing Gemini response: {e}")
            return {
                "has_availability": False,
                "needs_clarification": True,
                "clarification_question": "I'm having trouble understanding your availability. Could you please provide your available times in a different format?",
                "slots": []
            }
            data.setdefault("clarification_question", None)
            data.setdefault("slots", [])
            
            # Ensure slots is a list
            if not isinstance(data.get("slots"), list):
                data["slots"] = []
                
            # Validate each slot
            for slot in data["slots"]:
                slot.setdefault("date", None)
                slot.setdefault("start_time", None)
                slot.setdefault("end_time", None)
                slot.setdefault("timezone", None)
                slot.setdefault("raw_text", message)
                slot.setdefault("confidence", 0.7)  # Default confidence
                
                # Convert date and time to proper format if they exist
                if slot["date"] and isinstance(slot["date"], str):
                    try:
                        # Try to parse and reformat the date
                        dt = datetime.strptime(slot["date"], "%Y-%m-%d")
                        slot["date"] = dt.strftime("%Y-%m-%d")
                    except (ValueError, TypeError):
                        slot["date"] = None
                        
                # Validate time format
                for time_field in ["start_time", "end_time"]:
                    if slot[time_field] and isinstance(slot[time_field], str):
                        try:
                            # Try to parse and reformat the time
                            time_obj = datetime.strptime(slot[time_field], "%H:%M")
                            slot[time_field] = time_obj.strftime("%H:%M")
                        except (ValueError, TypeError) as e:
                            logger.error(f"Error parsing time field: {e}")
                            slot[time_field] = None
                            
            # If no availability found, try to extract time patterns
            if not data["has_availability"] and not data["slots"]:
                # Look for simple time patterns in the message
                time_patterns = [
                    r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap]m\b|a\.?m\.?|p\.?m\.?)',  # 2pm, 2:30pm, 2 p.m.
                    r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\b',  # Just hours and optional minutes
                ]
                
                # Timezone mapping with common abbreviations and their IANA timezone names
                tz_mapping = [
                    # US Timezones
                    (r'\b(?:eastern(?:\s*time)?|et|est|edt)\b', 'America/New_York', 'ET'),
                    (r'\b(?:central(?:\s*time)?|ct|cst|cdt)\b', 'America/Chicago', 'CT'),
                    (r'\b(?:mountain(?:\s*time)?|mt|mst|mdt)\b', 'America/Denver', 'MT'),
                    (r'\b(?:pacific(?:\s*time)?|pt|pst|pdt)\b', 'America/Los_Angeles', 'PT'),
                    (r'\b(?:alaska(?:\s*time)?|akst|akdt)\b', 'America/Anchorage', 'AKT'),
                    (r'\b(?:hawaii(?:[-\s]aleutian)?\s*time|hst|hdt)\b', 'Pacific/Honolulu', 'HST'),
                    
                    # International Timezones
                    (r'\b(?:india(?:n)?\s*standard\s*time|ist)\b', 'Asia/Kolkata', 'IST'),
                    (r'\b(?:british\s*summer\s*time|bst|gmt|utc)\b', 'Europe/London', 'GMT'),
                    (r'\b(?:central\s*european\s*time|cet|cest)\b', 'Europe/Paris', 'CET'),
                    (r'\b(?:eastern\s*european\s*time|eet|eest)\b', 'Europe/Helsinki', 'EET'),
                    (r'\b(?:australian\s*eastern\s*time|aest|aedt)\b', 'Australia/Sydney', 'AEST'),
                    
                    # Common timezone offsets (e.g., UTC+5:30, GMT-8)
                    (r'\b(?:utc|gmt)\s*([+-]?\d{1,2}:?\d{0,2})\b', None, None),
                ]
                
                # Check for timezone in the message
                tz_found = False
                tz_name = None
                tz_abbr = None
                
                # First check for offset-based timezones (e.g., UTC+5:30, GMT-8)
                offset_match = re.search(r'\b(?:utc|gmt)\s*([+-]?\d{1,2}(?::?\d{2})?)\b', 
                                      message, re.IGNORECASE)
                if offset_match:
                    offset = offset_match.group(1)
                    # Convert offset to IANA timezone (simplified)
                    try:
                        hours = int(offset.replace(':', ''))
                        if -5 <= hours <= -4:
                            tz_name, tz_abbr = 'America/New_York', 'ET'
                        elif -6 <= hours < -5:
                            tz_name, tz_abbr = 'America/Chicago', 'CT'
                        elif -7 <= hours < -6:
                            tz_name, tz_abbr = 'America/Denver', 'MT'
                        elif -8 <= hours < -7:
                            tz_name, tz_abbr = 'America/Los_Angeles', 'PT'
                        elif 5 <= hours <= 6:
                            tz_name, tz_abbr = 'Asia/Kolkata', 'IST'
                        tz_found = True
                    except (ValueError, AttributeError):
                        pass
                
                # If no offset found, check for named timezones
                if not tz_found:
                    for pattern, tz, abbr in tz_mapping[:-1]:  # Skip the offset pattern we already checked
                        if re.search(pattern, message, re.IGNORECASE):
                            tz_name, tz_abbr = tz, abbr
                            tz_found = True
                            break
                
                # If we found a timezone, check for time patterns
                if tz_found:
                    for pattern in time_patterns:
                        if re.search(pattern, message, re.IGNORECASE):
                            return {
                                "has_availability": True,
                                "slots": [{
                                    "date": datetime.now().strftime("%Y-%m-%d"),
                                    "start_time": "09:00",
                                    "end_time": "17:00",
                                    "timezone": tz_name,
                                    "timezone_abbr": tz_abbr,
                                    "raw_text": message
                                }]
                            }
                
                # If no timezone specified, return has_availability=False to trigger timezone question
                return {
                    "has_availability": False,
                    "slots": [],
                    "needs_timezone": True,
                    "raw_text": message
                }
            
            return data
            
        except json.JSONDecodeError as je:
            logger.error(f"Failed to parse Gemini response as JSON: {je}")
            # Try to extract any time-like patterns as a last resort
            if any(word in message.lower() for word in ['am', 'pm', 'morning', 'afternoon', 'evening', 'today', 'tomorrow']):
                return {
                    "has_availability": True,
                    "slots": [{
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "start_time": "09:00",
                        "end_time": "17:00",
                        "timezone": "local",
                        "raw_text": message
                    }]
                }
            return {"has_availability": False, "slots": []}
            
        except Exception as e:
            logger.error(f"Error in extract_availability: {e}", exc_info=True)
            return {
                "has_availability": False,
                "needs_clarification": True,
                "error": str(e)
            }

def get_preferred_timezone_abbr(tz_info: str, original_abbr: str = None) -> str:
    """Get the preferred timezone abbreviation.
    
    Args:
        tz_info: The timezone string (e.g., 'America/New_York')
        original_abbr: The original timezone abbreviation from the message (e.g., 'EST')
        
    Returns:
        str: The preferred timezone abbreviation
    """
    # If we have the original abbreviation (e.g., 'EST'), use that
    if original_abbr and original_abbr.upper() in ['EST', 'EDT', 'CST', 'CDT', 'MST', 'MDT', 'PST', 'PDT']:
        return original_abbr.upper()
            
    # Timezone mapping
    tz_mapping = {
        'GMT': 'GMT',
        'UTC': 'UTC',
        'EST': 'EST',
        'EDT': 'EDT',
        'CST': 'CST',
        'CDT': 'CDT',
        'MST': 'MST',
        'MDT': 'MDT',
        'PST': 'PST',
        'PDT': 'PDT',
        'America/New_York': 'EST',
        'America/Chicago': 'CST',
        'America/Denver': 'MST',
        'America/Los_Angeles': 'PST',
        'America/Phoenix': 'MST',
        'America/Toronto': 'EST',
        'America/Vancouver': 'PST',
        'Europe/London': 'GMT',
        'Europe/Paris': 'CET',
        'Europe/Berlin': 'CET',
        'Europe/Madrid': 'CET',
        'Europe/Rome': 'CET',
        'Europe/Amsterdam': 'CET',
        'Europe/Athens': 'EET',
        'Europe/Helsinki': 'EET',
        'Asia/Kolkata': 'IST',
        'Asia/Calcutta': 'IST',
        'Asia/Tokyo': 'JST',
        'Asia/Seoul': 'KST',
        'Asia/Shanghai': 'CST',
        'Asia/Singapore': 'SGT',
        'Australia/Sydney': 'AEST',
        'Australia/Melbourne': 'AEST',
        'Pacific/Auckland': 'NZST',
        'Africa/Nairobi': 'EAT',
        'Africa/Cairo': 'EET',
        'Africa/Johannesburg': 'SAST',
        'America/Sao_Paulo': 'BRT',
        'America/Mexico_City': 'CST',
        'America/Argentina/Buenos_Aires': 'ART',
        'Asia/Dubai': 'GST',
        'Asia/Riyadh': 'AST',
        'Asia/Tehran': 'IRST',
        'Asia/Karachi': 'PKT',
        'Asia/Dhaka': 'BST',
        'Asia/Bangkok': 'ICT',
        'Asia/Ho_Chi_Minh': 'ICT',
        'Asia/Jakarta': 'WIB',
        'Asia/Manila': 'PHT',
        'Australia/Perth': 'AWST',
        'Australia/Adelaide': 'ACST',
        'Australia/Brisbane': 'AEST',
        'Australia/Darwin': 'ACST',
        'Pacific/Guam': 'ChST',
        'Pacific/Majuro': 'MHT',
        'Pacific/Tongatapu': 'TOT'
    }
        
    def _get_tz_mapping(self) -> Dict[str, str]:
        """Get the timezone mapping dictionary."""
        return {
            # North America
            'est': 'America/New_York',
            'edt': 'America/New_York',
            'et': 'America/New_York',
            'cst': 'America/Chicago',
            'cdt': 'America/Chicago',
            'mst': 'America/Denver',
            'mdt': 'America/Denver',
            'pst': 'America/Los_Angeles',
            'pdt': 'America/Los_Angeles',
            'pt': 'America/Los_Angeles',
            'akt': 'America/Anchorage',
            'hst': 'Pacific/Honolulu',
            'ast': 'America/Puerto_Rico',
            'nst': 'America/St_Johns',
            'eastern': 'America/New_York',
            'central': 'America/Chicago',
            'mountain': 'America/Denver',
            'pacific': 'America/Los_Angeles',
            'alaska': 'America/Anchorage',
            'hawaii': 'Pacific/Honolulu',
            
            # International
            'ist': 'Asia/Kolkata',
            'gmt': 'GMT',
            'utc': 'UTC',
            'bst': 'Europe/London',
            'cet': 'Europe/Paris',
            'cest': 'Europe/Paris',
            'eet': 'Europe/Helsinki',
            'eest': 'Europe/Helsinki',
            'jst': 'Asia/Tokyo',
            'kst': 'Asia/Seoul',
            'sgt': 'Asia/Singapore',
            'aest': 'Australia/Sydney',
            'aedt': 'Australia/Sydney',
            'nzst': 'Pacific/Auckland',
            'nzdt': 'Pacific/Auckland',
            'eat': 'Africa/Nairobi',
            'cat': 'Africa/Johannesburg',
            'wast': 'Africa/Windhoek',
            'adt': 'America/Halifax',
            'ndt': 'America/St_Johns',
            'awst': 'Australia/Perth',
            'acst': 'Australia/Adelaide',
            'acdt': 'Australia/Adelaide',
            'sydney': 'Australia/Sydney',
            'london': 'Europe/London',
            'paris': 'Europe/Paris',
            'berlin': 'Europe/Berlin',
            'moscow': 'Europe/Moscow',
            'dubai': 'Asia/Dubai',
            'singapore': 'Asia/Singapore',
            'hongkong': 'Asia/Hong_Kong',
            'tokyo': 'Asia/Tokyo',
            'seoul': 'Asia/Seoul',
            'beijing': 'Asia/Shanghai',
            'shanghai': 'Asia/Shanghai'
        }

    def _get_timezone_aware_response(self, message: str) -> str:
        """Generate a response that confirms timezone information when needed."""
        prompt = (
            "You're an AI assistant helping schedule interviews. The user has provided some availability, "
            "but didn't specify their timezone. Generate a friendly, natural response that asks for clarification "
            "about their timezone in a conversational way. Keep it professional but approachable. "
            "Example formats you can suggest: '2pm ET', '11am PST', '3:30pm your time'. "
            "Here's their message: " + message + "\n\nResponse:"
        )
        
        try:
            response = self.gemini_model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            logger.error(f"Error generating timezone confirmation: {e}")

    def _get_tz_abbreviation(self, tz_info: str, original_abbr: str = None) -> str:
        """Convert timezone info to a user-friendly abbreviation.
        
        Args:
            tz_info: The timezone string (e.g., 'America/New_York')
            original_abbr: The original timezone abbreviation from the message (e.g., 'EST')
            
        Returns:
            str: The preferred timezone abbreviation
        """
        # If we have the original abbreviation (e.g., 'EST'), use that
        if original_abbr and original_abbr.upper() in ['EST', 'EDT', 'CST', 'CDT', 'MST', 'MDT', 'PST', 'PDT']:
            return original_abbr.upper()
            
        # Timezone mapping
        tz_mapping = {
            'GMT': 'GMT',
            'UTC': 'UTC',
            'EST': 'EST',
            'EDT': 'EDT',
            'CST': 'CST',
            'CDT': 'CDT',
            'MST': 'MST',
            'MDT': 'MDT',
            'PST': 'PST',
            'PDT': 'PDT',
            'America/New_York': 'EST',
            'America/Chicago': 'CST',
            'America/Denver': 'MST',
            'America/Los_Angeles': 'PST',
            'America/Phoenix': 'MST',
            'America/Toronto': 'EST',
            'America/Vancouver': 'PST',
            'Europe/London': 'GMT',
            'Europe/Paris': 'CET',
            'Europe/Berlin': 'CET',
            'Europe/Madrid': 'CET',
            'Europe/Rome': 'CET',
            'Europe/Amsterdam': 'CET',
            'Europe/Athens': 'EET',
            'Europe/Helsinki': 'EET',
            'Asia/Kolkata': 'IST',
            'Asia/Calcutta': 'IST',
            'Asia/Tokyo': 'JST',
            'Asia/Seoul': 'KST',
            'Asia/Shanghai': 'CST',
            'Asia/Singapore': 'SGT',
            'Australia/Sydney': 'AEST',
            'Australia/Melbourne': 'AEST',
            'Pacific/Auckland': 'NZST',
            'Africa/Nairobi': 'EAT',
            'Africa/Cairo': 'EET',
            'Africa/Johannesburg': 'SAST',
            'America/Sao_Paulo': 'BRT',
            'America/Mexico_City': 'CST',
            'America/Argentina/Buenos_Aires': 'ART',
            'Asia/Dubai': 'GST',
            'Asia/Riyadh': 'AST',
            'Asia/Tehran': 'IRST',
            'Asia/Karachi': 'PKT',
            'Asia/Dhaka': 'BST',
            'Asia/Bangkok': 'ICT',
            'Asia/Ho_Chi_Minh': 'ICT',
            'Asia/Jakarta': 'WIB',
            'Asia/Manila': 'PHT',
            'Australia/Perth': 'AWST',
            'Australia/Adelaide': 'ACST',
            'Australia/Brisbane': 'AEST',
            'Australia/Darwin': 'ACST',
            'Pacific/Guam': 'ChST',
            'Pacific/Majuro': 'MHT',
            'Pacific/Tongatapu': 'TOT'
        }
        
        # First try direct match with timezone info
        tz_abbr = tz_mapping.get(tz_info, '')
        
        # If no direct match and it looks like a timezone name with /, try to extract
        if not tz_abbr and '/' in tz_info:
            # Try exact match with timezone name
            tz_abbr = tz_mapping.get(tz_info.split('/')[-1], '')
            
            # If still no match, try to generate an abbreviation
            if not tz_abbr:
                tz_parts = tz_info.split('/')[-1].replace('_', ' ').split()
                tz_abbr = ''.join(part[0] for part in tz_parts).upper()
        
        # If we have a valid abbreviation, return it in uppercase
        if tz_abbr:
            return tz_abbr.upper()
            
        # Fallback: return the last part of the timezone name in uppercase
        return tz_info.split('/')[-1].upper()

    def _create_natural_schedule_confirmation(self, availability: Dict) -> str:
        """Generate a natural-sounding confirmation of the scheduled time."""
        try:
            from datetime import datetime, timezone
            import pytz
            
            slot = availability['slots'][0]  # Take the first available slot
            
            # Get the timezone from the slot or use default
            tz_str = slot.get('timezone', 'America/New_York')
            if not tz_str or tz_str == 'UTC':
                tz_str = 'America/New_York'  # Default to ET if not specified
            
            try:
                tz = pytz.timezone(tz_str)
            except pytz.exceptions.UnknownTimeZoneError:
                tz = pytz.timezone('America/New_York')  # Fallback to ET
                tz_str = 'America/New_York'
                
            # Parse the date and time in the specified timezone
            if slot.get('date'):
                date_obj = datetime.strptime(slot['date'], '%Y-%m-%d')
                start_time = datetime.strptime(slot['start_time'], '%H:%M').time()
                localized_dt = tz.localize(datetime.combine(date_obj, start_time))
            else:
                # If no date, assume today/tomorrow based on current time
                now = datetime.now(tz)
                time_parts = list(map(int, slot['start_time'].split(':')))
                localized_dt = now.replace(hour=time_parts[0], minute=time_parts[1], second=0, microsecond=0)
                if localized_dt < now:
                    localized_dt += timedelta(days=1)
            
            # Format the time in a natural way (without timezone or day name)
            time_str = localized_dt.strftime('%-I:%M %p').lower()
            
            # Generate natural language confirmation (without timezone or day name)
            now = datetime.now(tz)
            if localized_dt.date() == now.date():
                when = f"today at {time_str}"
            elif localized_dt.date() == (now + timedelta(days=1)).date():
                when = f"tomorrow at {time_str}"
            else:
                when = f"on {localized_dt.strftime('%B %-d')} at {time_str}"
                
            # Add relative time if it's more than a day away
            time_until = (localized_dt - now).total_seconds()
            if time_until > 86400:  # More than 24 hours
                days_until = int(time_until / 86400)
                when += f" (in {days_until} days)"
            
            # Generate confirmation message
            confirmations = [
                f"Perfect! I've scheduled our call for {when}. Looking forward to our conversation! "
                "If anything changes, just let me know.",
                
                f"Got it! I've scheduled our call for {when}. I'll send you a reminder before we connect. "
                "If you need to reschedule, just say the word!",
                
                f"Great! I've got you down for {when}. I'm really looking forward to our chat. "
                "I'll be in touch if anything changes on my end.",
                
                f"All set! I've scheduled our call for {when}. If this time no longer works for you, "
                "please let me know and I'll be happy to adjust it.",
                
                f"I've got you scheduled for {when}. I'll send you a reminder before we connect. "
                "Looking forward to speaking with you then!"
            ]
            
            return random.choice(confirmations)
            
        except Exception as e:
            logger.error(f"Error creating schedule confirmation: {e}", exc_info=True)
            return "Thanks for sharing your availability! I've noted it and will be in touch with the details."

    def get_conversation_state(self, phone_number: str) -> Dict:
        """Get or initialize the conversation state for a phone number."""
        if phone_number not in self.conversation_state:
            self.conversation_state[phone_number] = {
                'last_interaction': datetime.now().isoformat(),
                'scheduled_calls': [],
                'scheduling_status': 'not_started',
                'timezone': 'America/New_York',  # Default timezone
                'conversation_history': []
            }
            # Initialize conversation history if not exists
            if phone_number not in self.conversation_histories:
                self.conversation_histories[phone_number] = []
        return self.conversation_state[phone_number]
        
    def update_conversation_state(self, phone_number: str, message: str, response: str):
        """Update conversation state with the latest exchange"""
        state = self.get_conversation_state(phone_number)
        state['last_message'] = message
        state['last_response'] = response
        state['conversation_history'].append({
            'timestamp': datetime.utcnow().isoformat(),
            'message': message,
            'response': response
        })
        # Keep only the last 10 messages to prevent memory issues
        if len(state['conversation_history']) > 10:
            state['conversation_history'] = state['conversation_history'][-10:]
    
    def process_incoming_message(self, from_number: str, message: str) -> str:
        """
        Process an incoming message with full Gemini integration for natural conversation.
        Maintains context and handles both job queries and scheduling naturally.
        """
        if not self.gemini_model:
            return "I'm currently experiencing technical difficulties. Please try again later."

        try:
            # Initialize conversation history if it doesn't exist
            if from_number not in self.conversation_histories:
                self.conversation_histories[from_number] = []
            
            conversation_history = self.conversation_histories[from_number]
            state = self.get_conversation_state(from_number)
            state['last_interaction'] = datetime.now().isoformat()
            
            # Handle timezone if we're waiting for it
            if state.get('awaiting_timezone'):
                timezone = self._extract_timezone(message)
                if timezone:
                    state['timezone'] = timezone
                    state['awaiting_timezone'] = False
                    
                    if 'pending_availability' in state:
                        availability = state.pop('pending_availability')
                        return self._process_availability(from_number, availability, "Thanks! I've noted your timezone.")
                    
                    return "Thanks! How can I assist you further?"
                else:
                    return "I couldn't recognize that timezone. Could you please specify it (e.g., EST, PST, UTC+5:30)?"
            
            # Prepare context for Gemini
            context = {
                "current_time": datetime.now().strftime("%Y-%m-%d %H:%M %Z"),
                "previous_messages": conversation_history[-5:],  # Last 5 exchanges
                "user_number": from_number,
                "timezone": state.get('timezone')
            }
            
            # Load job description
            job_description = self.load_job_description()
            
            # Prepare the prompt for Gemini
            prompt = f"""You are James Chandler, a recruiter at Radixsol, helping with job applications and interview scheduling.

Identity rules (critical):
- Never say you are an AI, a bot, an assistant, or a language model.
- If asked who you are, say: "I’m James Chandler from Radixsol."
Current time: {context['current_time']}

Job Description:
{job_description}

Previous conversation:
{self._format_conversation_history(conversation_history[-5:])}

User's message: {message}

Please respond naturally while:
1. Answering questions about the job/company using details from the job description
2. Collecting availability for scheduling
3. Asking clarifying questions when needed
4. Being friendly and professional
5. When discussing the role, reference specific requirements or responsibilities from the job description

If the user provides availability, extract and structure it in your response like this:
AVAILABILITY: {{"date": "YYYY-MM-DD", "time": "HH:MM", "timezone": "TZ"}}

Your response should be concise, relevant to the job, and focused on moving the conversation forward."""
            
            # Get response from Gemini
            response = self.gemini_model.generate_content(prompt)
            response_text = response.text.strip()
            
            # Process availability if present in the response
            if "AVAILABILITY:" in response_text:
                try:
                    availability_str = response_text.split("AVAILABILITY:", 1)[1].strip()
                    availability = json.loads(availability_str)
                    response_prefix = response_text.split("AVAILABILITY:", 1)[0].strip()
                    return self._process_availability(from_number, availability, response_prefix)
                except Exception as e:
                    logger.error(f"Error processing availability: {e}")
            
            # Update conversation history
            conversation_history.append({
                "role": "user",
                "content": message,
                "timestamp": datetime.utcnow().isoformat()
            })
            conversation_history.append({
                "role": "assistant",
                "content": response_text,
                "timestamp": datetime.utcnow().isoformat()
            })
            
            # Keep conversation history manageable
            if len(conversation_history) > 20:  # Last 10 exchanges
                conversation_history = conversation_history[-20:]
            
            return response_text
            
        except Exception as e:
            logger.error(f"Error in process_incoming_message: {e}", exc_info=True)
            return "I'm having trouble processing your request. Could you please rephrase or try again later?"
    
    def _format_conversation_history(self, history: List[Dict]) -> str:
        """Format conversation history for the prompt."""
        formatted = []
        for msg in history:
            role = "User" if msg["role"] == "user" else "Assistant"
            formatted.append(f"{role}: {msg['content']}")
        return "\n".join(formatted)

    def _process_availability(self, from_number: str, availability: Dict, response_prefix: str = "") -> str:
        """Process extracted availability and schedule a call."""
        try:
            state = self.get_conversation_state(from_number)
            timezone = availability.get('timezone') or state.get('timezone')
            
            if not timezone:
                state['pending_availability'] = availability
                state['awaiting_timezone'] = True
                return (
                    f"{response_prefix}\n\n"
                    "I noticed you didn't specify a timezone. Could you please let me know your timezone? "
                    "For example: 'I'm in New York' or 'My timezone is PST'"
                )
            
            date_str = availability.get('date')
            time_str = availability.get('time')
            if not date_str or not time_str:
                return "I couldn't determine the exact time. Could you please specify a date and time?"
            
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", '%Y-%m-%d %H:%M')
                tz = pytz.timezone(timezone)
                dt = tz.localize(dt)
                
                if self.schedule_call(from_number, dt, timezone):
                    formatted_time = dt.strftime("%A, %B %d at %I:%M %p")
                    if from_number in self.conversation_state:
                        del self.conversation_state[from_number]
                    return f"{response_prefix}\n\nGreat! I've scheduled the call for {formatted_time} ({timezone}). We'll call you then!"
                
                return "I'm sorry, I couldn't schedule the call. Please try again later."
                
            except Exception as e:
                logger.error(f"Error scheduling call: {e}")
                return "I'm having trouble scheduling that time. Could you try a different time?"
                
        except Exception as e:
            logger.error(f"Error processing availability: {e}", exc_info=True)
            return "I'm having trouble with scheduling right now. Could you please try again?"

    def _extract_timezone(self, text: str) -> Optional[str]:
        """Extract timezone from text using common patterns.
        
        Handles various timezone formats including:
        - Abbreviations (EST, PST, IST, etc.)
        - Common names (Eastern, Pacific, India, etc.)
        - UTC/GMT offsets (+5:30, -8, etc.)
        - Case-insensitive matching
        """
        try:
            # Convert to uppercase for case-insensitive matching
            text_upper = text.upper()
            
            # Common timezone patterns with case-insensitive matching
            tz_patterns = {
                # US Timezones
                r'\b(?:EST|EDT|ET|EASTERN(?:\s*STANDARD|\s*DAYLIGHT)?\s*TIME?)\b': 'America/New_York',
                r'\b(?:CST|CDT|CT|CENTRAL(?:\s*STANDARD|\s*DAYLIGHT)?\s*TIME?)\b': 'America/Chicago',
                r'\b(?:MST|MDT|MT|MOUNTAIN(?:\s*STANDARD|\s*DAYLIGHT)?\s*TIME?)\b': 'America/Denver',
                r'\b(?:PST|PDT|PT|PACIFIC(?:\s*STANDARD|\s*DAYLIGHT)?\s*TIME?)\b': 'America/Los_Angeles',
                r'\b(?:AKST|AKDT|AKT|ALASKA(?:\s*STANDARD|\s*DAYLIGHT)?\s*TIME?)\b': 'America/Anchorage',
                r'\b(?:HST|HDT|HT|HAWAII(?:-ALEUTIAN)?(?:\s*STANDARD|\s*DAYLIGHT)?\s*TIME?)\b': 'Pacific/Honolulu',
                
                # International Timezones
                r'\b(?:IST|INDIA(?:N)?\s*STANDARD\s*TIME?)\b': 'Asia/Kolkata',
                r'\b(?:GMT|UTC)([+-]\d{1,2}(?::?\d{2})?)?\b': 'Etc/GMT',  # Will handle offset later
                r'\b(?:UTC|GMT|GREENWICH(?:\s*MEAN)?\s*TIME?)\b': 'UTC',
                r'\b(?:CET|CEST|CENTRAL\s*EUROPE(?:AN)?(?:\s*STANDARD|\s*DAYLIGHT)?\s*TIME?)\b': 'Europe/Paris',
                r'\b(?:EET|EEST|EASTERN\s*EUROPE(?:AN)?(?:\s*STANDARD|\s*DAYLIGHT)?\s*TIME?)\b': 'Europe/Helsinki',
                r'\b(?:AEST|AEDT|AUSTRALIAN\s*EASTERN(?:\s*STANDARD|\s*DAYLIGHT)?\s*TIME?)\b': 'Australia/Sydney',
                r'\b(?:JST|JAPAN(?:ESE)?\s*STANDARD\s*TIME?)\b': 'Asia/Tokyo',
                r'\b(?:CST|CHINA\s*STANDARD\s*TIME?)\b': 'Asia/Shanghai',
                r'\b(?:SGT|SINGAPORE\s*TIME?)\b': 'Asia/Singapore',
                
                # Common city/country names
                r'\b(?:NEW\s*YORK|NYC?|NY\s*TIME)\b': 'America/New_York',
                r'\b(?:CHICAGO|CHI\s*TIME)\b': 'America/Chicago',
                r'\b(?:DENVER|MOUNTAIN\s*TIME|MT\s*TIME)\b': 'America/Denver',
                r'\b(?:LOS\s*ANGELES|LA|SEATTLE|SAN\s*FRANCISCO|SF|PACIFIC\s*TIME|PT\s*TIME)\b': 'America/Los_Angeles',
                r'\b(?:LONDON|LONDON\s*TIME|UK\s*TIME)\b': 'Europe/London',
                r'\b(?:PARIS|FRANCE|EUROPE(?:AN)?\s*TIME|CET|CEST)\b': 'Europe/Paris',
                r'\b(?:BERLIN|GERMAN(?:Y)?\s*TIME)\b': 'Europe/Berlin',
                r'\b(?:TOKYO|JAPAN(?:ESE)?\s*TIME)\b': 'Asia/Tokyo',
                r'\b(?:SYDNEY|MELBOURNE|AUSTRALIA(N)?\s*TIME)\b': 'Australia/Sydney',
                r'\b(?:MUMBAI|DELHI|BANGALORE|INDIA(N)?\s*TIME)\b': 'Asia/Kolkata',
                r'\b(?:BEIJING|SHANGHAI|CHINA\s*TIME)\b': 'Asia/Shanghai',
                r'\b(?:SINGAPORE|SINGAPORE\s*TIME|SGT)\b': 'Asia/Singapore',
                
                # Timezone offsets (e.g., UTC+5:30, GMT-8, etc.)
                r'\b(?:UTC|GMT)\s*([+-]\d{1,2}(?::?\d{2})?)\b': 'Etc/GMT'  # Will handle offset later
            }
            
            for pattern, tz_prefix in tz_patterns.items():
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    if tz_prefix == 'Etc/GMT':
                        # Handle GMT/UTC offsets (e.g., GMT+5:30, UTC-8)
                        offset = match.group(1)
                        if offset:
                            # Convert offset to IANA format
                            sign = '-' if offset[0] == '-' else '+'
                            hours = offset[1:].split(':')
                            hours_int = int(hours[0])
                            if sign == '-':
                                hours_int = -hours_int
                            return f'Etc/GMT{sign if hours_int > 0 else ""}{abs(hours_int)}'
                    return tz_prefix
                    
            return None
        except Exception as e:
            logger.error(f"Error extracting timezone: {e}")
            return None

    def _handle_scheduling(self, from_number: str, message: str) -> str:
        """Handle scheduling-related messages with timezone support."""
        try:
            # Get or initialize conversation state
            state = self.get_conversation_state(from_number)
            state['last_interaction'] = datetime.now().isoformat()
            
            # Check if we're waiting for timezone
            if state.get('awaiting_timezone'):
                timezone = self._extract_timezone(message)
                if timezone:
                    state['timezone'] = timezone
                    state['awaiting_timezone'] = False
                    # Store the updated state
                    self.conversation_state[from_number] = state
                    return "Thanks! When would you like to schedule the call?"
                return "I couldn't recognize that timezone. Could you please specify it (e.g., EST, PST, UTC+5:30)?"

            # Extract timezone if present
            timezone = self._extract_timezone(message)
            if timezone:
                state['timezone'] = timezone
                
            # Extract availability
            availability = self.extract_availability(message)
            
            if not availability:
                # If no availability found, ask for it
                return (
                    "I'd be happy to schedule a call! Could you let me know your availability? "
                    "For example: 'I'm available tomorrow at 2pm ET' or 'I can do Thursday morning'"
                )
                
            # If we have availability but no timezone, ask for it
            if availability.get("has_availability", False) and not state.get('timezone'):
                state['awaiting_timezone'] = True
                # Store the updated state
                self.conversation_state[from_number] = state
                return (
                    "I noticed you didn't specify a timezone. Could you please let me know your timezone? "
                    "For example: 'I'm in New York' or 'My timezone is PST'"
                )
                
            # Process the scheduling
            if availability.get("has_availability", False):
                slot = availability['slots'][0]
                timezone = state.get('timezone', 'UTC')  # Default to UTC if no timezone specified
                
                try:
                    # Parse the date and time
                    date_str = slot.get('date')
                    time_str = slot.get('start_time')
                    
                    if not date_str or not time_str:
                        return "I couldn't determine the exact time. Could you please specify a date and time?"
                    
                    # Parse the datetime
                    dt = datetime.strptime(f"{date_str} {time_str}", '%Y-%m-%d %H:%M')
                    
                    # Apply timezone if available
                    if timezone:
                        tz = pytz.timezone(timezone)
                        dt = tz.localize(dt)
                    
                    # Schedule the call
                    if self.schedule_call(from_number, dt, timezone):
                        # Format a nice confirmation message
                        formatted_time = dt.strftime("%A, %B %d at %I:%M %p")
                        # Clear the state after successful scheduling
                        if from_number in self.conversation_state:
                            del self.conversation_state[from_number]
                        return f"Great! I've scheduled the call for {formatted_time} ({timezone}). We'll call you then!"
                    else:
                        return "I'm sorry, I couldn't schedule the call. Please try again later."
                except Exception as e:
                    logger.error(f"Error scheduling call: {e}")
                    return "I'm having trouble scheduling that time. Could you try a different time?"
            
            return "I'm not sure I understand. Could you please provide a specific date and time?"
            
        except Exception as e:
            logger.error(f"Error in scheduling conversation: {e}", exc_info=True)
            return "I'm having trouble with scheduling right now. Could you please try again?"
    
    def _handle_job_question(self, message: str) -> str:
        """Handle job/role related questions with Gemini."""
        logger.info(f"Handling job question: {message}")
        
        try:
            job_description = self.load_job_description()
            
            # If no job description is available, return a helpful message
            if not job_description or "no job description" in (job_description or "").lower():
                logger.warning("No job description found")
                return (
                    "I'll need to get the latest details about this role for you. "
                    "In the meantime, I can help you schedule a call to discuss the position. "
                    "Would you be available for a quick chat this week?"
                )
                
            if not self.gemini_model:
                self.initialize_gemini()
                if not self.gemini_model:
                    return "I'm having trouble connecting to our AI service. Please try again later."
            
            prompt = f"""You are James Chandler from Radixsol, responding to job-related questions.

            Identity rules (critical):
            - Never say you are an AI, a bot, an assistant, or a language model.
            - If asked who you are, say: "I’m James Chandler from Radixsol."
            Here's the job description:
            {job_description}
            
            The candidate asked: {message}
            
            Please provide a helpful response based on the job description."""
            
            response = self.gemini_model.generate_content(prompt)
            return response.text.strip()
            
            logger.info("Job description loaded successfully")
            
            message_lower = message.lower()
            # Simple response for basic job description requests
            if any(phrase in message_lower for phrase in [
                "about role", "about job", "about position", 
                "what is the role", "tell me about the role", "role details"
            ]) or any(phrase == message_lower.strip().rstrip('?') for phrase in ["role", "job", "position"]):
                return (
                    f"Here's an overview of the position:\n\n{job_description}\n\n"
                    "Would you like to schedule a call to discuss this role in more detail?"
                )
            
            # If no specific question is asked, just return the job description
            if len(message.split()) <= 3:  # Very short messages like "tell me about role"
                return (
                    f"Here's what I can share about the position:\n\n{job_description}\n\n"
                    "Would you like to know anything specific about this role?"
                )
                
        except Exception as e:
            logger.error(f"Error handling job question: {e}")
            return (
                "I apologize, but I'm having trouble retrieving the job details at the moment. "
                "A team member will reach out to you with more information. "
                "In the meantime, would you like to schedule a call to discuss the position?"
            )
            
        # If we get here, it's a more specific question that might need Gemini
        if not hasattr(self, 'gemini_model') or not self.gemini_model:
            logger.warning("Gemini model not available, falling back to basic response")
            return (
                f"Here's what I can share about the position:\n\n{job_description}\n\n"
                "Would you like to schedule a call to discuss this further?"
            )
        
        try:
            # Only use Gemini for more specific questions
            prompt = (
                "You are a helpful recruiter assistant. The candidate is asking about the job/role. "
                "Use ONLY the following job description to answer their question. "
                "Be specific and provide clear, detailed information.\n\n"
                f"JOB DESCRIPTION:\n{job_description}\n\n"
                f"CANDIDATE'S QUESTION: {message}\n\n"
                "INSTRUCTIONS FOR YOUR RESPONSE:\n"
                "1. Start directly with the answer to their specific question\n"
                "2. If asking about responsibilities, list the key responsibilities clearly\n"
                "3. If asking about requirements, list the key requirements\n"
                "4. If asking about the role itself, provide a clear overview\n"
                "5. Keep responses concise but informative (1-2 short paragraphs)\n"
                "6. Sound natural and conversational, like a friendly recruiter\n"
                "7. DO NOT mention scheduling a call - that will be handled separately\n"
                "8. If you don't know the answer, say: \"I'll have a team member get back to you with that information.\"\n\n"
                "YOUR RESPONSE (start directly with the answer, no greetings):"
            )
            
            response = self.generate_ai_response(prompt).strip()
            if not response:
                raise ValueError("Empty response from AI model")
                
            # Add a natural follow-up about scheduling
            follow_ups = [
                "\n\nBy the way, when would be a good time for a quick chat about the role?",
                "\n\nWould you be available for a quick call this week to discuss further?",
                "\n\nI'd love to hear more about your experience. When might you be free for a quick chat?"
            ]
            return response + random.choice(follow_ups)
        except Exception as e:
            logger.error(f"Error generating AI response: {e}")
            return (
                f"Here's what I can share about the position:\n\n{job_description}\n\n"
                "Would you like to schedule a call to discuss this further?"
            )
        
        if not self.gemini_model:
            return (
                f"Here's what I can share about the position:\n\n{job_description}\n\n"
                "Would you like to schedule a call to discuss this further?"
            )
        
        # Only use Gemini for more specific questions
        prompt = (
            "You are a helpful recruiter assistant. The candidate is asking about the job/role. "
            "Use ONLY the following job description to answer their question. "
            "Be specific and provide clear, detailed information.\n\n"
            f"JOB DESCRIPTION:\n{job_description}\n\n"
            f"CANDIDATE'S QUESTION: {message}\n\n"
            "INSTRUCTIONS FOR YOUR RESPONSE:\n"
            "1. Start directly with the answer to their specific question\n"
            "2. If asking about responsibilities, list the key responsibilities clearly\n"
            "3. If asking about requirements, list the key requirements\n"
            "4. If asking about the role itself, provide a clear overview\n"
            "5. Keep responses concise but informative (1-2 short paragraphs)\n"
            "6. Sound natural and conversational, like a friendly recruiter\n"
            "7. DO NOT mention scheduling a call - that will be handled separately\n"
            "8. If you don't know the answer, say: \"I'll have a team member get back to you with that information.\"\n\n"
            "YOUR RESPONSE (start directly with the answer, no greetings):"
        )
        
        try:
            response = self.generate_ai_response(prompt).strip()
            # Add a natural follow-up about scheduling
            follow_ups = [
                "\n\nBy the way, when would be a good time for a quick chat about the role?",
                "\n\nWould you be available for a quick call this week to discuss further?",
                "\n\nI'd love to hear more about your experience. When might you be free for a quick chat?"
            ]
            return response + random.choice(follow_ups)
        except Exception as e:
            logger.error(f"Error generating job response: {e}")
            return "I'll have a team member get back to you with that information. Would you like to schedule a call to discuss further?"
            
    def _generate_gemini_response(self, message: str) -> str:
        """Generate a response using Gemini for job-related and other questions."""
        if not self.gemini_model:
            return "I'll need to check on that. In the meantime, would you like to schedule a call to discuss further?"
        
        job_description = self.load_job_description()
        
        prompt = (
            "You are a helpful recruiter assistant. Respond naturally to the candidate's message. "
            "If the question is about the job, use this job description to answer. "
            "If you don't know the answer, say you'll have someone get back to them.\n\n"
            f"JOB DESCRIPTION:\n{job_description if job_description else 'No job description available.'}\n\n"
            f"CANDIDATE'S MESSAGE: {message}\n\n"
            "RESPONSE GUIDELINES:\n"
            "1. If the message is a greeting, respond naturally and ask how you can help\n"
            "2. If asking about the job/role, provide specific details from the description\n"
            "3. If asking about requirements, list the key requirements from the description\n"
            "4. If asking about responsibilities, summarize the main responsibilities\n"
            "5. If the question is unclear or you don't know, say you'll have someone get back to them\n"
            "6. Keep responses concise (1-2 short paragraphs max)\n"
            "7. Sound natural and conversational, not robotic\n"
            "8. If appropriate, end with a question to continue the conversation\n\n"
            "YOUR RESPONSE:"
        )
        
        try:
            response = self.generate_ai_response(prompt)
            return response.strip()
        except Exception as e:
            logger.error(f"Error generating Gemini response: {e}")
            return "Thanks for your message! I'll have a team member get back to you with more information."
            
            # 3. Check if message contains availability information
            availability_keywords = ["available", "free", "schedule", "time", "when", "timing", "call", "interview", "meet", "chat"]
            time_patterns = [
                r'\b(1[0-9]|0?[1-9])(?::([0-5]\d))?\s*([ap]m\b|a\.?m\.?|p\.?m\.?)',
                r'\b(1[0-9]|0?[1-9])(?::([0-5]\d))?\b',
                r'\b(morning|afternoon|evening|noon|midday|midnight)\b',
                r'\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b'
            ]
            
            has_availability_keywords = any(word in message.lower() for word in availability_keywords)
            has_time_patterns = any(re.search(pattern, message.lower()) for pattern in time_patterns)
            
            if has_availability_keywords or has_time_patterns:
                # Extract availability from the message
                availability = self.extract_availability(message)
                
                if availability:
                    # Check if we need to ask for timezone
                    if availability.get("needs_timezone", False):
                        return self._get_timezone_aware_response(message)
                        
                    if availability.get("has_availability", False):
                        slot = availability['slots'][0]
                        timezone_specified = bool(slot.get('timezone'))
                        
                        if not timezone_specified:
                            # If no timezone, ask for clarification
                            return self._get_timezone_aware_response(message)
                        
                        # If we have timezone, confirm the schedule naturally
                        confirmation = self._create_natural_schedule_confirmation(availability)
                        
                        # Extract and schedule the call if we have all required info
                        if slot.get('start_time') and slot.get('timezone'):
                            try:
                                # Get the timezone
                                tz = pytz.timezone(slot['timezone'])
                                now = datetime.now(tz)
                                
                                # If we have a specific date in the slot, use it
                                if slot.get('date'):
                                    dt = datetime.strptime(slot['date'], '%Y-%m-%d')
                                    # Parse the time and combine with date
                                    time_parts = list(map(int, slot['start_time'].split(':')))
                                    dt = tz.localize(datetime(
                                        year=dt.year,
                                        month=dt.month,
                                        day=dt.day,
                                        hour=time_parts[0],
                                        minute=time_parts[1] if len(time_parts) > 1 else 0,
                                        second=0,
                                        microsecond=0
                                    ))
                                else:
                                    # No date specified, parse just the time
                                    time_parts = list(map(int, slot['start_time'].split(':')))
                                    hour = time_parts[0]
                                    minute = time_parts[1] if len(time_parts) > 1 else 0
                                    
                                    # Create datetime for today at the specified time
                                    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                                    
                                    # If the time has already passed today, schedule for tomorrow
                                    if dt <= now:
                                        dt += timedelta(days=1)
                                
                                # Schedule the call (without sending another confirmation)
                                self.schedule_call(from_number, dt, slot['timezone'])
                            except Exception as e:
                                logger.error(f"Error scheduling call: {e}")
                        
                        return confirmation
                else:
                    # Ask for specific availability in a more natural way
                    prompts = [
                        "I'd love to schedule a time to chat! What's your schedule looking like this week? "
                        "You can say something like 'Monday at 2pm ET' or 'I'm free Wednesday afternoon your time'.",
                        
                        "When would be a good time for a quick call? I'm available most afternoons ET, "
                        "but let me know what works best for you! Please include your timezone, like '3pm PT' or '11am your time'.",
                        
                        "I'd be happy to set up a time to talk. What's your availability like? "
                        "For example: 'I'm free tomorrow after 2pm' or 'Thursday morning works for me' - and please include your timezone!"
                    ]
                    return random.choice(prompts)
            
            # 3. Handle job-related queries
            job_info_keywords = ["job", "role", "position", "responsibilit", "requirement", "skill", "experience"]
            if any(word in message.lower() for word in job_info_keywords):
                job_description = self.load_job_description()
                if job_description and "no job description" not in job_description.lower():
                    prompt = (
                        f"The candidate is asking about the job. Here's the job description: {job_description}\n\n"
                        f"Candidate's question: {message}\n\n"
                        "Please provide a natural, conversational response as if you're a friendly recruiter. "
                        "Keep it brief and personable. If you don't know the answer, just say you'll check and get back to them. "
                        "Sound helpful and approachable, like you're having a casual chat."
                    )
                    response = self.generate_ai_response(prompt)
                    
                    # Add a natural follow-up about scheduling
                    follow_ups = [
                        "\n\nBy the way, when would be a good time for a quick chat about the role?",
                        "\n\nWould you be available for a quick call this week to discuss further?",
                        "\n\nI'd love to hear more about your experience. When might you be free for a quick chat?"
                    ]
                    return response + random.choice(follow_ups)
                else:
                    responses = [
                        "I'll check with the hiring manager about that and get back to you with the details. "
                        "In the meantime, when would be a good time for a quick call? I'm free most afternoons this week.",
                        
                        "Great question! Let me get the latest details from the team and circle back. "
                        "What's your schedule looking like for a quick chat?",
                        
                        "I'll need to double-check that with the hiring manager. "
                        "While I look into it, when might you be available for a quick call to discuss the role?"
                    ]
                    return random.choice(responses)
            
            # 4. Default response for other messages - more natural and varied
            default_responses = [
                "Thanks for getting in touch! I'd love to set up a time to chat about the position. "
                "What does your schedule look like this week?",
                
                "I appreciate your message! When would be a good time for a quick call? "
                "I'm pretty flexible, so just let me know what works for you.",
                
                "Got your message! I think a quick chat would be great. "
                "What's your availability looking like over the next few days?"
            ]
            return random.choice(default_responses)
            
        except Exception as e:
            logger.error(f"Error processing message: {str(e)}")
            error_responses = [
                "Oops! Something went wrong on our end, but don't worry - I've let the team know. "
                "In the meantime, when would be a good time for a quick chat about the role?",
                
                "Ah, I'm having a bit of a technical hiccup here. My team is looking into it now. "
                "While they sort that out, what's your schedule like for a quick call this week?",
                
                "I'm having a bit of trouble on my end - must be the Monday blues! "
                "The team is already on it. When might you be free for a quick chat about the position?"
            ]
            return random.choice(error_responses)
    
    def send_interview_reminder(self, phone_number: str, interview_details: Dict) -> bool:
        """
        Send an interview reminder via SMS.
        
        Args:
            phone_number: The recipient's phone number
            interview_details: Dictionary containing interview details
                            (e.g., {'date': '2023-12-15', 'time': '14:00', 'position': 'Software Engineer'})
        
        Returns:
            bool: True if reminder was sent successfully, False otherwise
        """
        message = (
            f"Reminder: Your interview for {interview_details.get('position', 'the position')} is scheduled for "
            f"{interview_details.get('date')} at {interview_details.get('time')}. "
            f"Join via: {interview_details.get('meeting_link', 'the provided link')}"
        )
        return self.send_sms(phone_number, message)

    def _make_call_async(self, phone_number: str, call_info: dict):
        """Helper method to make the actual call at the scheduled time."""
        logger.info("Scheduled voice calling is disabled in spreadsheet outreach mode")
        return False

        # Legacy implementation retained below; unreachable while voice is disabled.
        try:
            logger.info(f"Making scheduled call to {phone_number}")
            
            # Ensure base_url is set and use it for the callback URL
            callback_url = os.getenv('CALLBACK_URL', self.base_url)
            callback_url = callback_url.rstrip('/') + '/api/call/status'
            
            if not callback_url.startswith(('http://', 'https://')):
                logger.warning(f"Invalid callback URL: {callback_url}")
                return False
                
            logger.info(f"Using callback URL: {callback_url}")
            
            # Create a natural, conversational interview flow with TwiML
            interviewer_name = random.choice(['Sarah', 'Emily', 'Jessica', 'David', 'Michael'])
            company_name = 'Radixsol'
            position = 'the position'

            try:
                gather_timeout = int(os.getenv("TWILIO_GATHER_TIMEOUT", "6") or "6")
            except Exception:
                gather_timeout = 6
            gather_timeout = max(3, min(gather_timeout, 15))

            speech_timeout = (os.getenv("TWILIO_SPEECH_TIMEOUT", "2") or "2").strip()
            if speech_timeout.lower() != "auto":
                try:
                    speech_timeout = str(max(1, min(int(float(speech_timeout)), 10)))
                except Exception:
                    speech_timeout = "2"
            
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
            <Response>
                <Gather input="speech" action="{self.base_url}/api/voice/response" method="POST" timeout="{gather_timeout}" speechTimeout="{speech_timeout}" actionOnEmptyResult="true">
                    <Say voice="Polly.Joanna-Neural">
                        <prosody rate="medium" pitch="+5%">
                            Hi! This is {interviewer_name} calling from {company_name}.
                            <break time="1s"/>
                            
                            Thanks for taking my call. Is now still a good time to talk for a few minutes?
                            <break time="1s"/>
                            
                            Great. To start, could you tell me a little about your recent experience and what kind of role you're looking for right now?
                            <break time="1s"/>
                            
                            Take your time - I'm listening.
                        </prosody>
                    </Say>
                </Gather>
                
                <Gather input="speech" action="{self.base_url}/api/voice/followup" method="POST" timeout="{gather_timeout}" speechTimeout="{speech_timeout}" actionOnEmptyResult="true">
                    <Say voice="Polly.Joanna-Neural">
                        <prosody rate="medium" pitch="+5%">
                            Sorry about that - I didn't catch your response.
                            <break time="1s"/>
                            
                            Whenever you're ready, could you share a quick overview of your recent experience and what you're hoping for in your next role?
                        </prosody>
                    </Say>
                </Gather>
                
                <Say voice="Polly.Joanna-Neural">
                    <prosody rate="medium" pitch="+5%">
                        I think we're having some trouble with the connection.
                        I'll try again later. Thank you, and have a great day.
                    </prosody>
                </Say>
            </Response>"""
            
            # Create the call with the enhanced TwiML
            call = self.twilio_client.calls.create(
                twiml=twiml,
                to=phone_number,
                from_=self.twilio_phone_number,
                status_callback=callback_url,
                status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
                status_callback_method='POST',
                timeout=30,
                record=True,
                machine_detection='Enable',
                machine_detection_timeout=5,  # Timeout in seconds (1-10)
                send_digits='wwww1'  # Play .wav file first
            )
            
            # Update the call info with the call SID
            call_info['call_sid'] = call.sid
            call_info['status'] = 'in_progress'
            call_info['call_started_at'] = datetime.now(pytz.utc).isoformat()
            
            logger.info(f"Initiated call to {phone_number}, Call SID: {call.sid}")
            return True
            
        except Exception as e:
            logger.error(f"Error in scheduled call to {phone_number}: {e}")
            if 'call_info' in locals():
                call_info['status'] = 'failed'
                call_info['error'] = str(e)
            return False

    def schedule_call(self, phone_number: str, datetime_obj: datetime, detected_tz: str = None) -> bool:
        """Schedule a Twilio call for the specified time.
        
        This implementation uses threading.Timer to schedule the call for the future.
        For production use, consider using a task queue like Celery with Redis.
        """
        try:
            if not self.twilio_client:
                logger.error("Twilio client not initialized. Cannot schedule call.")
                return False
            
            # Ensure datetime_obj is timezone-aware
            if datetime_obj.tzinfo is None:
                tz = pytz.timezone(detected_tz) if detected_tz else pytz.timezone('America/New_York')
                datetime_obj = tz.localize(datetime_obj)
            
            current_time = datetime.now(pytz.utc)
            if datetime_obj < current_time:
                logger.warning(f"Cannot schedule call in the past: {datetime_obj}")
                return False
            
            # Calculate delay in seconds
            delay_seconds = (datetime_obj - current_time).total_seconds()
            
            # Store the scheduled call in conversation state
            state = self.get_conversation_state(phone_number)
            call_info = {
                'scheduled_time': datetime_obj.isoformat(),
                'timezone': detected_tz or 'America/New_York',
                'status': 'scheduled',
                'created_at': current_time.isoformat()
            }
            
            # Add to scheduled calls list and keep only the most recent ones
            if 'scheduled_calls' not in state:
                state['scheduled_calls'] = []
            state['scheduled_calls'].append(call_info)
            state['scheduled_calls'] = state['scheduled_calls'][-5:]  # Keep only last 5 scheduled calls
            
            # Schedule the call using a thread timer
            import threading
            timer = threading.Timer(
                delay_seconds, 
                self._make_call_async, 
                args=(phone_number, call_info)
            )
            timer.daemon = True  # Allow the program to exit even if the timer is still running
            timer.start()
            
            logger.info(f"Call to {phone_number} scheduled for {datetime_obj} (in {delay_seconds/60:.1f} minutes)")
            return True
            
        except Exception as e:
            logger.error(f"Error scheduling call: {e}", exc_info=True)
            return False
            state['scheduled_calls'].append(call_info)
            # Keep only the most recent 5 scheduled calls
            state['scheduled_calls'] = state['scheduled_calls'][-5:]
            
            # Format the time for display (remove leading zeros, handle 12-hour format)
            formatted_time = datetime_obj.strftime('%-I:%M %p').lstrip('0').replace(' 0', ' ')
            formatted_date = datetime_obj.strftime('%A, %B %d')
            tz_abbr = self._get_tz_abbreviation(datetime_obj.tzinfo.zone) if datetime_obj.tzinfo else (detected_tz or 'ET')
            
            confirmations = [
                f"Great! I've scheduled a call with you for {formatted_date} at {formatted_time} {tz_abbr}. We'll call you then!",
                f"Got it! I've got you down for {formatted_date} at {formatted_time} {tz_abbr}. Looking forward to speaking with you!",
                f"Perfect! I've got you scheduled for {formatted_date} at {formatted_time} {tz_abbr}. We'll give you a call then!"
            ]
            
            return random.choice(confirmations)
            
        except Exception as e:
            logger.error(f"Error in schedule_call: {str(e)}", exc_info=True)
            return "I'm having trouble scheduling that time. Could you try another time?"

    def initialize_gemini(self):
        """Initialize the Gemini model with the API key."""
        if not self.gemini_api_key:
            logger.warning("Gemini API key not found in environment variables. AI responses will be disabled.")
            return
            
        try:
            genai.configure(api_key=self.gemini_api_key)
            
            # List available models and log them
            available_models = genai.list_models()
            available_model_names = [m.name for m in available_models]
            logger.info(f"Available models: {available_model_names}")
            
            # Try different model names in order of preference
            preferred_models = [
                'gemini-1.5-pro-latest',  # Most capable model
                'gemini-1.0-pro-latest',  # General purpose model
                'gemini-pro'              # Legacy model name
            ]
            
            for model_name in preferred_models:
                # Find the full model name that matches our pattern
                full_model_name = next((name for name in available_model_names if model_name in name), None)
                if full_model_name:
                    try:
                        self.gemini_model = genai.GenerativeModel(
                            model_name=full_model_name,
                            generation_config={
                                "temperature": 0.7,
                                "top_p": 0.9,
                                "top_k": 40,
                                "max_output_tokens": 2048,
                            },
                        )
                        logger.info(f"Successfully initialized Gemini model: {full_model_name}")
                        return
                    except Exception as e:
                        logger.warning(f"Failed to initialize model {full_model_name}: {e}")
                        continue
                        
            logger.error("No suitable Gemini model could be initialized")
            
        except Exception as e:
            logger.error(f"Error initializing Gemini: {e}")
            self.gemini_model = None
        
        # Initialize conversation tracking
        self.conversations = {}  # Active conversations
        self.conversation_histories = {}  # Message history
        self.conversation_state = {}  # State tracking (timezone, etc.)
        
        logger.info(f"Initialized with BASE_URL: {self.base_url}")
        
        if not all([self.twilio_account_sid, self.twilio_auth_token, self.twilio_phone_number]):
            logger.warning("Twilio credentials not found in environment variables. SMS and voice call functionality will be disabled.")
            self.twilio_client = None
        else:
            self.twilio_client = Client(self.twilio_account_sid, self.twilio_auth_token)
        
        # Initialize Gemini
        self.gemini_api_key = os.getenv('GOOGLE_API_KEY')
        if not self.gemini_api_key:
            logger.warning("Gemini API key not found in environment variables. AI responses will be disabled.")
            self.gemini_model = None
        else:
            genai.configure(api_key=self.gemini_api_key)
            try:
                # List all available models
                available_models = genai.list_models()
                model_names = [model.name for model in available_models]
                logger.info(f"Available models: {model_names}")
                
                # Try to find a working model
                model_attempts = [
                    'gemini-2.5-flash-lite',
                    'models/gemini-2.5-flash-lite'
                ]
                
                for model_name in model_attempts:
                    try:
                        self.gemini_model = genai.GenerativeModel(
                            model_name=model_name,
                            generation_config={
                                "temperature": 0.7,
                                "top_p": 0.9,
                                "top_k": 40,
                                "max_output_tokens": 2048,
                            },
                        )
                        # Test the model with a simple prompt
                        test_response = self.gemini_model.generate_content("Test")
                        if test_response and hasattr(test_response, 'text'):
                            logger.info(f"Successfully initialized Gemini model: {model_name}")
                            break
                    except Exception as e:
                        logger.warning(f"Failed to initialize model {model_name}: {e}")
                else:
                    raise Exception(f"Failed to initialize any Gemini model. Available models: {model_names}")
                    
            except Exception as e:
                logger.error(f"Gemini initialization failed: {e}")
                self.gemini_model = None
        
        # Interview questions (you can customize these)
        self.interview_questions = [
            "Can you tell me a little about yourself and your experience?",
            "What interests you about this position?",
            "Can you describe a challenging project you've worked on?",
            "Where do you see yourself in five years?",
            "Do you have any questions for us?"
        ]
        
    def generate_interview_response(self, call_sid: str, user_input: str = "") -> str:
        """Generate the next question or response in the interview."""
        if call_sid not in self.conversations:
            # Initialize new conversation if it doesn't exist
            self.conversations[call_sid] = {
                'current_question': 0,
                'transcript': [],
                'responses': [],
                'start_time': datetime.now().isoformat(),
                'status': 'in_progress',
                'last_question': ""
            }
        
        conv = self.conversations[call_sid]
        
        # If we've asked all questions, end the interview
        if conv['current_question'] >= len(self.interview_questions):
            conv['status'] = 'completed'
            return ""  # Empty string will be handled by handle_voice_response
        
        # Get the next question
        question = self.interview_questions[conv['current_question']]
        conv['last_question'] = question
        
        # Only increment the question counter if we're not on the first question
        if conv['current_question'] > 0 or (conv['current_question'] == 0 and user_input):
            conv['current_question'] += 1
        
        return question
    
    def end_interview(self, call_sid: str) -> None:
        """Handle the end of an interview, saving the transcript and responses."""
        if call_sid in self.conversations:
            conv = self.conversations[call_sid]
            
            # Prepare interview data
            interview_data = {
                'call_sid': call_sid,
                'start_time': conv.get('start_time'),
                'end_time': datetime.now().isoformat(),
                'status': conv.get('status', 'completed'),
                'questions_asked': len(self.interview_questions),
                'questions_answered': len(conv.get('responses', [])),
                'transcript': "\n".join(conv.get('transcript', [])),
                'responses': conv.get('responses', []),
                'metadata': {
                    'system': 'automated_interview',
                    'version': '1.0'
                }
            }
            
            try:
                # Ensure transcripts directory exists
                os.makedirs('transcripts', exist_ok=True)
                
                # Save transcript to file
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f'transcripts/interview_{call_sid}_{timestamp}.json'
                
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(interview_data, f, indent=2, ensure_ascii=False)
                
                logger.info(f"Interview completed. Transcript saved to: {os.path.abspath(filename)}")
                
                # Log a summary of the interview
                logger.info(
                    f"Interview Summary - Questions: {interview_data['questions_answered']}/"
                    f"{interview_data['questions_asked']} answered, "
                    f"Duration: {self._format_duration(interview_data['start_time'], interview_data['end_time'])}"
                )
                
            except Exception as e:
                logger.error(f"Error saving interview transcript: {str(e)}", exc_info=True)
    
    def _format_duration(self, start_time: str, end_time: str) -> str:
        """Format the duration between two ISO timestamps as a human-readable string."""
        try:
            start = datetime.fromisoformat(start_time)
            end = datetime.fromisoformat(end_time)
            delta = end - start
            
            minutes, seconds = divmod(int(delta.total_seconds()), 60)
            hours, minutes = divmod(minutes, 60)
            
            parts = []
            if hours > 0:
                parts.append(f"{hours} hour{'s' if hours > 1 else ''}")
            if minutes > 0:
                parts.append(f"{minutes} minute{'s' if minutes > 1 else ''}")
            if seconds > 0 or not parts:  # Always show at least seconds if no other parts
                parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
                
            return ' '.join(parts)
        except Exception as e:
            logger.warning(f"Error formatting duration: {e}")
            return "unknown duration"
    
    def handle_voice_response(self, call_sid: str, digits: Optional[str] = None, speech_result: Optional[str] = None):
        """Generate TwiML response for voice calls with conversation state."""
        response = VoiceResponse()
        
        # Initialize or get conversation state
        if call_sid not in self.conversations:
            self.conversations[call_sid] = {
                'current_question': 0,
                'transcript': [],
                'responses': [],
                'start_time': datetime.now().isoformat(),
                'status': 'greeting',  # Start with greeting state
                'call_sid': call_sid,
                'call_type': 'confirmation'  # or 'interview' for different flows
            }
        
        conv = self.conversations[call_sid]
        user_input = (speech_result or '').strip().lower()
        
        # Handle DTMF digits if provided
        if digits:
            if digits == '1':
                user_input = 'yes'
            elif digits == '2':
                user_input = 'call me later'
        
        # Log the user input
        if user_input:
            logger.info(f"User input received - Call SID: {call_sid}, Input: {user_input}")
            conv['transcript'].append(f"User: {user_input}")
        
        # State machine for call flow
        if conv['status'] == 'greeting':
            # Initial greeting
            greeting = """
                Hello! Thank you for taking the time to speak with us today. 
                This is a confirmation call for your scheduled interview with Radixsol.
                If you're available to speak now, please say 'Yes' or press 1.
                If now is not a good time, please say 'Call me later' or press 2.
            """
            response.say(greeting)
            conv['transcript'].append(f"System: {greeting.strip()}")
            conv['status'] = 'awaiting_response'
            
            # Add gather for user response
            gather = Gather(
                input='speech dtmf',
                action=f'/api/voice/response?call_sid={call_sid}',
                method='POST',
                speechTimeout=5,
                timeout=10,
                numDigits=1
            )
            response.append(gather)
            
        elif conv['status'] == 'awaiting_response':
            if 'yes' in user_input or '1' in user_input:
                # User is available, proceed with interview
                response.say("""
                    Great! Let's get started with the interview.
                    I'll ask you a few questions about your experience and skills.
                    Please feel free to answer in your own words.
                """)
                conv['status'] = 'interview_started'
                conv['call_type'] = 'interview'
                
                # Ask the first question
                next_question = self.interview_questions[0]
                response.say(next_question)
                conv['last_question'] = next_question
                conv['transcript'].append(f"Interviewer: {next_question}")
                
                # Set up gathering the response
                gather = Gather(
                    input='speech',
                    action=f'/api/voice/response?call_sid={call_sid}',
                    method='POST',
                    speechTimeout='auto',
                    timeout=10
                )
                response.append(gather)
                
            elif 'later' in user_input or '2' in user_input or 'call me' in user_input:
                # Schedule a callback
                response.say("""
                    We understand. We'll call you back at a better time.
                    Please let us know a convenient time by replying to our text message.
                    Thank you and have a great day!
                """)
                conv['status'] = 'completed'
                self.end_interview(call_sid)
            else:
                # Unrecognized response
                response.say("""
                    I'm sorry, I didn't understand your response. 
                    Please say 'Yes' or press 1 if you'd like to proceed with the interview now,
                    or say 'Call me later' or press 2 to schedule another time.
                """)
                conv['status'] = 'greeting'  # Return to greeting state
        
        elif conv['status'] == 'interview_started' and conv['call_type'] == 'interview':
            # Handle interview question responses
            if user_input:
                conv['responses'].append({
                    'question': conv.get('last_question', ''),
                    'answer': user_input,
                    'timestamp': datetime.now().isoformat()
                })
                
                # Move to next question
                next_question_index = conv['current_question'] + 1
                if next_question_index < len(self.interview_questions):
                    next_question = self.interview_questions[next_question_index]
                    response.say(next_question)
                    conv['last_question'] = next_question
                    conv['current_question'] = next_question_index
                    conv['transcript'].append(f"Interviewer: {next_question}")
                    
                    # Set up gathering the next response
                    gather = Gather(
                        input='speech',
                        action=f'/api/voice/response?call_sid={call_sid}',
                        method='POST',
                        speechTimeout='auto',
                        timeout=10
                    )
                    response.append(gather)
                else:
                    # End of interview
                    response.say("""
                        Thank you for taking the time to speak with us today. 
                        We appreciate your responses and will be in touch soon about the next steps.
                        Have a great day!
                    """)
                    conv['status'] = 'completed'
                    self.end_interview(call_sid)
            else:
                # No input received, reprompt
                response.say("Sorry - I didn't catch that.")
                response.say(conv.get('last_question', 'Could you please answer the question?'))
                
                # Set up gathering the response again
                gather = Gather(
                    input='speech',
                    action=f'/api/voice/response?call_sid={call_sid}',
                    method='POST',
                    speechTimeout='auto',
                    timeout=10
                )
                response.append(gather)
        
        if conv['status'] != 'completed':
            response.pause(length=1)
        
        return str(response)

    def extract_and_schedule_call(self, from_number: str, message: str) -> str:
        """Extract time from message and schedule a call."""
        try:
            # First try to extract availability using the extract_availability method
            availability = self.extract_availability(message)
            
            if availability and availability.get("has_availability", False):
                slot = availability['slots'][0]
                if slot.get('start_time') and slot.get('timezone'):
                    try:
                        # Get the timezone
                        tz = pytz.timezone(slot['timezone'])
                        now = datetime.now(tz)
                        
                        # Parse the time parts
                        time_parts = list(map(int, slot['start_time'].split(':')))
                        hour = time_parts[0]
                        minute = time_parts[1] if len(time_parts) > 1 else 0
                        
                        # If we have a specific date in the slot, use it
                        if slot.get('date'):
                            dt = datetime.strptime(slot['date'], '%Y-%m-%d')
                            dt = tz.localize(datetime(
                                year=dt.year,
                                month=dt.month,
                                day=dt.day,
                                hour=hour,
                                minute=minute,
                                second=0,
                                microsecond=0
                            ))
                        else:
                            # No date specified, use today's date with the specified time
                            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                            
                            # If the time has already passed today, schedule for tomorrow
                            if dt <= now:
                                dt += timedelta(days=1)
                        
                        # Ensure the datetime is timezone-aware
                        if dt.tzinfo is None:
                            dt = tz.localize(dt)
                        
                        # Schedule the call
                        if self.schedule_call(from_number, dt, slot['timezone']):
                            # Format time in a Windows-compatible way without timezone
                            time_str = dt.strftime('%I:%M %p').lstrip('0').replace(' 0', ' ')
                            day_str = dt.strftime('%A, %B %d')
                            return f"Great! I've scheduled a call with you for {day_str} at {time_str}. We'll call you then!"
                        
                    except Exception as e:
                        logger.error(f"Error parsing time from availability: {e}", exc_info=True)
            
            # Fallback to simple time extraction if the above fails
            # This pattern matches times like:
            # - 12 PM
            # - 12:30 PM
            # - 12pm
            # - 12:30pm
            time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*([ap]m|AM|PM)?\b', message, re.IGNORECASE)
            if not time_match:
                return "I couldn't find a specific time in your message. Could you please provide a time like '2 PM' or '14:00'?"
            
            # Extract time components
            hour = int(time_match.group(1))
            minute = 0  # Default to 0 minutes if not specified
            if time_match.group(2):
                minute = int(time_match.group(2))
            period = (time_match.group(3) or '').lower()
            
            # Convert to 24-hour format
            if 'pm' in period and hour < 12:
                hour += 12
            elif 'am' in period and hour == 12:
                hour = 0
            
            # Default to ET if no timezone specified
            tz = pytz.timezone('America/New_York')
            now = datetime.now(tz)
            
            # Create datetime for today at the specified time
            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # If the time has already passed today, schedule for tomorrow
            if dt <= now:
                dt += timedelta(days=1)
                
            # Ensure the datetime is timezone-aware
            if dt.tzinfo is None:
                dt = tz.localize(dt)
            tz_abbr = 'ET'
            tz_name = 'America/New_York'
            
            # Get timezone from message if specified
            tz_match = re.search(r'\b(?:EST|EDT|CST|CDT|MST|MDT|PST|PDT|IST|GMT|UTC)\b', message.upper())
            if tz_match:
                tz_abbr = tz_match.group(0)
                tz_mapping = {
                    'EST': 'America/New_York',
                    'EDT': 'America/New_York',
                    'CST': 'America/Chicago',
                    'CDT': 'America/Chicago',
                    'MST': 'America/Denver',
                    'MDT': 'America/Denver',
                    'PST': 'America/Los_Angeles',
                    'PDT': 'America/Los_Angeles',
                    'IST': 'Asia/Kolkata',
                    'GMT': 'GMT',
                    'UTC': 'UTC'
                }
                tz_name = tz_mapping.get(tz_abbr, 'America/New_York')
            
            tz = pytz.timezone(tz_name)
            
            # Get current time in the specified timezone
            now = datetime.now(tz)
            
            # Create datetime object for today at the specified time
            scheduled_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            
            # If the time has already passed today, schedule for tomorrow
            if scheduled_time < now:
                scheduled_time += timedelta(days=1)
            
            # Schedule the call
            if self.schedule_call(from_number, scheduled_time, tz_abbr):
                # Format the time in a user-friendly way without timezone
                # Format time in a Windows-compatible way
                time_str = scheduled_time.strftime('%I:%M %p').lstrip('0').replace(' 0', ' ')
                formatted_time = f"{scheduled_time.strftime('%A, %B %d')} at {time_str}"
                return f"Great! I've scheduled a call with you for {formatted_time}. We'll call you then!"
            
            return "I'm sorry, I couldn't schedule the call. Please try again later."
                
        except Exception as e:
            logger.error(f"Error in extract_and_schedule_call: {str(e)}", exc_info=True)
            return "I'm sorry, there was an error processing your request. Please try again later."

_messaging_service_instance = None

def get_messaging_service(db: Optional[Session] = None) -> MessagingService:
    """Get or create the messaging service instance.
    
    Args:
        db: SQLAlchemy database session. Required for first-time initialization.
        
    Returns:
        MessagingService: The messaging service instance.
        
    Raises:
        ValueError: If db is None and this is the first initialization.
    """
    global _messaging_service_instance
    
    if _messaging_service_instance is None:
        if db is None:
            raise ValueError("Database session is required for first-time initialization")
        _messaging_service_instance = MessagingService(db)
    
    return _messaging_service_instance

# Create a proxy object that will be used as the module-level messaging_service
class _MessagingServiceProxy:
    def __getattr__(self, name):
        if _messaging_service_instance is None:
            raise RuntimeError(
                "Messaging service not initialized. "
                "Call get_messaging_service(db) first."
            )
        return getattr(_messaging_service_instance, name)

# This is what other modules will import
messaging_service = _MessagingServiceProxy()
