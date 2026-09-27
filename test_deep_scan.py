import asyncio
import re
from playwright.sync_api import sync_playwright
from backend.config import DATA_DIR
from backend.database import AsyncSessionLocal, Course, Assignment
from sqlalchemy import select

auth_file = str(DATA_DIR / "moodle_auth.json")

def scrape_all_courses_deep():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=auth_file)
        page = context.new_page()

        courses_to_scan = [
            {"name": "Cálculo Integral", "url": "https://licenciatura.ucnl.edu.mx/course/view.php?id=18919"},
            {"name": "Bases de Datos Relacionales", "url": "https://licenciatura.ucnl.edu.mx/course/view.php?id=18925"},
            {"name": "Circuitos Análogos y Digitales", "url": "https://licenciatura.ucnl.edu.mx/course/view.php?id=18927"},
            {"name": "Inglés Enfocado a Sistemas", "url": "https://licenciatura.ucnl.edu.mx/course/view.php?id=18928"},
            {"name": "Probabilidad y Estadística", "url": "https://licenciatura.ucnl.edu.mx/course/view.php?id=18921"},
            {"name": "Programación Orientada a Objetos", "url": "https://licenciatura.ucnl.edu.mx/course/view.php?id=18923"}
        ]

        for c in courses_to_scan:
            print(f"Scanning: {c['name']} ({c['url']})...")
            try:
                page.goto(c["url"], wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1000)

                modules = page.query_selector_all(".activity, .activityinstance")
                c_activities = []
                for m in modules:
                    link_el = m.query_selector("a[href*='/mod/']")
                    if not link_el:
                        continue
                    link = link_el.get_attribute("href") or ""
                    name = link_el.inner_text().strip()

                    # Extract mod ID (e.g. id=774780)
                    m_id_match = re.search(r'id=(\d+)', link)
                    mod_id = m_id_match.group(1) if m_id_match else None

                    # Check description
                    desc_el = m.query_selector(".contentafterlink, .summary, .activity-description")
                    desc = desc_el.inner_text().strip() if desc_el else ""

                    if name and mod_id:
                        c_activities.append({
                            "title": name,
                            "url": link,
                            "mod_id": mod_id,
                            "description": desc
                        })

                results[c["name"]] = c_activities
                print(f" -> Found {len(c_activities)} activities in {c['name']}")
            except Exception as e:
                print(f"Error scanning {c['name']}: {e}")

        browser.close()
    return results

async def main():
    scraped_data = await asyncio.to_thread(scrape_all_courses_deep)

    async with AsyncSessionLocal() as session:
        courses = (await session.execute(select(Course))).scalars().all()
        assignments = (await session.execute(select(Assignment))).scalars().all()

        matched_count = 0
        created_count = 0

        for c in courses:
            c_key = next((k for k in scraped_data.keys() if k.lower() in c.name.lower()), None)
            if not c_key:
                continue

            course_mods = scraped_data[c_key]
            print(f"\nMatching for Course: {c.name} (DB ID: {c.id})")

            for mod in course_mods:
                mod_id = mod["mod_id"]
                mod_title = mod["title"]
                full_uid = f"{mod_id}@licenciatura.ucnl.edu.mx"

                # Check if assignment with moodle_id or title exists
                matching_asgn = next((
                    a for a in assignments 
                    if (a.moodle_id and mod_id in str(a.moodle_id)) or 
                       (mod_title.lower() == a.title.lower()) or
                       (mod_title.lower() in a.title.lower() and len(mod_title) > 4) or
                       (a.title.lower() in mod_title.lower() and len(a.title) > 4)
                ), None)

                if matching_asgn:
                    matching_asgn.course_id = c.id
                    matching_asgn.moodle_url = mod["url"]
                    if not matching_asgn.description and mod["description"]:
                        matching_asgn.description = mod["description"]
                    matched_count += 1
                    print(f"  [MATCHED] '{matching_asgn.title}' -> Course: {c.name}")
                else:
                    # Check if already in DB by moodle_id
                    existing_db = await session.execute(select(Assignment).where(Assignment.moodle_id == full_uid))
                    if not existing_db.scalar_one_or_none():
                        if any(t in mod["url"] for t in ["/mod/assign/", "/mod/quiz/"]):
                            session.add(Assignment(
                                course_id=c.id,
                                title=mod["title"],
                                description=mod["description"] or f"Activity from course: {c.name}",
                                moodle_url=mod["url"],
                                moodle_id=full_uid,
                                source="course_page"
                            ))
                            created_count += 1
                            print(f"  [NEW ACTIVITY] '{mod['title']}' -> Course: {c.name}")

        await session.commit()
        print(f"\n=== FINISHED DEEP MATCHING ===")
        print(f"Matched {matched_count} existing calendar events to courses.")
        print(f"Created {created_count} new course page activities.")

if __name__ == "__main__":
    asyncio.run(main())
