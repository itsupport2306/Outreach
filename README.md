# Interview AI (CEIPAL Outreach + Scheduling)

FastAPI service that:

- Ingests jobs/candidates (CEIPAL BI reports)
- Sends outreach via **Twilio SMS** (and can initiate voice calls)
- Uses **Gemini** for message generation / conversation handling
- Persists runs and outreach state to a database via **SQLAlchemy**

## Tech Stack

- **Python 3.11+**
- **FastAPI** / **Uvicorn**
- **SQLAlchemy**
- **MySQL** (prod) or **SQLite** (dev)
- **Twilio** (SMS + Voice)
- **Google Gemini** (`google-generativeai`)
- **Alembic** (migrations)

## Project Structure

- `app/main.py` - FastAPI app + CEIPAL processing + core flow
- `app/ceipal_client.py` - CEIPAL client wrapper
- `app/messaging.py`, `app/messaging_service.py` - SMS/Voice integrations and message orchestration
- `app/interview_service.py`, `app/voice_handler.py` - Interview/call logic
- `app/database.py`, `app/models.py` - DB setup + ORM models
- `templates.json` - SMS/email templates used by the system
- `alembic/` - DB migrations

## Prerequisites

- Python 3.11+
- A database:
  - **SQLite** for local dev (default)
  - **MySQL** for production (recommended)
- Twilio account (SMS + Voice)
- Gemini API key
- (Optional) Google Calendar OAuth credentials if you want calendar scheduling

## Installation

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

## Configuration

The app reads configuration from environment variables (supports a local `.env`).

### Required (most setups)

- `GEMINI_API_KEY` - Gemini API key
- `DATABASE_URL`
  - SQLite example: `sqlite:///./interview_scheduler.db`
  - MySQL example: `mysql+mysqldb://USER:PASSWORD@HOST:3306/DBNAME`
- **Twilio**
  - `TWILIO_ACCOUNT_SID`
  - `TWILIO_AUTH_TOKEN`
  - `TWILIO_FROM_NUMBER` (or messaging service SID depending on your setup)
- `BASE_URL` - public URL for webhooks (use ngrok in dev)

### CEIPAL

- `CEIPAL_JOB_REPORT_URL` (or `CEIPAL_JD_REPORT_URL`)
- `CEIPAL_CANDIDATE_REPORT_URL`
- CEIPAL auth/env vars required by `build_ceipal_client_from_env()` (see `app/ceipal_client.py`)

### Optional

- `OUTREACH_ENABLED` - set to `1` to send live SMS; `0` to disable sending
- `DB_AUTO_CREATE_TABLES` - set to `1` if you want to auto-create tables (dev only)
- Google Calendar OAuth token/credentials under `.credentials/` (see app logs for exact path)

## Database Setup

### Option A: Alembic (recommended)

```bash
alembic upgrade head
```

### Option B: Auto-create tables (dev only)

Set:

```bash
DB_AUTO_CREATE_TABLES=1
```

Then run the app once.

## Run the API (Local)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Health checks:

- `GET /health`
- `GET /health/ceipal`

## Webhooks / Public URL (Twilio)

Twilio needs a public URL to reach your FastAPI endpoints.

In development, run ngrok:

```bash
ngrok http 8000
```

Set:

- `BASE_URL=https://<your-ngrok-subdomain>.ngrok-free.dev`

Then configure Twilio webhook(s) to point to the appropriate endpoints in `app/main.py`.

## Templates

Templates are loaded from `templates.json` via the template manager.

There is also a runtime reload endpoint:

- `POST /api/templates/reload`

(Consider protecting or removing this endpoint in production.)

## Deployment Notes (GitHub + Production)

- Do **NOT** commit:
  - `.env`
  - `.credentials/`
  - `logs/`
  - `.venv/`
  - any real CEIPAL exported reports (`ceipal_*_report.json`)
- Use a production DB (MySQL recommended).
- Put secrets in your deployment provider’s secret manager / env config.

## Security Notes

This system can send **real SMS/calls**.

- Keep `OUTREACH_ENABLED=0` in non-prod environments unless you are explicitly testing.
- Restrict/secure any admin or template reload endpoints.

---

If you want, tell me where you’re deploying (Render / Railway / EC2 / Azure / etc.) and I can tailor the deployment steps and recommended environment variables.
