# 🛠️ School Agent — Developer Journey & Engineering Log (`about_dev.md`)

> **Document Purpose:** A complete, chronological account of the conception, architectural design, iterative development, bugs encountered, and engineering solutions applied throughout the creation of the **School Agent** platform.

---

## 📑 Table of Contents
1. [Project Origin & Initial Vision](#1-project-origin--initial-vision)
2. [Chronological Development Phases](#2-chronological-development-phases)
   - [Phase 1: Foundation & Initial Architecture](#phase-1-foundation--initial-architecture)
   - [Phase 2: Moodle Session Authentication & Scraping](#phase-2-moodle-session-authentication--scraping)
   - [Phase 3: Scraping Overview Grades & The Missing History Bug](#phase-3-scraping-overview-grades--the-missing-history-bug)
   - [Phase 4: Solving Unscheduled Deadlines & Lazy Teacher Posts](#phase-4-solving-unscheduled-deadlines--lazy-teacher-posts)
   - [Phase 5: Productivity Tools (AI Action Plans & Bulk Management)](#phase-5-productivity-tools-ai-action-plans--bulk-management)
   - [Phase 6: Academic Period Transitions & Smart Cascading Filters](#phase-6-academic-period-transitions--smart-cascading-filters)
   - [Phase 7: Visual Refinements, Layout Polish & Dark Mode Dropdowns](#phase-7-visual-refinements-layout-polish--dark-mode-dropdowns)
3. [Summary of Key Technical Challenges & Solutions](#3-summary-of-key-technical-challenges--solutions)
4. [Core Architectural Decisions](#4-core-architectural-decisions)
5. [Future Roadmap & Extensibility](#5-future-roadmap--extensibility)

---

## 1. Project Origin & Initial Vision

The project was born out of real-world friction experienced by a student at **Universidad Ciudadana de Nuevo León (UCNL)** using their Moodle LMS (`https://licenciatura.ucnl.edu.mx/`). 

### The Real-World Problems:
- **Disappearing Academic History:** When a four-month period (*tetramestre*) finishes, courses and grades disappear from the active student view. Students have no built-in personal transcript or GPA calculator.
- **Unorganized Calendar Deadlines:** Professors frequently do not register calendar activities. Instead, they announce critical deliverables in discussion forums, emails, or course page descriptions.
- **Generic iCal Feeds:** The exported iCal feed produces cryptic event names (e.g., *"Actividad de Regularización cierra"*) with no course association or direct links.
- **Lack of Study Synthesis:** Students spend too much time organizing and figuring out what to do rather than actually studying.

### The Objective:
Create **School Agent** — a local, privacy-first, autonomous academic command center that automatically scrapes courses and grades, unifies calendar and forum deadlines, provides 1-click AI homework execution plans, and offers an intelligent multimodal chatbot aware of the student's exact academic situation.

---

## 2. Chronological Development Phases

### Phase 1: Foundation & Initial Architecture
- **Backend Setup:** Constructed a high-performance **FastAPI** backend with asynchronous SQLAlchemy (`aiosqlite`) supporting SQLite.
- **Data Models:** Designed relational models:
  - `Term`: Represents academic periods (*1er, 2do, 3er, 4to Tetramestre*) with GPA and active status flags.
  - `Course`: Represents subjects with course codes, term links, grades, status (*enrolled*, *completed*), and Moodle URLs.
  - `Assignment`: Deliverables with due dates, priority, submission requirements, and origin tracking (`source`: *calendar*, *teacher_post*, *course_page*, *manual*).
  - `StudyNote`: Lecture summaries and active recall flashcards.
  - `ChatMessage`: Full conversation memory between the student and the AI agent.
- **Frontend Setup:** Built a responsive Single Page Application (SPA) using **Tailwind CSS**, **FontAwesome 6**, and **Marked.js** with zero Node.js build overhead.
- **AI Integration:** Integrated Google's official `google-genai` SDK using `gemini-3.5-flash-lite` for ultra-fast, structured reasoning.

---

### Phase 2: Moodle Session Authentication & Scraping
- **Initial Idea:** Use Playwright to launch a browser, let the user log into Moodle, and persist the session cookies locally.
- **Bug Faced:**
  ```text
  Scraping error: BrowserType.launch_persistent_context() got an unexpected keyword argument 'storage_state'
  ```
- **Root Cause:** In Playwright Python, `launch_persistent_context()` takes `user_data_dir` as its primary positional argument; `storage_state` is a parameter for `browser.new_context()`, not `launch_persistent_context()`.
- **Solution Applied:**
  Refactored `moodle_scraper.py` into a two-tiered authentication workflow:
  1. `launch_interactive_login()` opens a dedicated Chromium window targeting `data/playwright_profile` where the user logs in once.
  2. The authenticated storage state is exported cleanly to `data/moodle_auth.json`.
  3. All automated scrapers load `storage_state="data/moodle_auth.json"` with headless Chromium.

---

### Phase 3: Scraping Overview Grades & The Missing History Bug
- **The Issue:**
  The student completed the 3rd tetramestre with all classes passed and asked the AI chatbot:
  > *"Tell me what courses did I take on the 3rd grade and what grades did I get?"*
  The chatbot replied: *"I don't have access to your specific course names or grades... my memory only shows general placeholders."*
- **Root Cause:**
  Moodle's home dashboard (`/my/`) replaced past enrolled courses with generic image links (`mod/page/view.php`) without course names or final grades.
- **The Engineering Solution:**
  1. **Deep Grade Report Scraper:** Pointed Playwright to Moodle's hidden official Grade Overview Report:
     `https://licenciatura.ucnl.edu.mx/grade/report/overview/index.php`
  2. Playwright parsed the official table containing all 6 enrolled courses, their exact names (*Cálculo Integral, Estructuras de Datos, etc.*), and final numerical grades.
  3. **Grades Overview Dashboard:** Built a dedicated **Academic Records** tab in the UI:
     - **Cumulative GPA (Promedio General)** and **Active Period GPA** cards.
     - **Active Period Card** showcasing enrolled subjects and grades.
     - **Historical Archived Periods** preserving past tetramestres permanently.
     - Interactive **"Add Course / Past Grade"** modal for backfilling 1st and 2nd tetramestres.

---

### Phase 4: Solving Unscheduled Deadlines & Lazy Teacher Posts
- **The Problem:**
  Teachers rarely set up official calendar items; assignments are described inside course pages or informal forum announcements. The calendar iCal feed exported vague titles like *"Actividad 1 cierra"*.
- **The Solution:**
  1. **Direct Course Page Scraper (`_scrape_course_activities_sync`):**
     Playwright visits individual course pages (e.g. *Cálculo Integral* `course/view.php?id=18919`), scans all `mod/assign` (homeworks) and `mod/quiz` (exams), cleans titles with regex, and captures direct submission links.
  2. **iCal Exact Activity Matching:**
     Calendar event UIDs map directly to Moodle activity IDs (`{mod_id}@licenciatura.ucnl.edu.mx`), linking vague calendar entries directly to course subjects.
  3. **AI Post Extractor (`extract_tasks_from_teacher_post`):**
     A dedicated Gemini tool where students can paste unformatted WhatsApp messages or forum notices; Gemini extracts the deliverable, computes ISO timestamps, and detects file format rules (e.g. *PDF only, 30MB limit*).

---

### Phase 5: Productivity Tools (AI Action Plans & Bulk Management)
- **1-Click AI Breakdown Plans (`POST /api/assignments/{id}/ai-breakdown`):**
  Added an **"🤖 AI Plan"** button to every assignment card. Gemini analyzes the deliverable title, course context, and instructions to generate:
  - Objective statement.
  - Realistic time estimate (e.g., *1.5 hours*).
  - 3-step structured execution guide.
  - Interactive submission checklist.
  - "Pro-Tips for 10/10" rubric advice.
- **Bulk Selection & Neon Floating Toolbar:**
  - Added checkboxes to every assignment card and a master **"Select All"** toggle.
  - Implemented batch backend endpoints (`POST /api/assignments/bulk-update` and `POST /api/assignments/bulk-delete`).
  - Added a high-contrast floating action bar with neon glow outline (`ring-4 ring-indigo-500/25 shadow-[0_0_35px_rgba(99,102,241,0.55)]`) to batch complete, revert, or delete items.

---

### Phase 6: Academic Period Transitions & Smart Cascading Filters
- **The Question:**
  > *"When a new Tetramestre is added, will those items in Deadlines & Tasks disappear or does the user have to manually erase them?"*
- **The Solution (Zero Data Loss Architecture):**
  - Updated `GET /api/assignments` to accept `term_id`.
  - Deliverables are linked to courses, which belong to terms.
  - When the user starts the **4th Tetramestre** and marks it as *Current*, Deadlines & Tasks automatically defaults to the active period.
  - Historical deliverables from the 3rd Tetramestre remain safely archived and searchable under *All History* or *3er Tetramestre (Archived)* without manual deletion.
- **Smart Cascading Filters:**
  Implemented `updateCourseFilterOptions()`: selecting a **Period** dynamically repopulates the **Course** dropdown to show only courses enrolled in that specific period with live item counts.

---

### Phase 7: Visual Refinements, Layout Polish & Dark Mode Dropdowns
- **Scrollbar Margin Alignment:**
  - *Issue:* Scrollbars were positioned next to inner centered containers (`max-w-6xl mx-auto`), creating floating gaps ("air") on wide monitors.
  - *Fix:* Moved `overflow-y-auto` to full-width containers (`w-full flex-1 overflow-y-auto`) with transparent tracks (`background: transparent`), pinning scrollbars flush against the browser border.
- **Single-Row Organized Filter Toolbar:**
  Replaced loose filter text labels with unified, self-contained pill selectors (`bg-slate-950 border border-slate-800 rounded-xl px-3 py-1.5`) with color-coded FontAwesome icons.
- **Dark Mode `<select>` & `<option>` Fix:**
  - *Issue:* Clicking filter dropdowns on Windows Chromium browsers rendered white backgrounds with white text.
  - *Fix:* Added `color-scheme: dark;` along with explicit `select option { background-color: #0f172a; color: #f8fafc; }` and hover states in CSS.

---

## 3. Summary of Key Technical Challenges & Solutions

| Challenge / Bug | Root Cause | Engineering Solution |
|---|---|---|
| `launch_persistent_context()` error | Invalid keyword argument in Playwright Python | Separated interactive login (`launch_persistent_context`) from automated scraping (`new_context(storage_state=...)`). |
| Empty course names & grades in chatbot memory | Moodle `/my/` dashboard hides past terms | Built automated scraper targeting Moodle's official Grade Overview Report (`/grade/report/overview/index.php`). |
| Windows Asyncio Loop conflicts with Playwright | Playwright sync API conflicts with FastAPI async event loop | Wrapped all synchronous Playwright calls in `await asyncio.to_thread(...)`. |
| Unscheduled teacher homeworks | Teachers post in forums/messages instead of calendar | Built Gemini AI Natural Language Task Extractor & Deep Course Page Scanner (`course/view.php?id=...`). |
| Vague iCal calendar events | Moodle iCal export strips course names | Correlated iCal event UIDs (`{mod_id}@...`) with parsed course module URLs (`mod/assign/view.php?id={mod_id}`). |
| White-on-white text in select dropdowns | Chromium OS default popup styling on transparent selects | Added `color-scheme: dark;` and explicit CSS rules for `select` and `option`. |
| Floating scrollbars creating "air" in layout | `overflow-y-auto` placed on nested `max-w-6xl` containers | Moved scroll containers to `w-full flex-1` with centered inner wrappers. |

---

## 4. Core Architectural Decisions

1. **Zero-Build Single Page Application:**
   - Utilizing native ESM, Tailwind CSS via CDN, and Vanilla JS allows the entire app to run immediately with `python main.py` without requiring Node.js, Webpack, or Vite installations.
2. **Local Privacy First:**
   - Moodle session cookies (`data/moodle_auth.json`) and database records (`data/school_agent.db`) are stored 100% locally on the student's machine.
3. **Structured AI Outputs:**
   - Using Pydantic schemas and Gemini's JSON mode ensures deterministic AI extraction for homework deadlines, time estimates, and rubric checklists without parsing errors.
4. **Relational Term & Course Hierarchy:**
   - `Term` ➔ `Course` ➔ `Assignment` ensures permanent data preservation across multi-year academic degrees.

---

## 5. Future Roadmap & Extensibility

- **Automated WhatsApp / Telegram Webhook:** Forward teacher WhatsApp messages directly to School Agent to auto-schedule deliverables.
- **Push Notifications & Reminder Daemon:** Windows desktop notifications 24 hours before assignment due dates.
- **Exam Simulation Studio:** AI-generated timed mock exams generated from student notes and lecture PDFs.

---

*Authored as the definitive technical history of the School Agent development lifecycle.*
