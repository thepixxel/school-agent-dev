# 🎓 School Agent - AI Personal Class Assistant

An intelligent personal academic assistant tailored for online classes, automated Moodle synchronization, deadline tracking, active recall study tools, and Google services integration.

---

## ✨ Features

- 💬 **Context-Aware AI Assistant**: Powered by Google Gemini (`gemini-3.7-flash`), always informed of your active classes, upcoming deadlines, and study notes.
- 📅 **Deadlines & Assignment Tracker**: Filter by course, status (Pending, In Progress, Done), urgency badges (Overdue, Due Soon), and 1-click Google Calendar integration.
- 🔄 **Automated Moodle Scraper & Sync**:
  - **Browser Automation:** Launches Playwright using a persistent browser profile so you log in once with saved credentials, then scrapes course links, assignments, and calendar events.
  - **iCal Feed Sync:** Zero-configuration instant deadline sync via Moodle's export calendar link.
- 📚 **Study Studio & Flashcards**:
  - AI Lecture Summarizer & takeaway generator.
  - Active recall 3D flip flashcards.
  - **Google Docs Export**: Formatted HTML / clean Markdown ready for Google Docs.
- ✉️ **Professor Email Drafter**: Professional templates for extension requests, questions, absence notices, and office hour requests.

---

## 🚀 Quick Start

### 1. Configure Settings
You can either edit `.env` or set these directly in the **Settings** tab in the Web Dashboard:
- `GEMINI_API_KEY`: Get a free key at [Google AI Studio](https://aistudio.google.com/).
- `MOODLE_URL`: Your school's Moodle web address (e.g. `https://moodle.yourschool.edu`).

### 2. Launch the Assistant
Run:
```bash
python main.py
```
Open your browser and navigate to:
```
http://127.0.0.1:8000
```

---

## 🛠️ Project Structure
```
school-agent/
├── backend/
│   ├── ai_service.py       # Google Gemini API integration
│   ├── app.py              # FastAPI server & REST API
│   ├── config.py           # Settings & environment variables
│   ├── database.py         # SQLite database & SQLAlchemy models
│   ├── google_export.py    # Google Calendar links, ICS & Google Docs formatters
│   └── moodle_scraper.py   # Playwright persistent session scraper & iCal parser
├── data/                   # SQLite database & browser profile storage
├── static/
│   └── index.html          # Web UI & Chatbot dashboard
├── .env                    # Environment variables
├── main.py                 # Application launcher
└── requirements.txt        # Python dependencies
```
