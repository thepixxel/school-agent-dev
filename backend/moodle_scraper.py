import os
import sys
import re
import datetime
import asyncio
import subprocess
from typing import List, Dict, Any, Optional
import requests
from bs4 import BeautifulSoup
from icalendar import Calendar
from backend.config import DATA_DIR, get_moodle_url, get_moodle_ical_url

AUTH_STATE_FILE = str(DATA_DIR / "moodle_auth.json")

SPANISH_MONTHS = {
    "enero": 1, "ene": 1, "jan": 1, "january": 1,
    "febrero": 2, "feb": 2, "february": 2,
    "marzo": 3, "mar": 3, "march": 3,
    "abril": 4, "abr": 4, "apr": 4, "april": 4,
    "mayo": 5, "may": 5,
    "junio": 6, "jun": 6, "june": 6,
    "julio": 7, "jul": 7, "july": 7,
    "agosto": 8, "ago": 8, "aug": 8, "august": 8,
    "septiembre": 9, "sep": 9, "setiembre": 9, "sept": 9, "september": 9,
    "octubre": 10, "oct": 10, "october": 10,
    "noviembre": 11, "nov": 11, "november": 11,
    "diciembre": 12, "dic": 12, "dec": 12, "december": 12
}

def parse_moodle_date(raw_date: str, default_year: Optional[int] = None) -> Optional[datetime.datetime]:
    if not raw_date:
        return None
    s = raw_date.lower().strip()
    s = re.sub(r'^(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)[,\s]*', '', s)
    s = s.replace(" de ", " ").replace(",", " ")

    hour, minute = 23, 59
    time_match = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', s)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        s = re.sub(r'\d{1,2}:\d{2}(?::\d{2})?', '', s).strip()

    year = default_year or datetime.datetime.now().year
    year_match = re.search(r'\b(202\d)\b', s)
    if year_match:
        year = int(year_match.group(1))
        s = re.sub(r'\b202\d\b', '', s).strip()

    month = None
    for m_name, m_val in SPANISH_MONTHS.items():
        if re.search(r'\b' + m_name + r'\b', s):
            month = m_val
            s = re.sub(r'\b' + m_name + r'\b', '', s).strip()
            break

    day_match = re.search(r'\b(\d{1,2})\b', s)
    day = int(day_match.group(1)) if day_match else None

    if month and day:
        try:
            return datetime.datetime(year, month, day, hour, minute)
        except Exception:
            return None
    return None

class MoodleScraper:
    def __init__(self, moodle_url: Optional[str] = None):
        self.moodle_url = (moodle_url or get_moodle_url()).rstrip("/")
        self.auth_state_path = AUTH_STATE_FILE

    def launch_login_browser_process(self) -> Dict[str, Any]:
        """
        Launches an independent browser window in the background for the user to log in.
        Returns immediately so the Web UI doesn't hang or timeout.
        """
        if not self.moodle_url:
            return {"success": False, "message": "Moodle URL is not configured. Please set your school Moodle URL in Settings."}

        try:
            cmd = [sys.executable, "-m", "backend.moodle_scraper", "--login"]
            subprocess.Popen(cmd, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            return {
                "success": True, 
                "message": "Browser window launched! Log into your campus. Once you see your courses/dashboard, close the browser window and click 'Run Auto Sync Now'."
            }
        except Exception as e:
            return {"success": False, "message": f"Failed to launch browser: {str(e)}"}

    def open_login_window_direct(self):
        """
        Synchronous runner for the interactive login window.
        Saves all authentication cookies (including transient session cookies) to moodle_auth.json.
        """
        from playwright.sync_api import sync_playwright

        if not self.moodle_url:
            print("Moodle URL is not set.")
            return

        print(f"Launching interactive browser for: {self.moodle_url}")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage"
                ]
            )

            # Check if previous auth state exists
            storage_args = {}
            if os.path.exists(self.auth_state_path):
                storage_args["storage_state"] = self.auth_state_path

            context = browser.new_context(**storage_args)
            page = context.new_page()
            page.goto(self.moodle_url, wait_until="domcontentloaded")
            print("Browser is open. Please log into your Moodle campus.")

            # Continuously track and persist auth state while user is logged in
            while not page.is_closed():
                try:
                    context.storage_state(path=self.auth_state_path)
                    page.wait_for_timeout(1000)
                except Exception:
                    break

            try:
                context.storage_state(path=self.auth_state_path)
                browser.close()
            except Exception:
                pass
            print(f"Authentication session saved successfully to {self.auth_state_path}.")

    def _scrape_moodle_sync(self, headless: bool = True) -> Dict[str, Any]:
        """
        Synchronous scraping function running in worker thread.
        Scrapes courses, activities, course topics/announcements, and calendar events.
        """
        from playwright.sync_api import sync_playwright

        if not self.moodle_url:
            return {"success": False, "error": "Moodle URL is not configured in Settings."}

        courses_found = []
        assignments_found = []
        seen_course_ids = set()

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage"
                    ]
                )

                storage_args = {}
                if os.path.exists(self.auth_state_path):
                    storage_args["storage_state"] = self.auth_state_path

                context = browser.new_context(**storage_args)
                page = context.new_page()

                # 1. Verify authentication on Dashboard
                dashboard_url = f"{self.moodle_url}/my/"
                page.goto(dashboard_url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1500)

                curr_url = page.url.lower()
                curr_title = page.title().lower()
                if "login/index.php" in curr_url or "ingresar" in curr_title:
                    browser.close()
                    return {
                        "success": False,
                        "error": "Session expired or not logged in yet. Please click 'Launch Browser & Log In', sign in, and then click 'Run Auto Sync Now'."
                    }

                # 2. Extract Complete Course List & Grades from Grade Overview Report
                try:
                    grade_url = f"{self.moodle_url}/grade/report/overview/index.php"
                    page.goto(grade_url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(1500)

                    rows = page.query_selector_all("table#overview-grade tr, .generaltable tr")
                    for r in rows:
                        try:
                            link_el = r.query_selector("a[href*='id=']")
                            if not link_el:
                                continue
                            href = link_el.get_attribute("href") or ""
                            cname = link_el.inner_text().strip()

                            # Grade column text
                            cells = r.query_selector_all("td")
                            grade_val = cells[-1].inner_text().strip() if len(cells) >= 2 else None
                            if grade_val in ["-", "", " "]:
                                grade_val = None

                            match = re.search(r'id=(\d+)', href)
                            if match and cname and len(cname) > 3:
                                cid = match.group(1)
                                if cid not in seen_course_ids:
                                    seen_course_ids.add(cid)
                                    # Parse code if present in brackets or name
                                    code_match = re.search(r'\((.*?)\)', cname)
                                    code = code_match.group(1) if code_match else "CURSO"
                                    # Proper course homepage URL:
                                    course_home_url = f"{self.moodle_url}/course/view.php?id={cid}"
                                    courses_found.append({
                                        "moodle_id": cid,
                                        "name": cname,
                                        "moodle_url": course_home_url,
                                        "code": code,
                                        "grade": grade_val,
                                        "activities": [],
                                        "raw_announcements": ""
                                    })
                        except Exception:
                            continue
                except Exception as e:
                    print("Grade overview extraction notice:", e)

                # 3. If courses were not found in grade overview, fallback to Dashboard course cards
                if not courses_found:
                    try:
                        page.goto(dashboard_url, wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(1500)

                        course_selectors = [
                            'a[href*="/course/view.php?id="]',
                            '.coursebox h3 a',
                            '.coursebox .coursename a',
                            '.dashboard-card a',
                            '.coursename a'
                        ]
                        
                        for sel in course_selectors:
                            elements = page.query_selector_all(sel)
                            for elem in elements:
                                href = elem.get_attribute("href") or ""
                                text = elem.inner_text().strip()
                                match = re.search(r'id=(\d+)', href)
                                if match:
                                    cid = match.group(1)
                                    if cid not in seen_course_ids and len(text) > 3 and not text.lower().startswith("view"):
                                        seen_course_ids.add(cid)
                                        courses_found.append({
                                            "moodle_id": cid,
                                            "name": text,
                                            "moodle_url": f"{self.moodle_url}/course/view.php?id={cid}",
                                            "code": "CURSO",
                                            "grade": None,
                                            "activities": [],
                                            "raw_announcements": ""
                                        })
                    except Exception:
                        pass

                # 4. Deep Course Inspection: Scrape Course Activities, Topic Texts & Teacher Announcements
                for course in courses_found:
                    try:
                        page.goto(course["moodle_url"], wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(1000)

                        # Check header title
                        header = page.query_selector("h1, .page-header-headings, .page-title")
                        if header:
                            clean_name = header.inner_text().strip()
                            if clean_name and "imagen" not in clean_name.lower():
                                course["name"] = clean_name

                        # 4a. Extract all course activity modules (Assignments, Quizzes, Feedback)
                        modules = page.query_selector_all(".activity, .activityinstance, li.activity")
                        for m in modules:
                            link_el = m.query_selector("a[href*='/mod/']")
                            if not link_el:
                                continue
                            act_href = link_el.get_attribute("href") or ""
                            act_name = link_el.inner_text().strip().split("\n")[0].strip()
                            clean_act_name = re.sub(r'\s+(Tarea|Examen|Foro|Página|Archivo|Cuestionario|Retroalimentación)$', '', act_name, flags=re.IGNORECASE).strip()

                            m_id_match = re.search(r'id=(\d+)', act_href)
                            mod_id = m_id_match.group(1) if m_id_match else None

                            desc_el = m.query_selector(".contentafterlink, .summary, .activity-description")
                            desc = desc_el.inner_text().strip() if desc_el else ""

                            dates_el = m.query_selector(".activity-dates, .text-muted, [data-region='activity-dates']")
                            dates_txt = dates_el.inner_text().strip() if dates_el else ""
                            parsed_act_date = parse_moodle_date(dates_txt)
                            act_due_iso = parsed_act_date.isoformat() if parsed_act_date else None

                            if mod_id and any(k in act_href for k in ["/mod/assign/", "/mod/quiz/", "/mod/feedback/", "/mod/h5pactivity/"]):
                                course["activities"].append({
                                    "title": clean_act_name,
                                    "url": act_href,
                                    "mod_id": mod_id,
                                    "moodle_id": f"{mod_id}@licenciatura.ucnl.edu.mx",
                                    "description": desc,
                                    "due_date_raw": dates_txt,
                                    "due_date_iso": act_due_iso
                                })

                        # 4b. Collect topic summaries, labels, and course announcements
                        extracted_text_blocks = []

                        # Topic content & section texts
                        content_nodes = page.query_selector_all(".section .content, .summary, .section-summary, .activityinstance, .contentafterlink")
                        for node in content_nodes[:15]:
                            txt = node.inner_text().strip()
                            if len(txt) > 20 and ("fecha" in txt.lower() or "entrega" in txt.lower() or "actividad" in txt.lower() or "tarea" in txt.lower() or "cierre" in txt.lower() or "cuestionario" in txt.lower()):
                                extracted_text_blocks.append(txt)

                        # Find Forum / Avisos / Novedades / Anuncios
                        forum_links = page.query_selector_all('a[href*="/mod/forum/view.php"]')
                        for f_link in forum_links[:3]:
                            f_href = f_link.get_attribute("href")
                            if f_href:
                                try:
                                    fp = context.new_page()
                                    fp.goto(f_href, wait_until="domcontentloaded", timeout=12000)
                                    fp.wait_for_timeout(800)
                                    posts = fp.query_selector_all(".discussion, .forumpost, .discussion-list tbody tr, tr.discussion")
                                    for p in posts[:6]:
                                        ptxt = p.inner_text().strip()
                                        if len(ptxt) > 15:
                                            extracted_text_blocks.append(ptxt)
                                    fp.close()
                                except Exception:
                                    pass

                        if extracted_text_blocks:
                            course["raw_announcements"] = "\n\n---\n\n".join(extracted_text_blocks)
                    except Exception:
                        continue

                # 5. Extract Upcoming & Month Calendar Events
                seen_cal_keys = set()
                try:
                    cal_url = f"{self.moodle_url}/calendar/view.php?view=upcoming"
                    page.goto(cal_url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(1000)

                    event_cards = page.query_selector_all('.card.rounded, [data-event-id], .calendar-event')
                    for card in event_cards:
                        try:
                            header_el = card.query_selector(".card-header h3.name, .event-name, h3.name")
                            raw_header = header_el.inner_text().strip() if header_el else ""

                            course_el = card.query_selector("a[href*='/course/view.php?id=']")
                            c_name = course_el.inner_text().strip() if course_el else ""
                            c_href = course_el.get_attribute("href") if course_el else ""
                            cid_match = re.search(r'id=(\d+)', c_href)
                            course_moodle_id = cid_match.group(1) if cid_match else None

                            act_el = card.query_selector("a[href*='/mod/assign/'], a[href*='/mod/quiz/'], a.card-link, a.autolink")
                            act_href = act_el.get_attribute("href") if act_el else ""
                            mod_id_match = re.search(r'id=(\d+)', act_href)
                            mod_id = mod_id_match.group(1) if mod_id_match else None

                            # Clean title & check subtitle
                            clean_title = raw_header
                            for phrase in [" está en fecha de entrega", " is due", " cierra", " closes", " opens", " abre", " pendiente"]:
                                clean_title = clean_title.replace(phrase, "")
                            clean_title = clean_title.strip()

                            h4_el = card.query_selector(".description-content h4")
                            sub_title = h4_el.inner_text().strip() if h4_el else ""
                            full_title = sub_title if sub_title and len(sub_title) > len(clean_title) else clean_title

                            # Date
                            date_text = ""
                            clock_icon = card.query_selector("i.fa-clock-o, i.icon.fa-clock-o")
                            if clock_icon:
                                parent_row = clock_icon.evaluate_handle("el => el.closest('.row')")
                                if parent_row:
                                    date_text = parent_row.as_element().inner_text().strip()
                                    date_text = re.sub(r'^(Cuando\s*|\n)', '', date_text).strip()

                            if not date_text:
                                date_el = card.query_selector(".date, .text-muted, [data-region='event-time']")
                                date_text = date_el.inner_text().strip() if date_el else ""

                            desc_el = card.query_selector(".description-content, .card-body")
                            desc_text = desc_el.inner_text().strip() if desc_el else ""

                            parsed_dt = parse_moodle_date(date_text)
                            due_iso = parsed_dt.isoformat() if parsed_dt else None
                            moodle_uid = f"{mod_id}@licenciatura.ucnl.edu.mx" if mod_id else None

                            unique_key = moodle_uid or f"{course_moodle_id}_{full_title}"
                            if full_title and unique_key not in seen_cal_keys:
                                seen_cal_keys.add(unique_key)
                                assignments_found.append({
                                    "title": full_title,
                                    "course_name": c_name,
                                    "course_moodle_id": course_moodle_id,
                                    "moodle_url": act_href,
                                    "moodle_id": moodle_uid,
                                    "due_date_raw": date_text,
                                    "due_date_iso": due_iso,
                                    "description": desc_text or f"Moodle Calendar: {date_text}",
                                    "source": "calendar"
                                })
                        except Exception:
                            continue
                except Exception:
                    pass

                browser.close()

                return {
                    "success": True,
                    "courses": courses_found,
                    "assignments": assignments_found,
                    "count_courses": len(courses_found),
                    "count_assignments": len(assignments_found)
                }

        except Exception as e:
            return {"success": False, "error": f"Scraping error: {str(e)}"}

    def _scrape_course_activities_sync(self, course_url: str, headless: bool = True) -> List[Dict[str, Any]]:
        """
        Visits a specific course page and extracts all modules, assignments, quizzes, and descriptions.
        """
        from playwright.sync_api import sync_playwright

        activities = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
                )

                storage_args = {}
                if os.path.exists(self.auth_state_path):
                    storage_args["storage_state"] = self.auth_state_path

                context = browser.new_context(**storage_args)
                page = context.new_page()
                page.goto(course_url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1000)

                modules = page.query_selector_all(".activity, .activityinstance, li.activity")
                for m in modules:
                    link_el = m.query_selector("a[href*='/mod/']")
                    if not link_el:
                        continue
                    act_href = link_el.get_attribute("href") or ""
                    act_name = link_el.inner_text().strip().split("\n")[0].strip()
                    clean_name = re.sub(r'\s+(Tarea|Examen|Foro|Página|Archivo|Cuestionario|Retroalimentación)$', '', act_name, flags=re.IGNORECASE).strip()

                    m_id_match = re.search(r'id=(\d+)', act_href)
                    mod_id = m_id_match.group(1) if m_id_match else None

                    desc_el = m.query_selector(".contentafterlink, .summary, .activity-description")
                    desc = desc_el.inner_text().strip() if desc_el else ""

                    dates_el = m.query_selector(".activity-dates, .text-muted, [data-region='activity-dates']")
                    dates_txt = dates_el.inner_text().strip() if dates_el else ""
                    parsed_act_date = parse_moodle_date(dates_txt)
                    act_due_iso = parsed_act_date.isoformat() if parsed_act_date else None

                    if clean_name and mod_id and any(k in act_href for k in ["/mod/assign/", "/mod/quiz/", "/mod/feedback/", "/mod/h5pactivity/"]):
                        activities.append({
                            "title": clean_name,
                            "url": act_href,
                            "mod_id": mod_id,
                            "moodle_id": f"{mod_id}@licenciatura.ucnl.edu.mx",
                            "description": desc,
                            "due_date_raw": dates_txt,
                            "due_date_iso": act_due_iso
                        })

                browser.close()
        except Exception as e:
            print(f"Error scraping course activities from {course_url}: {e}")
        return activities

    async def scrape_course_activities(self, course_url: str, headless: bool = True) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._scrape_course_activities_sync, course_url, headless)

    async def scrape_moodle(self, headless: bool = True) -> Dict[str, Any]:
        """
        Async interface delegating to thread-safe synchronous scraper.
        """
        return await asyncio.to_thread(self._scrape_moodle_sync, headless)

    def sync_from_ical_url(self, ical_url: Optional[str] = None) -> Dict[str, Any]:
        """
        Synchronizes assignments and events directly from Moodle's iCal calendar export feed URL.
        100% reliable, fast, zero browser scraping required.
        """
        url = ical_url or get_moodle_ical_url()
        if not url:
            return {"success": False, "error": "No iCal URL provided."}

        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()

            cal = Calendar.from_ical(response.text)
            events = []

            for component in cal.walk():
                if component.name == "VEVENT":
                    summary = str(component.get("summary", "Moodle Event"))
                    description = str(component.get("description", ""))
                    dtstart = component.get("dtstart")
                    dtend = component.get("dtend")
                    uid = str(component.get("uid", ""))
                    categories = str(component.get("categories", ""))

                    # Clean boilerplate phrases from Moodle summaries
                    clean_title = summary
                    for phrase in [" está en fecha de entrega", " is due", " cierra", " closes", " opens", " abre"]:
                        clean_title = clean_title.replace(phrase, "")
                    clean_title = clean_title.strip()

                    # Clean description
                    clean_desc = re.sub(r'\[\d+\]', '', description).strip()

                    due_dt = None
                    if dtend:
                        due_dt = dtend.dt
                    elif dtstart:
                        due_dt = dtstart.dt

                    due_iso = due_dt.isoformat() if hasattr(due_dt, "isoformat") else str(due_dt)

                    events.append({
                        "title": clean_title,
                        "description": clean_desc or f"Moodle calendar deliverable: {summary}",
                        "due_date": due_iso,
                        "moodle_id": uid,
                        "course_name": categories or "Moodle Course",
                        "source": "calendar"
                    })

            return {
                "success": True,
                "events": events,
                "count": len(events)
            }
        except Exception as e:
            return {"success": False, "error": f"iCal Sync Error: {str(e)}"}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--login":
        scraper = MoodleScraper()
        scraper.open_login_window_direct()
