from playwright.sync_api import sync_playwright
from backend.config import DATA_DIR

auth_file = str(DATA_DIR / "moodle_auth.json")
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(storage_state=auth_file)
    page = context.new_page()
    page.goto("https://licenciatura.ucnl.edu.mx/course/view.php?id=18919", wait_until="domcontentloaded", timeout=20000)
    page.wait_for_timeout(1500)

    # Search for all activity elements that are assignments/quizzes/forums
    modules = page.query_selector_all(".activity")
    print(f"Total .activity modules: {len(modules)}")
    for m in modules:
        classes = m.get_attribute("class") or ""
        name_el = m.query_selector(".instancename, .activityname, a")
        name = name_el.inner_text().strip() if name_el else ""
        link_el = m.query_selector("a")
        link = link_el.get_attribute("href") if link_el else ""

        # Content/description under the activity
        desc_el = m.query_selector(".contentafterlink, .summary, .activity-description")
        desc = desc_el.inner_text().strip() if desc_el else ""

        if name:
            clean_name = name.replace("Tarea", "").replace("Examen", "").replace("Foro", "").replace("Página", "").strip()
            print(f"[{classes.split()[1] if len(classes.split())>1 else ''}] {clean_name} | URL: {link}")
            if desc:
                print(f"   Desc: {desc[:100]}")
    browser.close()
