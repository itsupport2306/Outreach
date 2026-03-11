import os
import json
import logging
from typing import Dict, List, Optional
import google.generativeai as genai

logger = logging.getLogger(__name__)

class GeminiChat:
    def __init__(self, api_key: str):
        """Initialize the Gemini chat model."""
        # Avoid external side effects (network/quota consumption) during migrations and offline tasks.
        # Alembic sets ALEMBIC_RUNNING=1 in alembic/env.py.
        self._alembic_running = (os.getenv("ALEMBIC_RUNNING") or "").strip() in {"1", "true", "True"}
        genai.configure(api_key=api_key)
        
        # Using the specified model
        self.model_name = "gemini-2.5-flash-lite"  # Using the specified model version
        
        try:
            # Initialize with safe defaults
            self.model = genai.GenerativeModel(
                self.model_name,
                generation_config={
                    "temperature": 0.7,
                    "top_p": 0.9,
                    "top_k": 40,
                    "max_output_tokens": 2048,
                },
                safety_settings={
                    'HARM_CATEGORY_HARASSMENT': 'BLOCK_NONE',
                    'HARM_CATEGORY_HATE_SPEECH': 'BLOCK_NONE',
                    'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'BLOCK_NONE',
                    'HARM_CATEGORY_DANGEROUS_CONTENT': 'BLOCK_NONE',
                }
            )
            # IMPORTANT: do not perform a test request here.
            # It consumes quota and can fail (429) during import-time contexts like Alembic.
            if not self._alembic_running:
                logger.info(f"Successfully initialized Gemini model: {self.model_name}")
            
        except Exception as e:
            logger.error(f"Failed to initialize Gemini model {self.model_name}: {str(e)}")
            # Fall back to a more basic model if the primary one fails
            self.model_name = "gemini-pro"  # Most basic available model
            self.model = genai.GenerativeModel(self.model_name)
            logger.info(f"Falling back to model: {self.model_name}")
            
        self.chats = {}  # Store chat sessions by conversation ID

    def get_chat(self, conversation_id: str):
        """Get or create a chat session."""
        if conversation_id not in self.chats:
            self.chats[conversation_id] = self.model.start_chat(history=[])
        return self.chats[conversation_id]

    async def generate_response(
        self,
        conversation_id: str,
        message: str,
        system_prompt: str,
        conversation_history: Optional[List[Dict]] = None,
        max_history: int = 10
    ) -> str:
        """Generate a response using Gemini with conversation history."""
        try:
            # Get or create chat session
            chat = self.get_chat(conversation_id)
            
            # Prepare the full prompt with system message and history
            full_prompt = system_prompt + "\n\n"
            
            # Add conversation history if provided
            if conversation_history:
                for msg in conversation_history[-max_history:]:  # Limit history length
                    role = "user" if msg.get("role") == "user" else "model"
                    full_prompt += f"{role}: {msg.get('content', '')}\n"
            
            # Add the current message
            full_prompt += f"user: {message}\n"
            
            logger.debug(f"Sending prompt to Gemini: {full_prompt[:200]}...")  # Log first 200 chars of prompt
            
            try:
                # Generate response with error handling
                response = await chat.send_message_async(full_prompt)
                if not response or not hasattr(response, 'text') or not response.text:
                    raise ValueError("Empty or invalid response from Gemini API")
                response_text = response.text.strip()
                
                # Update chat history
                chat.history.extend([
                    {"role": "user", "parts": [full_prompt]},
                    {"role": "model", "parts": [response_text]}
                ])
                
                return response_text
                
            except Exception as api_error:
                logger.error(f"Gemini API error: {str(api_error)}", exc_info=True)
                # Try one more time with a fresh chat session
                try:
                    self.chats.pop(conversation_id, None)  # Clear the chat session
                    chat = self.get_chat(conversation_id)  # Get a fresh session
                    response = await chat.send_message_async(full_prompt)
                    response_text = response.text.strip()
                    
                    # Update chat history
                    chat.history.extend([
                        {"role": "user", "parts": [full_prompt]},
                        {"role": "model", "parts": [response_text]}
                    ])
                    
                    return response_text
                except Exception as retry_error:
                    logger.error(f"Retry failed: {str(retry_error)}")
                    raise
                    
        except Exception as e:
            logger.error(f"Error generating Gemini response: {e}", exc_info=True)
            return "I apologize, but I'm having trouble processing your message. Could you please try again?"

    def clear_conversation(self, conversation_id: str) -> None:
        """Clear conversation history for a specific conversation."""
        if conversation_id in self.chats:
            del self.chats[conversation_id]
