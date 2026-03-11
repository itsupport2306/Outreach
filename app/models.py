from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

# Import shared SQLAlchemy Base and session from database module
from .database import Base, SessionLocal

class InterviewSlot(BaseModel):
    """Represents an available time slot for an interview."""
    start_time: datetime
    end_time: datetime
    timezone: str = "UTC"

class ScheduleRequest(BaseModel):
    """Request model for scheduling an interview."""
    candidate_email: str
    interviewer_email: str
    start_time: datetime
    end_time: datetime
    timezone: str = "UTC"
    meeting_title: str = "Interview"
    description: str = "Scheduled Interview"
    location: str = "Online"
    send_notifications: bool = True

class AvailabilityRequest(BaseModel):
    """Request model for checking availability."""
    candidate_email: str
    interviewer_email: str
    date: str  # YYYY-MM-DD format
    timezone: str = "UTC"

class InterviewSession(BaseModel):
    """Model for tracking interview sessions."""
    session_id: str
    candidate_email: str
    interviewer_email: str
    start_time: datetime
    end_time: datetime
    status: str = "scheduled"  # scheduled, in_progress, completed, cancelled
    questions: List[Dict[str, Any]] = []
    current_question_index: int = 0
    evaluation: Optional[Dict[str, Any]] = None

class InterviewResponse(BaseModel):
    """Model for interview responses."""
    session_id: str
    question_id: str
    answer: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class InterviewEvaluation(BaseModel):
    """Model for interview evaluation results."""

class InterviewSchedule(Base):
    """SQLAlchemy model for storing interview schedules."""
    __tablename__ = "interview_schedules"
    
    id = Column(Integer, primary_key=True, index=True)
    # Phone numbers are typically short; 50 chars is more than enough and satisfies MySQL's VARCHAR length requirement.
    candidate_phone = Column(String(50), index=True)
    scheduled_datetime = Column(DateTime, nullable=False)
    timezone = Column(String(50))
    status = Column(String(20), default='scheduled')  # scheduled, in_progress, completed, cancelled
    call_sid = Column(String(50), nullable=True)  # Twilio Call SID
    recording_url = Column(Text, nullable=True)  # URL to the call recording
    # Whether the candidate actually picked up the scheduled call: 'Yes' or 'No'.
    call_pickup = Column(String(3), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    def __repr__(self):
        return f"<InterviewSchedule(phone={self.candidate_phone}, time={self.scheduled_datetime}, status={self.status})>"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    full_name = Column(String(255), nullable=True)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class OutreachTracker(Base):
    __tablename__ = "outreach_tracker"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(String(255), nullable=True, index=True)
    contact = Column(String(255), nullable=False, index=True)
    channel = Column(String(20), nullable=False, index=True)
    job_run_id = Column(Integer, nullable=True, index=True)
    job_code = Column(String(255), nullable=True, index=True)
    job_title = Column(String(255), nullable=True)
    first_contacted_at = Column(DateTime, nullable=False)
    last_outreach_at = Column(DateTime, nullable=False)
    last_inbound_at = Column(DateTime, nullable=True)
    replied_at = Column(DateTime, nullable=True)
    followup_count = Column(Integer, nullable=False, default=0)
    next_followup_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="active")
