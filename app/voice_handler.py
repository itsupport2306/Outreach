from fastapi import Request, HTTPException
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
import os
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from .models import InterviewSchedule, SessionLocal

logger = logging.getLogger(__name__)

class VoiceHandler:
    def __init__(self):
        # Interview questions
        self.questions = [
            "Can you tell me about your experience with AI and machine learning?",
            "What interests you about this AI Engineer position?",
            "Can you describe a challenging project you've worked on?",
            "How do you stay updated with the latest AI developments?",
            "Do you have any questions for us?"
        ]
        # Store active interviews: {call_sid: {phone: str, question_index: int}}
        self.active_interviews: Dict[str, Dict] = {}

    def get_voice_response(self) -> VoiceResponse:
        """Create a new TwiML voice response."""
        return VoiceResponse()

    def handle_incoming_call(self, phone_number: str, request: Request) -> str:
        """Handle an incoming call for an interview."""
        response = self.get_voice_response()
        
        # Check if this is a scheduled interview
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            interview = db.query(InterviewSchedule).filter(
                InterviewSchedule.candidate_phone == phone_number,
                InterviewSchedule.status == 'scheduled',
                InterviewSchedule.scheduled_datetime <= now + timedelta(minutes=15),  # 15 min buffer
                InterviewSchedule.scheduled_datetime >= now - timedelta(minutes=30)  # 30 min buffer
            ).first()
            
            if not interview:
                response.say("We don't have a scheduled interview for you at this time. "
                           "Please check your scheduled time and call back then. Goodbye.")
                response.hangup()
                return str(response)
            
            call_sid = request.form.get('CallSid')
            
            # Update interview status
            interview.status = 'in_progress'
            interview.call_sid = call_sid
            db.commit()
            
            # Initialize interview
            self.active_interviews[call_sid] = {
                'phone': phone_number,
                'question_index': 0,
                'interview_id': interview.id
            }
            
            # Start the interview
            response.say("Thank you for joining the interview for the AI Engineer position at Radixsol. "
                        "We'll begin with a few questions. Please wait for the beep after each question "
                        "to record your response. Let's start with the first question.")
            
            # Ask the first question
            self.ask_question(response, 0)
            return str(response)
            
        except Exception as e:
            logger.error(f"Error handling incoming call: {e}")
            response.say("We encountered an error setting up your interview. Please try again later.")
            response.hangup()
            return str(response)
        finally:
            db.close()

    def ask_question(self, response: VoiceResponse, question_index: int) -> None:
        """Add a question to the response."""
        if question_index < len(self.questions):
            response.say(self.questions[question_index])
            response.record(
                action=f"/api/voice/answer?q={question_index}",
                play_beep=True,
                max_length=300,  # 5 minutes max
                timeout=5
            )
            # Redirect to next question after recording
            response.redirect(f"/api/voice/next-question?q={question_index}")
        else:
            # End of questions
            self.end_interview(response)

    def handle_answer(self, call_sid: str, question_index: int, recording_url: str) -> str:
        """Handle the candidate's answer to a question."""
        response = self.get_voice_response()
        
        # Store the answer (in a real app, you'd save this to a database)
        logger.info(f"Storing answer for question {question_index} from {call_sid}")
        logger.info(f"Recording URL: {recording_url}")
        
        # Move to next question
        self.ask_question(response, question_index + 1)
        return str(response)

    def end_interview(self, response: VoiceResponse, call_sid: Optional[str] = None) -> None:
        """End the interview and clean up."""
        if call_sid and call_sid in self.active_interviews:
            # Update interview status in database
            db = SessionLocal()
            try:
                interview = db.query(InterviewSchedule).get(
                    self.active_interviews[call_sid]['interview_id']
                )
                if interview:
                    interview.status = 'completed'
                    interview.updated_at = datetime.utcnow()
                    db.commit()
                    logger.info(f"Interview {interview.id} marked as completed")
            except Exception as e:
                logger.error(f"Error updating interview status: {e}")
            finally:
                db.close()
            
            # Remove from active interviews
            del self.active_interviews[call_sid]
        
        response.say("Thank you for your time. The interview is now complete. We'll be in touch soon.")
        response.hangup()
    
    def handle_recording(self, recording_url: str) -> str:
        """Handle the recorded response and prepare for next question."""
        # Here you could process the recording (transcribe, analyze, etc.)
        logger.info(f"Received recording URL: {recording_url}")
        
        response = self.get_voice_response()
        response.say("Thank you for your response. Let's move to the next question.")
        response.redirect("/api/voice/next-question")
        
        return str(response)
    
    def next_question(self) -> str:
        """Move to the next question or end the interview."""
        self.current_question_index += 1
        response = self.get_voice_response()
        
        if self.current_question_index < len(self.interview_questions):
            self.ask_question(response)
        else:
            # End of interview
            response.say("Thank you for your time. This concludes our interview. "
                        "We'll review your responses and get back to you soon. Have a great day!")
            response.hangup()
            self.interview_in_progress = False
        
        return str(response)

# Create a singleton instance
voice_handler = VoiceHandler()
