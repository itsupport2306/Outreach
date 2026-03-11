import os
import json
from datetime import datetime
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)

class TranscriptManager:
    def __init__(self, base_dir: str = None):
        """Initialize the transcript manager.
        
        Args:
            base_dir: Base directory to store transcripts. If None, uses 'transcripts' in the project root.
        """
        if base_dir is None:
            base_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'transcripts')
        
        self.transcripts_dir = base_dir
        os.makedirs(self.transcripts_dir, exist_ok=True)
        logger.info(f"Transcripts will be saved to: {self.transcripts_dir}")
    
    def save_transcript(self, call_sid: str, transcript_data: Dict) -> str:
        """Save a transcript to a file.
        
        Args:
            call_sid: The unique call identifier
            transcript_data: Dictionary containing transcript data
            
        Returns:
            str: Path to the saved transcript file
        """
        if not transcript_data.get('transcript'):
            logger.warning(f"No transcript data to save for call {call_sid}")
            return ""
            
        try:
            # Create a timestamp for the filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"interview_{call_sid}_{timestamp}.json"
            filepath = os.path.join(self.transcripts_dir, filename)
            
            # Add metadata
            transcript_data['metadata'] = {
                'call_sid': call_sid,
                'saved_at': datetime.now().isoformat(),
                'questions_asked': len(transcript_data.get('transcript', [])),  # Count of questions asked
                'version': '1.0'
            }
            
            # Save as JSON
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(transcript_data, f, indent=2, ensure_ascii=False)
                
            logger.info(f"Transcript saved to {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"Error saving transcript for call {call_sid}: {str(e)}")
            return ""
    
    def get_transcript(self, call_sid: str) -> Optional[Dict]:
        """Retrieve a transcript by call SID.
        
        Args:
            call_sid: The call identifier
            
        Returns:
            Optional[Dict]: The transcript data if found, None otherwise
        """
        try:
            # Find the most recent transcript for this call
            transcripts = [
                f for f in os.listdir(self.transcripts_dir)
                if f.startswith(f'interview_{call_sid}_')
            ]
            
            if not transcripts:
                return None
                
            # Get the most recent transcript
            latest = max(transcripts)
            filepath = os.path.join(self.transcripts_dir, latest)
            
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
                
        except Exception as e:
            logger.error(f"Error loading transcript for call {call_sid}: {str(e)}")
            return None
    
    def get_transcript_text(self, call_sid: str) -> str:
        """Get a formatted text version of a transcript.
        
        Args:
            call_sid: The call identifier
            
        Returns:
            str: Formatted text transcript
        """
        data = self.get_transcript(call_sid)
        if not data:
            return "No transcript found for this call."
            
        lines = [
            "=" * 60,
            f"INTERVIEW TRANSCRIPT - {data.get('metadata', {}).get('saved_at', '')}",
            f"Call SID: {call_sid}",
            "=" * 60,
            ""
        ]
        
        # Add each line of the conversation
        for i, line in enumerate(data.get('transcript', []), 1):
            lines.append(f"{i}. {line}")
            
        return "\n".join(lines)
