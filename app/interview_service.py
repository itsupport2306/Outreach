import os
import logging
import json
import hashlib
import re
import time
from pathlib import Path
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Union
from sqlalchemy.orm import Session
from fastapi import Response
from twilio.twiml.voice_response import VoiceResponse, Gather
import requests
import google.generativeai as genai
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

class InterviewService:
    def __init__(self, db: Session):
        """Initialize the interview service with database and required services."""
        self.db = db
        self.active_interviews = {}  # Maps call_sid to interview state
        self.gemini_model = None
        self._initialize_ai()

        self._tts_api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
        self._tts_voice_id = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
        raw_base_url = (os.getenv("BASE_URL") or "")
        # Support inline comments in .env like: BASE_URL=https://.../ # comment
        base_url = raw_base_url.split("#", 1)[0].strip().rstrip("/")
        self._public_base_url = base_url

        if self._public_base_url and not self._public_base_url.startswith(("http://", "https://")):
            logger.warning(f"BASE_URL does not look like a valid URL for TTS playback: {self._public_base_url}")

        project_root = Path(__file__).resolve().parents[1]
        self._tts_dir = project_root / "tts"
        try:
            self._tts_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Reuse HTTP connection(s) for lower latency
        self._http = requests.Session()

        # If enabled, avoid Twilio <Say> fallback so the voice never changes mid-call.
        # When strict, if we cannot generate/play ElevenLabs audio we will insert a short pause.
        self._strict_tts = (os.getenv("ELEVENLABS_STRICT_TTS") or "").strip().lower() in {"1", "true", "yes"}

    def _tts_enabled(self) -> bool:
        return bool(self._tts_api_key and self._tts_voice_id and self._public_base_url)

    def _tts_cache_filename(self, text: str) -> str:
        normalized = self._normalize_tts_text(text)
        key = f"{self._tts_voice_id}|{normalized}".encode("utf-8")
        digest = hashlib.sha256(key).hexdigest()
        return f"{digest}.mp3"

    def _normalize_tts_text(self, text: str) -> str:
        t = (text or "").strip()
        # Normalize punctuation so cache keys match across different sources
        # (prewarm vs runtime) and avoid ElevenLabs/Twilio voice switching.
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

    def _tts_public_url(self, filename: str) -> str:
        return f"{self._public_base_url}/tts/{filename}"

    def _tts_cached_url(self, text: str) -> Optional[str]:
        """Return cached public URL for text if already generated, else None.

        Important: This must be fast and MUST NOT call external APIs, because it
        can be executed in a Twilio webhook request path.
        """
        if not self._tts_enabled():
            return None
        clean = (text or "").strip()
        if not clean:
            return None
        filename = self._tts_cache_filename(clean)
        target_path = self._tts_dir / filename
        try:
            if target_path.exists() and target_path.stat().st_size > 0:
                return self._tts_public_url(filename)
        except Exception:
            return None
        return None

    def _prewarm_tts_text_async(self, text: str) -> None:
        """Best-effort background generation. Never blocks the current request."""
        if not self._tts_enabled():
            return
        clean = (text or "").strip()
        if not clean:
            return
        try:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(asyncio.to_thread(self._ensure_tts_mp3, clean))
            except RuntimeError:
                # No running loop (e.g., called from sync path). Fall back to a daemon thread.
                import threading

                t = threading.Thread(target=self._ensure_tts_mp3, args=(clean,), daemon=True)
                t.start()
        except Exception:
            # Never fail the call because background prewarm failed
            return

    def _ensure_tts_mp3(self, text: str) -> Optional[str]:
        """Generate and cache an MP3 for the given text using ElevenLabs.

        Returns a public URL (via BASE_URL + /tts/...) or None if not available.
        """
        if not self._tts_enabled():
            return None

        clean = (text or "").strip()
        if not clean:
            return None

        start = time.perf_counter()

        filename = self._tts_cache_filename(clean)
        target_path = self._tts_dir / filename
        if target_path.exists() and target_path.stat().st_size > 0:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            logger.info(f"TTS cache hit: {filename} ({elapsed_ms}ms)")
            return self._tts_public_url(filename)

        try:
            optimize_latency = int((os.getenv("ELEVENLABS_OPTIMIZE_STREAMING_LATENCY") or "3").strip() or "3")
            optimize_latency = max(0, min(4, optimize_latency))
            output_format = (os.getenv("ELEVENLABS_OUTPUT_FORMAT") or "mp3_22050_32").strip() or "mp3_22050_32"

            url = f"https://api.elevenlabs.io/v1/text-to-speech/{self._tts_voice_id}"
            params = {
                "optimize_streaming_latency": optimize_latency,
                "output_format": output_format,
            }
            headers = {
                "xi-api-key": self._tts_api_key,
                "accept": "audio/mpeg",
                "content-type": "application/json",
            }

            configured_model = (os.getenv("ELEVENLABS_MODEL_ID") or "").strip()
            default_model = "eleven_flash_v2_5"

            # Avoid mixing models across turns (can sound like a voice change).
            # Only fall back if the chosen model is rejected (deprecated/free-tier).
            model_candidates = [configured_model] if configured_model else [default_model]
            fallback_models = [
                "eleven_flash_v2_5",
                "eleven_turbo_v2_5",
                "eleven_turbo_v2",
                "eleven_multilingual_v2",
            ]

            configured_seed = (os.getenv("ELEVENLABS_SEED") or "").strip()
            seed: Optional[int] = None
            if configured_seed:
                try:
                    seed = int(configured_seed)
                except Exception:
                    seed = None

            last_err_text = ""
            for model_id in model_candidates:
                payload = {
                    "text": clean,
                    "model_id": model_id,
                    "voice_settings": {
                        "stability": float(os.getenv("ELEVENLABS_STABILITY", "0.45")),
                        "similarity_boost": float(os.getenv("ELEVENLABS_SIMILARITY_BOOST", "0.75")),
                    },
                }
                if seed is not None:
                    payload["seed"] = seed

                r = self._http.post(url, params=params, headers=headers, json=payload, timeout=12)
                if r.status_code == 200 and r.content:
                    tmp_path = target_path.with_suffix(".tmp")
                    tmp_path.write_bytes(r.content)
                    tmp_path.replace(target_path)
                    elapsed_ms = int((time.perf_counter() - start) * 1000)
                    logger.info(
                        f"TTS cache miss generated: {target_path.name} (model_id={model_id}, {elapsed_ms}ms)"
                    )
                    return self._tts_public_url(filename)

                err_text = (r.text or "")
                last_err_text = err_text

                # If the error indicates a deprecated/free-tier model, try the next model.
                if "model_deprecated" in err_text or "not available on the free tier" in err_text:
                    logger.warning(
                        f"ElevenLabs model rejected (model_id={model_id}, status={r.status_code}); trying fallback model."
                    )
                    # Expand to fallbacks only if our chosen model is rejected.
                    for fm in fallback_models:
                        if fm not in model_candidates:
                            model_candidates.append(fm)
                    continue

                # Other errors are likely not recoverable by switching model.
                logger.error(
                    f"ElevenLabs TTS failed (model_id={model_id}, status={r.status_code}): "
                    f"{err_text[:300] if err_text else 'no body'}"
                )
                return None

            logger.error(
                f"ElevenLabs TTS failed for all models (last_error={last_err_text[:300] if last_err_text else 'no body'})"
            )
            return None
        except Exception as e:
            logger.error(f"ElevenLabs TTS generation error: {e}", exc_info=True)
            return None

    def _append_tts(self, node: Union[VoiceResponse, Gather], text: str, *, voice: str = "man") -> None:
        """Append a TTS prompt to a TwiML node using <Play> if possible, else <Say>."""
        text = self._normalize_tts_text(text)
        # IMPORTANT: Do not block Twilio webhooks on external TTS generation.
        # Only play if we already have cached audio; otherwise fall back immediately
        # and prewarm in the background.
        audio_url = self._tts_cached_url(text)
        if audio_url:
            try:
                node.play(audio_url)
                return
            except Exception:
                pass

        # Cache miss: trigger background generation for future turns.
        if self._tts_enabled():
            self._prewarm_tts_text_async(text)

        if self._tts_enabled():
            logger.warning(f"TTS fallback to <Say> (tts_enabled=True, text={text!r}, strict={self._strict_tts})")
            if self._strict_tts:
                # Keep voice consistent by not switching to Twilio <Say>.
                try:
                    node.pause(length=1)
                except Exception:
                    pass
                return

        node.say(text, voice=voice)

    def _initialize_ai(self):
        """Initialize the AI model for conducting interviews."""
        try:
            genai.configure(api_key=os.getenv('GOOGLE_API_KEY'))
            # Use the same lightweight Gemini model configured elsewhere in the app
            self.gemini_model = genai.GenerativeModel('gemini-2.5-flash-lite')
        except Exception as e:
            logger.error(f"Failed to initialize AI model: {e}")
            raise

    async def start_interview(
        self,
        call_sid: str,
        phone_number: str,
        job_description: str,
        job_title: Optional[str] = None,
        important_questions: Optional[List[str]] = None,
        required_skills: Optional[List[str]] = None,
        required_questions: Optional[List[str]] = None,
        certifications: Optional[List[str]] = None,
    ) -> Response:
        """Start a new interview session."""
        response = VoiceResponse()

        questions = self._scripted_questions(
            job_description,
            job_title=job_title,
            required_skills=required_skills,
            required_questions=required_questions,
            certifications=certifications,
        )

        # Prewarm TTS audio for all scripted prompts + acknowledgements so
        # /api/interview/answer can respond quickly without blocking on ElevenLabs.
        asyncio.create_task(self._prewarm_tts_for_call(call_sid, questions))

        # Initialize interview state
        self.active_interviews[call_sid] = {
            'phone_number': phone_number,
            'job_description': job_description,
            'job_title': (job_title or '').strip(),
            'important_questions': important_questions or [],
            'required_skills': required_skills or [],
            'required_questions': required_questions or [],
            'certifications': certifications or [],
            'questions': questions,
            'current_question': 0,
            'answers': [],
            'start_time': datetime.now(timezone.utc),
            'status': 'in_progress',
            'intro_smalltalk_pending': True,
        }

        self._append_tts(
            response,"Hi, this is James Chandler calling from Radixsol. "
            "Thanks so much for taking my call today.",
            voice="man",
        )

        self._append_tts(
            response,
            "Before we start, just a quick note: if you’d like me to repeat a question, simply say ‘repeat’. "
            "And if you have any questions, we’ll save those for the end. "
            "Please take your time—there are no right or wrong answers.",
            voice="man",
        )

        # Warm small-talk opener and wait for the candidate's response.
        gather = self._append_speech_gather(response, call_sid)
        self._append_tts(gather, "How are you today?", voice="man")
        return response

    async def _prewarm_tts_for_call(self, call_sid: str, questions: List[str]) -> None:
        if not self._tts_enabled():
            return
        try:
            # Acknowledgement phrases used by _acknowledge_answer
            ack_texts = [
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
            ]

            # Also prewarm the greeting and repeat-ack.
            fixed_texts = [
                "Hi, this is James Chandler calling from Radixsol. Thanks so much for taking my call today.",
                "Before we start, just a quick note: if you’d like me to repeat a question, simply say ‘repeat’. And please take your time—there are no right or wrong answers.",
                "How are you today?",
                "Of course—happy to repeat that.",
            ]

            texts = []
            for t in (fixed_texts + ack_texts + list(questions or [])):
                t = (t or "").strip()
                if t:
                    texts.append(t)

            # Deduplicate while preserving order
            seen = set()
            unique_texts = []
            for t in texts:
                key = t.lower()
                if key in seen:
                    continue
                seen.add(key)
                unique_texts.append(t)

            sem = asyncio.Semaphore(3)

            async def _one(text: str) -> None:
                async with sem:
                    await asyncio.to_thread(self._ensure_tts_mp3, text)

            await asyncio.gather(*[_one(t) for t in unique_texts])
            logger.info(f"Prewarmed TTS cache for call_sid={call_sid} (items={len(unique_texts)})")
        except Exception as e:
            logger.error(f"Failed to prewarm TTS for call_sid={call_sid}: {e}", exc_info=True)

    def _extract_jd_fields(self, jd: str) -> Dict[str, Optional[str]]:
        text = (jd or "").strip()
        lower = text.lower()

        title = None
        m = re.search(r"(?im)^\s*(job title|position|role)\s*[:\-]\s*(.+?)\s*$", text)
        if m:
            title = m.group(2).strip()
        else:
            m2 = re.search(r"(?i)\b(hiring for|role of|position of)\b\s*[:\-]?\s*([A-Za-z0-9 /\-]{3,80})", text)
            if m2:
                title = m2.group(2).strip()
        if not title:
            # Common pattern: the first line contains the job title, e.g.
            # "Temp - Registered Nurse (RN) - OB/GYN (Days) Batesville, AR"
            first_line = (text.splitlines()[0].strip() if text else "")
            if first_line and len(first_line) <= 120:
                # Strip common prefixes
                first_line = re.sub(r"(?i)^\s*(temp|contract|full\s*time|part\s*time)\s*[-:]\s*", "", first_line).strip()
                # Keep only the title-ish segment before location if present
                first_line = re.split(r"\s{2,}|\s+\b(remote|hybrid|on[- ]?site)\b\s+|\s+\b[a-z]{2}\b\s*$", first_line, flags=re.I)[0].strip()
                if first_line:
                    title = first_line
        if not title:
            title = "this role"

        work_arrangement = None
        if "remote" in lower:
            work_arrangement = "remote"
        elif "hybrid" in lower:
            work_arrangement = "hybrid"
        elif "on-site" in lower or "onsite" in lower or "on site" in lower:
            work_arrangement = "on-site"

        certs = None
        cert_chunks = []
        # Prefer explicit JD sections mentioning licenses/certs
        for m in re.finditer(r"(?im)^\s*(licenses?|certifications?)\s*[:\-]\s*(.+?)\s*$", text):
            chunk = m.group(2).strip()
            if chunk:
                cert_chunks.append(chunk)
        # Also detect common healthcare credentials anywhere in the JD
        common_creds = [
            "rn", "lpn", "lvn", "np", "cna", "bls", "acls", "pals", "nrp",
            "bcls", "ccrn", "cen",
        ]
        found_creds = []
        for cred in common_creds:
            if re.search(rf"(?i)\b{re.escape(cred)}\b", text):
                found_creds.append(cred.upper())
        if cert_chunks or found_creds:
            combined = []
            if cert_chunks:
                combined.append("; ".join(cert_chunks[:2]))
            if found_creds:
                combined.append(", ".join(sorted(set(found_creds))) )
            certs = " ".join([c for c in combined if c]).strip()

        skills = []
        skill_section = re.search(r"(?is)(key skills|skills|requirements)\s*[:\-]\s*(.{0,800})", text)
        if skill_section:
            chunk = skill_section.group(2)
            chunk = re.split(r"\n\s*\n|\bresponsibilities\b|\bqualifications\b", chunk, flags=re.I)[0]
            parts = re.split(r"\n|,|;|\u2022|-\s+", chunk)
            for p in parts:
                s = re.sub(r"\s+", " ", p).strip(" \t\r\n-•")
                if 2 <= len(s) <= 40 and len(skills) < 6:
                    skills.append(s)
        # If we couldn't find a skills section, fall back to a few high-signal keywords
        if not skills:
            keyword_candidates = []
            # Pull up to ~6 keywords from the first part of the JD that look like skills
            head = "\n".join(text.splitlines()[:40])
            for m in re.finditer(r"(?i)\b([A-Za-z][A-Za-z0-9+/\-]{1,20})\b", head):
                token = m.group(1)
                t = token.strip().lower()
                if t in {"job", "description", "summary", "responsibilities", "requirements", "skills"}:
                    continue
                if len(t) < 3:
                    continue
                keyword_candidates.append(token)
            # Deduplicate while preserving order
            seen = set()
            for tok in keyword_candidates:
                key = tok.lower()
                if key in seen:
                    continue
                seen.add(key)
                skills.append(tok)
                if len(skills) >= 6:
                    break
        skills_text = ", ".join(skills) if skills else None

        return {
            "job_title": title,
            "key_skills": skills_text,
            "work_arrangement": work_arrangement,
            "certifications": certs,
        }

    def _scripted_questions(
        self,
        job_description: str,
        *,
        job_title: Optional[str] = None,
        required_skills: Optional[List[str]] = None,
        required_questions: Optional[List[str]] = None,
        certifications: Optional[List[str]] = None,
    ) -> List[str]:
        fields = self._extract_jd_fields(job_description)
        title_override = (job_title or "").strip()
        job_title = title_override or (fields.get("job_title") or "this role")
        key_skills = None
        if required_skills:
            cleaned = [str(s).strip() for s in required_skills if isinstance(s, (str, int, float)) and str(s).strip()]
            if cleaned:
                key_skills = ", ".join(cleaned[:10])
        if not key_skills:
            key_skills = fields.get("key_skills")
        work_arrangement = fields.get("work_arrangement")
        certs_override = [str(c).strip() for c in (certifications or []) if c is not None and str(c).strip()]
        certs_text = ", ".join(certs_override[:10]) if certs_override else (fields.get("certifications") or "")
        certs_text = (certs_text or "").strip()

        q: List[str] = []
        q.append(
            "Great—thanks. "
            "Just to set expectations, I’ll ask a few quick questions about your background and preferences. "
            f"It’ll help us see how your experience lines up with the {job_title} opportunity. "
            "Could you give me a quick overview of your experience so far?"
        )
        q.append("What was your most recent role, and what were your day-to-day responsibilities?")

        if key_skills:
            q.append(
                f"For this role, we’re especially looking for experience with {key_skills}. "
                "Could you share a couple of examples of how you’ve used those skills recently?"
            )
        else:
            q.append("What would you say are the key skills you’d bring that are most relevant to this role?")

        q.append("What type of opportunity are you looking for right now—contract, permanent, or something else?")
        if work_arrangement:
            q.append(f"And are you open to {work_arrangement} work for this role?")

        if required_questions:
            cleaned_q = [str(x).strip() for x in required_questions if x is not None and str(x).strip()]
            seen_q = set()
            cleaned_q = [x for x in cleaned_q if not (x.lower() in seen_q or seen_q.add(x.lower()))]
            for x in cleaned_q[:6]:
                q.append(x)
        q.append("Do you have any location preferences, or are you open to different areas?")
        q.append("What’s your availability to start, and do you have any constraints on start dates?")

        if certs_text:
            q.append(
                f"Do you currently hold the required licenses, certifications, or work eligibility for this role—"
                f"for example, {certs_text}?"
            )
            q.append("Are those credentials currently active and up to date?")
        else:
            q.append("Do you currently hold any required licenses, certifications, or work eligibility for this role?")

        q.append("Lastly, do you have any planned time off or commitments coming up that we should keep in mind?")

        return q

    def _acknowledge_answer(self, answer: str, question_index: int, *, has_recording: bool = False) -> str:
        a = (answer or "").strip()
        low = a.lower()
        if not a:
            # In <Record> mode Twilio won't send SpeechResult; we still want a
            # natural acknowledgement when we received a recording.
            if has_recording:
                record_variants = [
                    "Got it—thank you.",
                    "Okay, thank you.",
                    "Thanks, I got that.",
                ]
                return record_variants[question_index % len(record_variants)]
            return "No problem."

        # Light sentiment-aware acknowledgement
        if any(w in low for w in ["good", "great", "fine", "well", "doing well"]):
            return "Glad to hear that."
        if any(w in low for w in ["not good", "bad", "tired", "stressed", "okay"]):
            return "Thanks for letting me know."

        # Vary acknowledgements to avoid sounding repetitive
        variants = [
            "Thanks for sharing that.",
            "Got it—thank you.",
            "That’s helpful, thank you.",
            "Perfect—thanks.",
            "Okay, thank you.",
            "Understood.",
        ]
        return variants[question_index % len(variants)]

    def _append_speech_gather(self, response: VoiceResponse, call_sid: str) -> Gather:
        try:
            gather_timeout = int(os.getenv("TWILIO_GATHER_TIMEOUT", "8") or "8")
        except Exception:
            gather_timeout = 8
        gather_timeout = max(5, min(gather_timeout, 20))

        # Default to 'auto' so Twilio doesn't cut the caller off too quickly.
        speech_timeout = (os.getenv("TWILIO_SPEECH_TIMEOUT", "auto") or "auto").strip()
        if speech_timeout.lower() != "auto":
            try:
                speech_timeout = str(max(1, min(int(float(speech_timeout)), 10)))
            except Exception:
                speech_timeout = "auto"

        gather = Gather(
            action=f"/api/interview/answer?call_sid={call_sid}",
            method="POST",
            input="speech dtmf",
            timeout=gather_timeout,
            speech_timeout=speech_timeout,
            barge_in=False,
            finish_on_key="#",
            speech_model="experimental_conversations",
            speech_recognition_timeout="auto",
            enhanced="true",
        )
        response.append(gather)
        response.redirect(f"/api/interview/timeout?call_sid={call_sid}", method="POST")
        return gather

    @staticmethod
    def _parse_confidence(confidence: Optional[str]) -> Optional[float]:
        try:
            if confidence is None:
                return None
            c = float(str(confidence).strip())
            if c != c:
                return None
            return max(0.0, min(c, 1.0))
        except Exception:
            return None

    def _is_noise_input(self, user_response: Optional[str], confidence: Optional[str]) -> bool:
        """Heuristic gate to avoid treating background noise as a real answer."""
        text = (user_response or "").strip()
        # If Twilio sent no text at all, it's not a real answer.
        if not text:
            return True

        # Allow common short answers (don't treat them as noise).
        low = text.lower().strip(".?!,;: ")
        if low in {"yes", "no", "ok", "okay", "yeah", "yep", "nope", "sure", "correct", "right"}:
            return False

        # Very short/low-signal transcripts are often noise ("uh", "...", "a").
        min_chars = int(os.getenv("TWILIO_MIN_SPEECH_CHARS", "4") or "4")
        if len(text) < max(1, min(min_chars, 20)):
            return True

        # If confidence is provided and is low, treat as noise.
        conf_min = os.getenv("TWILIO_SPEECH_CONFIDENCE_MIN", "0.55")
        try:
            conf_min_f = float(str(conf_min).strip())
        except Exception:
            conf_min_f = 0.55
        conf = self._parse_confidence(confidence)
        if conf is not None and conf < conf_min_f:
            return True

        return False

    async def handle_question(
        self,
        call_sid: str,
        user_response: str = None,
        recording_url: str = None,
        confidence: str = None,
    ) -> Response:
        """Handle the interview question flow."""
        if call_sid not in self.active_interviews:
            return self._end_interview("Sorry, I couldn't find your interview session. Please try again later.")
        
        interview = self.active_interviews[call_sid]
        response = VoiceResponse()

        if interview.get("closing_questions_pending") and (user_response or recording_url):
            interview["closing_questions_pending"] = False
            interview.setdefault("candidate_questions", [])
            interview["candidate_questions"].append(
                {
                    "question": "Candidate questions/doubts",
                    "answer": user_response or "",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            interview.setdefault("answers", [])
            interview["answers"].append(
                {
                    "question": "Candidate questions/doubts",
                    "answer": user_response or "",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._append_tts(
                response,
                "Thank you. We’ll review everything and reach out if you’re a good fit for this role. Have a great day!",
                voice="man",
            )

            async def _finalize() -> None:
                try:
                    feedback = await self._generate_interview_feedback(interview)
                    interview['feedback'] = feedback
                    await self._save_interview_results(interview)
                except Exception as e:
                    logger.error(f"Failed finalizing interview for call_sid={call_sid}: {e}", exc_info=True)
                finally:
                    try:
                        self.active_interviews.pop(call_sid, None)
                    except Exception:
                        pass

            asyncio.create_task(_finalize())
            response.hangup()
            return Response(content=str(response), media_type="application/xml")

        # Small-talk opener: acknowledge the candidate's greeting and then move
        # into the structured interview questions.
        if interview.get('intro_smalltalk_pending') and (user_response or recording_url):
            interview['intro_smalltalk_pending'] = False
            ack = self._acknowledge_answer(user_response or "", 0, has_recording=bool(recording_url))
            self._append_tts(response, ack, voice="man")

            questions = interview.get('questions') or []
            if interview.get('current_question', 0) >= len(questions):
                return await self._complete_interview(call_sid)

            question = questions[interview['current_question']]
            interview['current_question'] += 1
            gather = self._append_speech_gather(response, call_sid)
            self._append_tts(gather, question, voice="man")
            return Response(content=str(response), media_type="application/xml")
        
        # If user provided a response, decide whether it's a real answer or
        # a request to repeat/clarify the question.
        if user_response or recording_url:
            normalized = (user_response or "").strip().lower()

            # Ignore low-confidence/noise inputs (common with background noise).
            # Treat them as "no input" and re-ask the same question.
            if self._is_noise_input(user_response, confidence):
                current_q_idx = max(interview.get("current_question", 0) - 1, 0)
                interview["no_input_attempts"] = int(interview.get("no_input_attempts") or 0) + 1
                attempts = int(interview.get("no_input_attempts") or 0)
                max_attempts = int(os.getenv("TWILIO_NO_INPUT_MAX_ATTEMPTS", "1") or "1")
                max_attempts = max(1, min(max_attempts, 5))
                question = interview["questions"][current_q_idx] if interview.get("questions") else ""
                # Only mention background noise after multiple failed attempts.
                prompt = "Sorry, could you repeat that?"
                if attempts >= 2:
                    prompt = "Sorry, I’m having trouble hearing you clearly. Could you repeat that?"

                if attempts <= max_attempts:
                    self._append_tts(response, prompt, voice="man")
                    gather = self._append_speech_gather(response, call_sid)
                    self._append_tts(gather, question, voice="man")
                    return Response(content=str(response), media_type="application/xml")

                # Too many no-input/noise events: move on rather than frustrating the candidate.
                interview["no_input_attempts"] = 0
                self._append_tts(response, "No problem—let’s move to the next question.", voice="man")
                return await self.handle_question(call_sid, user_response=None)

            if not normalized:
                current_q_idx = max(interview.get("current_question", 0) - 1, 0)
                question = interview["questions"][current_q_idx] if interview.get("questions") else ""
                self._append_tts(
                    response,
                    "Sorry, I didn't catch that. Could you please repeat?",
                    voice="man",
                )
                gather = self._append_speech_gather(response, call_sid)
                self._append_tts(gather, question, voice="man")
                return Response(content=str(response), media_type="application/xml")

            # Treat responses that clearly ask to repeat (containing the word
            # 'repeat') as clarification requests rather than normal answers.
            is_clarification = "repeat" in normalized

            if is_clarification and interview['current_question'] > 0:
                # Do not record this as an answer; instead, acknowledge and
                # repeat the same question in a friendly way.
                current_q_idx = max(interview['current_question'] - 1, 0)
                question = interview['questions'][current_q_idx]

                self._append_tts(
                    response,
                    "Of course—happy to repeat that.",
                    voice="man",
                )
                gather = self._append_speech_gather(response, call_sid)
                self._append_tts(gather, question, voice="man")
                return Response(content=str(response), media_type="application/xml")

            # Otherwise, treat it as a normal answer.
            answered_idx = max(interview['current_question'] - 1, 0)
            answered_question = interview['questions'][answered_idx] if interview.get('questions') else ""
            interview['answers'].append({
                'question': answered_question,
                'answer': user_response or "",
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
            # Reset no-input counter on a real answer
            interview["no_input_attempts"] = 0
            # Engaging, varied acknowledgement
            ack = self._acknowledge_answer(user_response, answered_idx, has_recording=False)
            self._append_tts(response, ack, voice="man")
        
        # Check if we have more questions
        if interview['current_question'] >= len(interview['questions']):
            interview["closing_questions_pending"] = True
            gather = self._append_speech_gather(response, call_sid)
            self._append_tts(
                gather,
                "Before we wrap up, do you have any questions or doubts you’d like me to note for our team?",
                voice="man",
            )
            return Response(content=str(response), media_type="application/xml")
        
        # Ask the next question in a natural way. The introduction already
        # explained that the candidate can say 'repeat' if needed, so we
        # don't repeat that instruction on every question.
        question = interview['questions'][interview['current_question']]
        interview['current_question'] += 1
        
        # Configure speech gathering to feel more natural and not cut the
        # candidate off too quickly. Give them longer to start speaking and
        # allow a few seconds of silence before assuming they are done.
        gather = self._append_speech_gather(response, call_sid)
        self._append_tts(gather, question, voice="man")

        # Let Twilio post back to the /api/interview/answer action when the caller
        # finishes speaking or if no input is received. The answer handler will
        # decide whether to retry, move on, or end the interview.
        return Response(content=str(response), media_type="application/xml")

    async def handle_timeout(self, call_sid: str, confidence: str = None) -> Response:
        """Handle no-input events without skipping questions too aggressively."""
        if call_sid not in self.active_interviews:
            return self._end_interview("Sorry, I couldn't find your interview session. Please try again later.")

        interview = self.active_interviews[call_sid]
        response = VoiceResponse()

        questions = interview.get("questions") or []
        if interview.get("current_question", 0) >= len(questions):
            return await self._complete_interview(call_sid)

        attempts = int(interview.get("no_input_attempts") or 0) + 1
        interview["no_input_attempts"] = attempts

        max_attempts = int(os.getenv("TWILIO_NO_INPUT_MAX_ATTEMPTS", "2") or "2")
        max_attempts = max(1, min(max_attempts, 5))

        # The last question we asked is current_question - 1 (because handle_question
        # increments current_question before appending <Gather>).
        q_idx = max(int(interview.get("current_question") or 0) - 1, 0)
        question = questions[q_idx] if q_idx < len(questions) else ""

        if attempts <= max_attempts:
            self._append_tts(
                response,
                "I didn’t catch that—could you say it again?",
                voice="man",
            )
            gather = self._append_speech_gather(response, call_sid)
            self._append_tts(gather, question, voice="man")
            return Response(content=str(response), media_type="application/xml")

        # After repeated no-inputs, move on (but do not end the whole interview).
        interview["no_input_attempts"] = 0
        self._append_tts(
            response,
            "No problem—let’s move to the next question.",
            voice="man",
        )
        # Ask next question
        return await self.handle_question(call_sid, user_response=None)

    async def _complete_interview(self, call_sid: str) -> Response:
        """Complete the interview and provide feedback."""
        interview = self.active_interviews.pop(call_sid, {})
        response = VoiceResponse()
        self._append_tts(
            response,
            "Thank you again for your time today. I’ll follow up shortly with the next step. Have a great day!",
            voice="man",
        )

        async def _finalize() -> None:
            try:
                # Generate feedback using AI, but do NOT read it aloud on the call.
                # We store it for internal review and dashboard use only.
                feedback = await self._generate_interview_feedback(interview)
                interview['feedback'] = feedback

                # Save interview results (including feedback) to the database and transcript
                await self._save_interview_results(interview)
            except Exception as e:
                logger.error(f"Failed finalizing interview for call_sid={call_sid}: {e}", exc_info=True)

        asyncio.create_task(_finalize())
        
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

    async def _generate_interview_questions(
        self,
        job_description: str,
        important_questions: Optional[List[str]] = None,
    ) -> List[str]:
        """Generate interview questions based on the job description."""
        prompt = f"""
        You are James Chandler, a healthcare recruiter at Radixsol, interviewing a healthcare professional by phone.
        Generate 5-7 clear, spoken interview questions based on the following job description.

        Requirements:
        - Use ONLY the information in the job description to guide what you ask.
        - Include 1-3 **critical screening questions** about the most important requirements
          (for example mandatory clinical experience, required specialties, shift type,
          location constraints, or minimum years of experience).
        - If the job description mentions any certifications, licenses, or registrations
          (for example RN license, BLS, ACLS, etc.), you MUST include at least one question
          that explicitly asks the candidate to confirm they have those certifications/licenses
          and that they are current.
        - The **rest of the questions** should be broader, non-requirement questions that help
          the recruiter understand the candidate as a healthcare professional, such as:
          experience with certain patient populations, communication style, dealing with stress,
          teamwork, reasons for interest in the role, or how they handle common clinical scenarios.
        - Keep each question short and easy to understand when spoken over the phone.
        - Do NOT include explanations or commentary, only the questions themselves.

        Job Description:
        {job_description}

        Return ONLY the questions as a JSON array of strings.
        """
        
        try:
            response = await self.gemini_model.generate_content_async(prompt)
            raw_text = (response.text or "").strip()

            # Log the raw Gemini output for debugging
            logger.debug(f"Raw Gemini questions response: {raw_text}")

            # If nothing came back, fall back immediately
            if not raw_text:
                return self._default_questions()

            # First, try to parse as JSON as requested in the prompt
            try:
                questions = json.loads(raw_text)
                if isinstance(questions, list) and all(isinstance(q, str) for q in questions):
                    base_questions = questions
                    extra = [q.strip() for q in (important_questions or []) if isinstance(q, str) and q.strip()]
                    if extra:
                        base_questions = base_questions + extra
                    return base_questions
            except json.JSONDecodeError:
                # Try to recover if the model wrapped JSON in a Markdown code block
                try:
                    if "```" in raw_text:
                        # Extract content between the first pair of backticks
                        parts = raw_text.split("```")
                        if len(parts) >= 3:
                            inner = parts[1]
                            # Strip potential language hint like ```json
                            inner = "\n".join(inner.split("\n")[1:]) if "\n" in inner else inner
                            inner = inner.strip()
                            questions = json.loads(inner)
                            if isinstance(questions, list) and all(isinstance(q, str) for q in questions):
                                base_questions = questions
                                extra = [q.strip() for q in (important_questions or []) if isinstance(q, str) and q.strip()]
                                if extra:
                                    base_questions = base_questions + extra
                                return base_questions
                except Exception:
                    # Fall through to plain-text fallback below
                    pass

            # As a fallback, treat each non-empty line as a question
            lines = [ln.strip("- ") for ln in raw_text.splitlines() if ln.strip()]
            base_questions = lines[:7] if lines else self._default_questions()
            extra = [q.strip() for q in (important_questions or []) if isinstance(q, str) and q.strip()]
            if extra:
                base_questions = base_questions + extra
            return base_questions

        except Exception as e:
            logger.error(f"Error generating questions: {e}")
            base_questions = self._default_questions()
            extra = [q.strip() for q in (important_questions or []) if isinstance(q, str) and q.strip()]
            if extra:
                base_questions = base_questions + extra
            return base_questions
    
    async def _generate_interview_feedback(self, interview: Dict) -> str:
        """Generate feedback for the candidate based on their answers."""
        try:
            prompt = f"""
            Based on the following interview, provide brief, constructive feedback for internal recruiter review.
            
            Job Description:
            {interview['job_description']}
            
            Questions and Answers:
            """
            
            for i, (q, a) in enumerate(zip(interview['questions'], interview.get('answers', []))):
                prompt += f"\nQ{i+1}. {q}\nA: {a.get('answer', 'No response')}\n"
            
            prompt += "\nPlease provide 2-3 sentences of constructive feedback."
            
            response = await self.gemini_model.generate_content_async(prompt)
            return response.text.strip()
            
        except Exception as e:
            logger.error(f"Error generating feedback: {e}")
            return "We appreciate your time and will review your responses carefully."
    
    async def _save_interview_results(self, interview: Dict):
        """Save interview results to the database."""
        try:
            from .main import InterviewResult  # Import here to avoid circular import at module load

            logger.info(f"Saving interview results for {interview.get('phone_number')}")

            # Prepare payload
            phone_number = interview.get('phone_number')
            job_description = interview.get('job_description', '')
            answers_json = json.dumps(interview.get('answers', []), default=str)
            feedback_text = interview.get('feedback', '') if isinstance(interview.get('feedback'), str) else ''

            result = InterviewResult(
                phone_number=phone_number,
                job_description=job_description,
                answers_json=answers_json,
                feedback_text=feedback_text,
            )

            self.db.add(result)
            self.db.commit()

            logger.debug(f"Interview details saved: {json.dumps(interview, indent=2, default=str)}")

            # Additionally, store a human-readable transcript file on disk
            self._save_transcript_file(interview)

            # And store a separate performance analytics file for this interview
            self._save_performance_file(interview)
        except Exception as e:
            logger.error(f"Error saving interview results: {e}")
            self.db.rollback()

    def _save_transcript_file(self, interview: Dict) -> None:
        """Write a text transcript of the interview to the transcripts folder."""
        try:
            transcripts_dir = os.getenv("TRANSCRIPTS_DIR", "transcripts")
            os.makedirs(transcripts_dir, exist_ok=True)

            phone_number = interview.get('phone_number') or 'unknown'
            ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            filename = f"interview_{phone_number}_{ts}.txt"
            path = os.path.join(transcripts_dir, filename)

            lines = []
            lines.append(f"Phone: {phone_number}")
            lines.append(f"Start Time (UTC): {interview.get('start_time')}")
            lines.append("")
            lines.append("Job Description:")
            lines.append(interview.get('job_description', '').strip())
            lines.append("\nQuestions and Answers:\n")

            for idx, ans in enumerate(interview.get('answers', []), start=1):
                q = ans.get('question', '')
                a = ans.get('answer', '')
                t = ans.get('timestamp', '')
                lines.append(f"Q{idx}: {q}")
                lines.append(f"A{idx}: {a}")
                lines.append(f"Timestamp: {t}")
                lines.append("")

            transcript_text = "\n".join(lines)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(transcript_text)

            logger.info(f"Interview transcript saved to {path}")
        except Exception as e:
            logger.error(f"Error saving interview transcript file: {e}")
    def _save_performance_file(self, interview: Dict) -> None:
        """Write a simple performance analytics file for the interview."""
        try:
            performance_dir = os.getenv("PERFORMANCE_DIR", "Performance")
            os.makedirs(performance_dir, exist_ok=True)

            phone_number = interview.get('phone_number') or 'unknown'
            ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            filename = f"performance_{phone_number}_{ts}.txt"
            path = os.path.join(performance_dir, filename)

            questions = interview.get('questions', []) or []
            answers = interview.get('answers', []) or []
            total_questions = len(questions)
            total_answers = len(answers)
            completion_rate = 0.0
            if total_questions > 0:
                completion_rate = round((total_answers / total_questions) * 100, 1)

            feedback_text = interview.get('feedback', '') if isinstance(interview.get('feedback'), str) else ''

            lines = []
            lines.append(f"Phone: {phone_number}")
            lines.append(f"Start Time (UTC): {interview.get('start_time')}")
            lines.append("")
            lines.append("Performance Summary:")
            lines.append(f"  Total questions asked: {total_questions}")
            lines.append(f"  Total answers recorded: {total_answers}")
            lines.append(f"  Answer completion rate: {completion_rate}%")
            lines.append("")
            lines.append("AI Feedback:")
            lines.append(feedback_text.strip() or "(No feedback generated)")

            performance_text = "\n".join(lines)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(performance_text)

            logger.info(f"Interview performance analytics saved to {path}")
        except Exception as e:
            logger.error(f"Error saving performance analytics file: {e}")
    @staticmethod
    def _default_questions() -> List[str]:
        """Return default questions if AI generation fails."""
        return [
            "Can you tell me about your experience with Python?",
            "How do you approach debugging complex issues?",
            "Can you describe a challenging project you worked on?",
            "How do you handle working in a team environment?",
            "What are your thoughts on test-driven development?"
        ]
    
    @staticmethod
    def _end_interview(message: str) -> Response:
        """End the interview with an error message."""
        response = VoiceResponse()
        response.say(message, voice='man')
        response.hangup()
        return Response(content=str(response), media_type="application/xml")
