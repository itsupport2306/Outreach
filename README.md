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

- `GOOGLE_API_KEY` - not required for spreadsheet outreach; legacy AI features are inactive
- `DATABASE_URL`
  - SQLite example: `sqlite:///./interview_scheduler.db`
  - MySQL example: `mysql+mysqldb://USER:PASSWORD@HOST:3306/DBNAME`
  - PostgreSQL example: `postgresql://USER:PASSWORD@HOST:5432/DBNAME` (normalized to psycopg 3 by `app.database`)
- **Twilio**
  - `TWILIO_ACCOUNT_SID`
  - `TWILIO_AUTH_TOKEN`
  - `TWILIO_PHONE_NUMBER`
- `BASE_URL` - public URL for webhooks (use ngrok in dev)

### CEIPAL

- `CEIPAL_JOB_REPORT_URL` (or `CEIPAL_JD_REPORT_URL`)
- `CEIPAL_CANDIDATE_REPORT_URL`
- CEIPAL auth/env vars required by `build_ceipal_client_from_env()` (see `app/ceipal_client.py`)

### Optional

- `SHEET_OUTREACH_ENABLED` - set to `1` to enable spreadsheet SMS sends
- `OUTREACH_ENABLED` - legacy candidate pipeline switch; keep `0` in spreadsheet-only mode
- `DB_AUTO_CREATE_TABLES` - set to `1` to create tables when deploying a new, empty database
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

Then run the app once. For production schema evolution, use Alembic migrations.

### Render PostgreSQL

Create a Render PostgreSQL database in the same region as the web service and set `DATABASE_URL` to its **Internal Database URL**. The app converts Render's `postgresql://` URL to `postgresql+psycopg://`; `psycopg[binary]` is included in `requirements.txt`. Set `DB_AUTO_CREATE_TABLES=1` for the initial empty database so the app creates its tables. The database is needed to retain outreach history, avoid resending to previously contacted numbers, and associate candidate replies with their outreach record.

For a Render Web Service, use:

```text
Build: pip install -r requirements.txt
Start: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

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

## Spreadsheet SMS outreach

Open the candidate workflow, choose **Outreach from spreadsheet**, and upload an `.xlsx` workbook with candidate name and phone columns. The app previews the message and recipient count before sending; rows with missing names, invalid phone numbers, duplicate phone numbers, or numbers previously contacted by this workflow are skipped. Sending requires `SHEET_OUTREACH_ENABLED=1`.

The Twilio inbound messaging webhook must point to `/api/sms/webhook`. Replies to spreadsheet campaigns are emailed to `OFFTOPIC_FORWARD_EMAIL`; the email includes candidate name and phone, the original outreach, the reply text, and a UTC timestamp. Configure `OFFTOPIC_FORWARD_EMAIL`, `SENDGRID_API_KEY`, and `SENDGRID_FROM_EMAIL`. Replies also continue through the existing candidate reply flow.

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
- Use persistent production storage (Render PostgreSQL is supported).
- Put secrets in your deployment provider’s secret manager / env config.

## Security Notes

This system can send **real SMS/calls**.

- Keep `SHEET_OUTREACH_ENABLED=0` outside of an intentional outreach campaign.
- Restrict/secure any admin or template reload endpoints.

---

If you want, tell me where you’re deploying (Render / Railway / EC2 / Azure / etc.) and I can tailor the deployment steps and recommended environment variables.
