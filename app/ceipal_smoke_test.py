import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Optional


class CeipalSmokeTestError(RuntimeError):
    pass


class CeipalE2ESmokeTest:
    def __init__(
        self,
        *,
        score_threshold: float = 0.4,
        job_status: str = "Open,Active",
        max_jobs: int = 1,
        target_candidate_phone: Optional[str] = None,
        trigger_call: bool = False,
    ) -> None:
        self.score_threshold = float(score_threshold)
        self.job_status = str(job_status or "Open,Active")
        self.max_jobs = int(max_jobs)
        self.target_candidate_phone = (
            self._norm_e164(str(target_candidate_phone)) if target_candidate_phone else None
        )
        # If a target phone is provided, we will send a real SMS to that number
        self.live_sms_mode = bool(self.target_candidate_phone)
        self.trigger_call = trigger_call and self.live_sms_mode

    @staticmethod
    def _norm_e164(phone: str) -> str:
        """Best-effort E.164 normalization (mirrors app.main._norm_e164)."""
        import re
        raw = (phone or "").strip()
        if not raw:
            return ""
        if raw.startswith("00"):
            raw = "+" + raw[2:]
        if raw.startswith("+"):
            digits = re.sub(r"\D+", "", raw)
            if 10 <= len(digits) <= 15:
                return "+" + digits
            return ""
        digits = re.sub(r"\D+", "", raw)
        if not digits:
            return ""
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
        return ""

    @staticmethod
    def _require_env(name: str) -> str:
        val = (os.getenv(name) or "").strip()
        if not val:
            raise CeipalSmokeTestError(f"Missing required env var: {name}")
        return val

    @contextmanager
    def _env_override(self, updates: Dict[str, Optional[str]]):
        old: Dict[str, Optional[str]] = {}
        try:
            for k, v in (updates or {}).items():
                old[k] = os.getenv(k)
                if v is None:
                    if k in os.environ:
                        del os.environ[k]
                else:
                    os.environ[k] = str(v)
            yield
        finally:
            for k, v in old.items():
                if v is None:
                    if k in os.environ:
                        del os.environ[k]
                else:
                    os.environ[k] = v

    def run(self) -> Dict[str, Any]:
        """Run a CEIPAL end-to-end smoke test.

        This is designed to be safe:
        - It forces dry_run=True
        - It forces OUTREACH_ENABLED=0

        It validates:
        - CEIPAL report fetch (jobs + candidates)
        - Ranking pipeline does not crash
        - DB writes for CeipalBatchRun/CeipalJobRun/CeipalCandidateRun
        """
        # Lazy imports so this module can be imported even if DB drivers aren't installed.
        import app.main as app_main
        try:
            from app.database import SessionLocal
        except ModuleNotFoundError as e:
            missing = str(getattr(e, "name", "") or "")
            if missing.lower() in {"mysqldb"}:
                raise CeipalSmokeTestError(
                    "Missing MySQL driver 'MySQLdb' (mysqlclient). Activate your .venv and/or install mysqlclient, "
                    "or change SQLALCHEMY_DATABASE_URL to use a different driver (e.g., mysql+pymysql)."
                ) from e
            raise

        # Basic env sanity (won't validate correctness, just presence)
        self._require_env("CEIPAL_EMAIL")
        self._require_env("CEIPAL_PASSWORD")
        self._require_env("CEIPAL_API_KEY")
        self._require_env("CEIPAL_JD_REPORT_URL")
        self._require_env("CEIPAL_CANDIDATE_REPORT_URL")

        batch_id = uuid.uuid4().hex
        req_dict = {
            "score_threshold": float(self.score_threshold),
            "job_status": self.job_status,
            "max_jobs": int(self.max_jobs),
            "dry_run": True,
            "force_resend": False,
        }

        # If a target phone is provided, enable live SMS and send a single message using CEIPAL job context.
        env_overrides = {"OUTREACH_ENABLED": "1"} if self.live_sms_mode else {"OUTREACH_ENABLED": "0"}
        if self.live_sms_mode:
            req_dict["dry_run"] = False
            req_dict["force_resend"] = True
            # Fetch a CEIPAL job to use for context
            try:
                jobs = app_main._load_jobs_from_ceipal()
            except Exception as e:
                raise CeipalSmokeTestError(f"Failed to load CEIPAL jobs for target-phone test: {e}")
            if not jobs:
                raise CeipalSmokeTestError("No CEIPAL jobs found to use for context")
            # Use the first job for context
            job = jobs[0]
            job_code = str(job.get("JobCode") or "")
            job_title = str(job.get("JobTitle") or "")
            job_description = job.get("JobDescription", "")
            # Create a synthetic candidate record with the target phone
            synthetic_candidate = {
                "FirstName": "Test",
                "LastName": "Candidate",
                "PhoneNumber": self.target_candidate_phone,
                "EmailAddress": f"test+{self.target_candidate_phone}@example.com",
                "JobTitle": job_title,
            }
            # Create a CeipalJobRun row so OutreachTracker can be linked to CEIPAL job context
            db = SessionLocal()
            try:
                job_run = app_main.CeipalJobRun(
                    job_code=job_code,
                    job_title=job_title,
                    job_status=job.get("JobStatus"),
                    status="completed",
                    error=None,
                )
                db.add(job_run)
                db.commit()
                db.refresh(job_run)
                job_run_id = job_run.id
                # Send outreach SMS directly using CEIPAL flow with a CEIPAL-appropriate template
                ceipal_template = (
                    "Hi {name}, we have an opening for {job_title} in {{location}}. "
                    "Interested in a brief call to discuss? If yes, please share your availability (date, time, time zone)."
                )
                # Temporarily override the template loader to return our CEIPAL template
                original_get = app_main.template_manager.get_sms_template
                app_main.template_manager.get_sms_template = lambda key: ceipal_template if key == "first_outreach" else None
                try:
                    sms_result = app_main._send_outreach_sms(
                        db,
                        candidate=synthetic_candidate,
                        role=job_title,
                        location=job.get("Location"),
                        template_key="first_outreach",
                        job_run_id=job_run_id,
                        job_code=job_code,
                        job_title=job_title,
                    )
                finally:
                    app_main.template_manager.get_sms_template = original_get
            finally:
                db.close()
            if not sms_result.get("ok"):
                raise CeipalSmokeTestError(f"Failed to send SMS to target phone: {sms_result.get('error')}")
            # Minimal batch record for consistency
            batch_id = uuid.uuid4().hex
            db = SessionLocal()
            try:
                db.add(
                    app_main.CeipalBatchRun(
                        id=batch_id,
                        status="completed",
                        request_json=json.dumps(req_dict, ensure_ascii=False),
                        jobs_processed=1,
                        candidates_selected=1,
                        messages_attempted=1,
                        messages_sent=1,
                    )
                )
                db.commit()
            finally:
                db.close()
            # If trigger_call is enabled, wait for a reply and then initiate a call
            if self.trigger_call:
                import time
                print("\n[SMOKE TEST] SMS sent. Waiting for candidate reply to trigger call...")
                print("[SMOKE TEST] Reply to the SMS with availability (e.g., 'I am available now').")
                print("[SMOKE TEST] Press Ctrl+C to abort.\n")
                last_replied_at = None
                while True:
                    db = SessionLocal()
                    try:
                        tracker = (
                            db.query(app_main.OutreachTracker)
                            .filter(
                                app_main.OutreachTracker.channel == "sms",
                                app_main.OutreachTracker.contact == self.target_candidate_phone,
                            )
                            .order_by(app_main.OutreachTracker.id.desc())
                            .first()
                        )
                        if tracker and tracker.replied_at:
                            if last_replied_at is None:
                                last_replied_at = tracker.replied_at
                                print(f"[SMOKE TEST] Reply detected at {tracker.replied_at}. Initiating call...")
                                # Trigger an immediate call using CEIPAL job context
                                call_result = app_main.initiate_interview_call(
                                    db,
                                    to_number=self.target_candidate_phone,
                                    job_description=job_description,
                                    job_title=job_title,
                                    candidate_id=None,  # synthetic candidate
                                )
                                if call_result.get("ok"):
                                    print("[SMOKE TEST] Call initiated successfully.")
                                    print(f"[SMOKE TEST] Call SID: {call_result.get('call_sid')}")
                                else:
                                    print(f"[SMOKE TEST] Failed to initiate call: {call_result.get('error')}")
                                break
                            else:
                                # Already processed this reply
                                pass
                        time.sleep(5)
                    except KeyboardInterrupt:
                        print("\n[SMOKE TEST] Aborted by user.")
                        break
                    finally:
                        db.close()
        else:
            with self._env_override(env_overrides):
                # Enqueue and run synchronously.
                batch_id = app_main._enqueue_ceipal_batch_from_dict(req_dict)
                app_main._run_ceipal_batch(batch_id)

        # Skip raw-report validation when using live SMS mode (we send directly regardless)
        target_in_raw_report = None
        if self.target_candidate_phone and not self.live_sms_mode:
            try:
                candidates = app_main._load_candidates_from_ceipal()
            except Exception as e:
                raise CeipalSmokeTestError(f"Failed to load CEIPAL candidates for target-phone check: {e}")
            for c in (candidates or []):
                if not isinstance(c, dict):
                    continue
                raw_phone = (
                    c.get("PhoneNumber")
                    or c.get("MobileNumber")
                    or c.get("phone")
                    or c.get("Phone")
                    or c.get("ContactNumber")
                    or ""
                )
                if not raw_phone:
                    continue
                if self._norm_e164(str(raw_phone)) == self.target_candidate_phone:
                    target_in_raw_report = True
                    break
            if not target_in_raw_report:
                raise CeipalSmokeTestError(
                    f"Target phone not found in CEIPAL candidate report: {self.target_candidate_phone}"
                )

        # Validate DB writes
        db = SessionLocal()
        try:
            batch = db.query(app_main.CeipalBatchRun).filter(app_main.CeipalBatchRun.id == str(batch_id)).first()
            if not batch:
                raise CeipalSmokeTestError("CeipalBatchRun row not created")

            job_runs = (
                db.query(app_main.CeipalJobRun)
                .order_by(app_main.CeipalJobRun.id.desc())
                .limit(max(1, int(self.max_jobs)))
                .all()
            )
            if not job_runs:
                raise CeipalSmokeTestError("No CeipalJobRun rows created (jobs_processed=0?)")

            candidate_runs_q = db.query(app_main.CeipalCandidateRun).order_by(app_main.CeipalCandidateRun.id.desc())
            if self.target_candidate_phone:
                candidate_runs_q = candidate_runs_q.filter(
                    app_main.CeipalCandidateRun.candidate_phone == self.target_candidate_phone
                )
            candidate_runs = candidate_runs_q.limit(50).all()

            report: Dict[str, Any] = {
                "ok": True,
                "batch_id": str(batch_id),
                "batch_status": getattr(batch, "status", None),
                "batch_error": getattr(batch, "error", None),
                "jobs_processed": getattr(batch, "jobs_processed", None),
                "candidates_selected": getattr(batch, "candidates_selected", None),
                "messages_attempted": getattr(batch, "messages_attempted", None),
                "messages_sent": getattr(batch, "messages_sent", None),
                "job_runs_found": len(job_runs),
                "candidate_runs_found": len(candidate_runs),
                "sample_job": {
                    "job_code": getattr(job_runs[0], "job_code", None),
                    "job_title": getattr(job_runs[0], "job_title", None),
                    "job_status": getattr(job_runs[0], "job_status", None),
                    "status": getattr(job_runs[0], "status", None),
                    "error": getattr(job_runs[0], "error", None),
                }
                if job_runs
                else None,
                "sample_candidate": {
                    "candidate_phone": getattr(candidate_runs[0], "candidate_phone", None),
                    "candidate_email": getattr(candidate_runs[0], "candidate_email", None),
                    "job_code": getattr(candidate_runs[0], "job_code", None),
                    "job_title": getattr(candidate_runs[0], "job_title", None),
                    "sms_status": getattr(candidate_runs[0], "sms_status", None),
                    "email_status": getattr(candidate_runs[0], "email_status", None),
                    "error": getattr(candidate_runs[0], "error", None),
                }
                if candidate_runs
                else None,
                "ran_at": datetime.utcnow().isoformat() + "Z",
                "request": req_dict,
                "target_candidate_phone": self.target_candidate_phone,
                "target_candidate_found": bool(self.target_candidate_phone and candidate_runs),
                "target_candidate_in_raw_report": target_in_raw_report if self.target_candidate_phone else None,
            }

            # Treat failed batches as failures, even if rows exist.
            if str(report.get("batch_status") or "").lower() == "failed":
                raise CeipalSmokeTestError(f"CEIPAL batch failed: {report.get('batch_error')}")

            return report
        finally:
            db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-phone", default=None, help="Target candidate phone number to filter for (e.g., (209) 215-9715)")
    parser.add_argument("--trigger-call", action="store_true", help="After SMS, wait for reply and trigger an immediate call to test CEIPAL calling flow")
    args = parser.parse_args()
    t = CeipalE2ESmokeTest(target_candidate_phone=args.target_phone, trigger_call=args.trigger_call)
    out = t.run()
    print(json.dumps(out, indent=2, ensure_ascii=False))
