Reply Optimizer – AI Email Assistant
Reply Optimizer is a web application for automatically processing and generating professional replies to incoming corporate emails using AI (Google Gemini).
The application monitors IMAP inboxes, analyzes emails, generates replies based on specified instructions, and sends them via SMTP. Everything runs in the background, with complete user isolation.
Features

Automated professional email replies with Google Gemini
Support for multiple IMAP/SMTP accounts per user
Flexible instructions (role prompt) for customizing response styles
Complete data and log isolation between users
Convenient web dashboard: Mails, Settings, Logs
Separate log files for each session
Sending metrics to Datadog (latency, tokens, errors, etc.)
Ready for migration to Vertex AI Gemini

Technologies

Backend: Flask (Python)
AI: Google Gemini API (ready for migration to Vertex AI)
Email: aioimaplib + aiosmtplib (asynchronous processing)
Database: SQLite (with migration to PostgreSQL in production)
Monitoring: Datadog (metrics, health checks)
Frontend: HTML + vanilla JavaScript + CSS

How to run locally
Requirements

Python 3.11+
Google Gemini API key (or Vertex AI in the future)
Datadog API/App keys (optional for metrics)

Installation
Bashgit clone https://github.com/your-username/reply-optimizer.git
cd reply-optimizer

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate # Linux/Mac
# .venv\Scripts\activate # Windows

pip install -r requirements.txt
Configuration

Create a .env file (or just use the settings in the UI): text# Optional, everything can be configured through the web interface
Run the application:

Bashpython app.py

Open in a browser: http://localhost:5004
Register (the first user is created through the registration form)
In Settings, specify:
Gemini API key
Datadog keys (if any)
IMAP/SMTP details for your mailboxes
Response instructions

Deployment
The application is easily deployed to:

Render.com
Railway.app
Fly.io
Google Cloud Run

Architecture

Flask - web server and API
SessionManager - manages background workers for each session
SessionWorker - asynchronously monitors IMAP, processes emails, and sends responses
DatadogMetrics - sends metrics and health checks
Logs - separate file for each session (logs/session_*.log)
