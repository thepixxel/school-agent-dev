import os
import re
import json
import datetime
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, desc, update
from pydantic import BaseModel

from backend.config import (
    BASE_DIR, get_gemini_api_key, get_gemini_model, get_moodle_url, get_moodle_ical_url,
    update_env_variable
)
from backend.database import (
    init_db, get_db, Term, Course, Assignment, Note, StudyItem, ChatMessage
)
from backend.ai_service import AcademicAssistantService
from backend.moodle_scraper import MoodleScraper
from backend.google_export import GoogleExportService

app = FastAPI(title="School Agent - AI Personal Class Assistant")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup event
@app.on_event("startup")
async def on_startup():
    await init_db()

# --- Pydantic Request Models ---
class ConfigUpdate(BaseModel):
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None
    moodle_url: Optional[str] = None
    moodle_ical_url: Optional[str] = None

class TermCreate(BaseModel):
    name: str
    is_current: Optional[bool] = False

class CourseCreate(BaseModel):
    name: str
    code: Optional[str] = ""
    instructor: Optional[str] = ""
    color: Optional[str] = "#3b82f6"
    grade: Optional[str] = None
    term_id: Optional[int] = None
    status: Optional[str] = "completed"

class CourseUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    instructor: Optional[str] = None
    color: Optional[str] = None
    grade: Optional[str] = None
    term_id: Optional[int] = None
    status: Optional[str] = None

class AssignmentCreate(BaseModel):
    course_id: Optional[int] = None
    title: str
    description: Optional[str] = ""
    due_date: Optional[str] = None
    due_date_raw: Optional[str] = None
    priority: Optional[str] = "medium"
    source: Optional[str] = "manual"

class ExtractDeadlinesRequest(BaseModel):
    text: str
    course_id: Optional[int] = None

class AssignmentUpdate(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = None
    due_date_raw: Optional[str] = None

class BulkUpdateAssignmentsRequest(BaseModel):
    assignment_ids: List[int]
    status: Optional[str] = None
    priority: Optional[str] = None

class BulkDeleteAssignmentsRequest(BaseModel):
    assignment_ids: List[int]

class NoteCreate(BaseModel):
    course_id: Optional[int] = None
    title: str
    content: str
    tags: Optional[str] = "lecture"

class NoteSummarizeRequest(BaseModel):
    content: str
    course_id: Optional[int] = None
    generate_flashcards: Optional[bool] = True

class FlashcardGenerateRequest(BaseModel):
    content: str
    note_id: Optional[int] = None
    course_id: Optional[int] = None
    num_cards: Optional[int] = 6

class ChatRequest(BaseModel):
    message: str

class EmailDraftRequest(BaseModel):
    professor_name: str
    course_name: str
    purpose: str
    details: str
    tone: Optional[str] = "polite and professional"


# --- Context Helper for AI ---
async def get_academic_context(db: AsyncSession):
    # Current active term
    current_term_res = await db.execute(select(Term).where(Term.is_current == True))
    current_term = current_term_res.scalar_one_or_none()
    current_term_id = current_term.id if current_term else None

    # Courses
    courses_res = await db.execute(select(Course).order_by(Course.status.asc(), Course.name.asc()))
    courses = courses_res.scalars().all()

    active_courses = [c for c in courses if c.status == "active" or (current_term_id and c.term_id == current_term_id)]
    completed_courses = [c for c in courses if c not in active_courses]

    # Active term assignments prioritized first
    active_course_ids = [c.id for c in active_courses]

    active_asgns = []
    if active_course_ids:
        active_asgns_res = await db.execute(
            select(Assignment, Course)
            .outerjoin(Course, Assignment.course_id == Course.id)
            .where(Assignment.course_id.in_(active_course_ids))
            .order_by(
                Assignment.due_date.asc().nulls_last(),
                Assignment.id.asc()
            )
        )
        active_asgns = active_asgns_res.all()

    other_asgns = []
    if len(active_asgns) < 35:
        other_query = select(Assignment, Course).outerjoin(Course, Assignment.course_id == Course.id)
        if active_course_ids:
            other_query = other_query.where(~Assignment.course_id.in_(active_course_ids))
        other_query = other_query.order_by(Assignment.due_date.asc().nulls_last(), desc(Assignment.id)).limit(35 - len(active_asgns))
        other_res = await db.execute(other_query)
        other_asgns = other_res.all()

    assignments_rows = active_asgns + other_asgns

    notes_res = await db.execute(
        select(Note).order_by(desc(Note.created_at)).limit(5)
    )
    notes = notes_res.scalars().all()

    return {
        "current_term": {"id": current_term.id, "name": current_term.name} if current_term else None,
        "active_courses": [{"id": c.id, "name": c.name, "code": c.code, "grade": c.grade} for c in active_courses],
        "completed_courses": [{"id": c.id, "name": c.name, "code": c.code, "grade": c.grade} for c in completed_courses],
        "courses": [{"id": c.id, "name": c.name, "code": c.code, "grade": c.grade, "status": c.status} for c in courses],
        "assignments": [
            {
                "id": a.id,
                "title": a.title,
                "due_date": a.due_date.strftime("%Y-%m-%d %H:%M") if a.due_date else None,
                "due_date_raw": a.due_date_raw,
                "status": a.status,
                "priority": a.priority,
                "description": a.description or "",
                "course_name": c.name if c else "General"
            }
            for a, c in assignments_rows
        ],
        "notes": [{"id": n.id, "title": n.title} for n in notes]
    }


# ==================== API ENDPOINTS ====================

# 1. Config
@app.get("/api/config")
async def get_config():
    api_key = get_gemini_api_key()
    return {
        "gemini_api_key_configured": bool(api_key),
        "gemini_model": get_gemini_model(),
        "moodle_url": get_moodle_url(),
        "moodle_ical_url": get_moodle_ical_url(),
    }

@app.post("/api/config")
async def update_config(payload: ConfigUpdate):
    if payload.gemini_api_key is not None:
        key = payload.gemini_api_key.strip()
        if key and (" " in key and "?" in key):
            raise HTTPException(
                status_code=400,
                detail="Invalid Gemini API key format. Please provide your API key."
            )
        update_env_variable("GEMINI_API_KEY", key)
    if payload.gemini_model is not None:
        update_env_variable("GEMINI_MODEL", payload.gemini_model.strip())
    if payload.moodle_url is not None:
        update_env_variable("MOODLE_URL", payload.moodle_url.strip())
    if payload.moodle_ical_url is not None:
        update_env_variable("MOODLE_ICAL_URL", payload.moodle_ical_url.strip())
    return {"success": True, "message": "Settings updated successfully"}


# 2. Terms & Academic Periods
@app.get("/api/terms")
async def list_terms(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Term).order_by(desc(Term.id)))
    return res.scalars().all()

@app.post("/api/terms")
async def create_term(payload: TermCreate, db: AsyncSession = Depends(get_db)):
    if payload.is_current:
        await db.execute(update(Term).values(is_current=False))

    term = Term(
        name=payload.name,
        is_current=payload.is_current or False
    )
    db.add(term)
    await db.commit()
    await db.refresh(term)
    return term

@app.put("/api/terms/{term_id}/set-current")
async def set_current_term(term_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(update(Term).values(is_current=False))
    await db.execute(update(Term).where(Term.id == term_id).values(is_current=True))
    await db.commit()
    return {"success": True}

@app.delete("/api/terms/{term_id}")
async def delete_term(term_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Term).where(Term.id == term_id))
    await db.commit()
    return {"success": True}


# 3. Grades & Academic Overview
def parse_grade(grade_str: Optional[str]) -> Optional[float]:
    if not grade_str:
        return None
    try:
        # Match float numbers in grade string (e.g. "10.00", "9.90", "9.78")
        clean = re.findall(r"[-+]?(?:\d*\.\d+|\d+)", grade_str)
        if clean:
            return float(clean[0])
    except Exception:
        pass
    return None

@app.get("/api/grades/overview")
async def get_grades_overview(db: AsyncSession = Depends(get_db)):
    terms_res = await db.execute(select(Term).order_by(desc(Term.id)))
    terms = terms_res.scalars().all()

    courses_res = await db.execute(select(Course).order_by(Course.term_id.desc(), Course.name.asc()))
    courses = courses_res.scalars().all()

    # Map courses by term
    term_courses_map = {}
    unassigned_courses = []

    all_grades = []

    for c in courses:
        g_val = parse_grade(c.grade)
        if g_val is not None:
            all_grades.append(g_val)

        c_dict = {
            "id": c.id,
            "name": c.name,
            "code": c.code,
            "grade": c.grade,
            "grade_num": g_val,
            "status": c.status,
            "moodle_url": c.moodle_url,
            "color": c.color,
            "term_id": c.term_id
        }

        if c.term_id:
            if c.term_id not in term_courses_map:
                term_courses_map[c.term_id] = []
            term_courses_map[c.term_id].append(c_dict)
        else:
            unassigned_courses.append(c_dict)

    terms_data = []
    current_term_data = None
    historical_terms_data = []

    for t in terms:
        t_courses = term_courses_map.get(t.id, [])
        t_grades = [c["grade_num"] for c in t_courses if c["grade_num"] is not None]
        avg_gpa = round(sum(t_grades) / len(t_grades), 2) if t_grades else None

        t_data = {
            "id": t.id,
            "name": t.name,
            "is_current": t.is_current,
            "courses": t_courses,
            "course_count": len(t_courses),
            "term_gpa": avg_gpa
        }
        terms_data.append(t_data)

        if t.is_current:
            current_term_data = t_data
        else:
            historical_terms_data.append(t_data)

    cumulative_gpa = round(sum(all_grades) / len(all_grades), 2) if all_grades else None

    return {
        "cumulative_gpa": cumulative_gpa,
        "total_courses_completed": len([c for c in courses if c.grade is not None]),
        "total_courses_count": len(courses),
        "current_term": current_term_data,
        "historical_terms": historical_terms_data,
        "all_terms": terms_data,
        "unassigned_courses": unassigned_courses
    }


# 4. Courses CRUD
@app.get("/api/courses")
async def list_courses(term_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    query = select(Course).order_by(Course.name)
    if term_id:
        query = query.where(Course.term_id == term_id)
    res = await db.execute(query)
    courses = res.scalars().all()
    return courses

@app.post("/api/courses")
async def create_course(payload: CourseCreate, db: AsyncSession = Depends(get_db)):
    course = Course(
        name=payload.name,
        code=payload.code,
        instructor=payload.instructor,
        color=payload.color or "#3b82f6",
        grade=payload.grade,
        term_id=payload.term_id,
        status=payload.status or "completed"
    )
    db.add(course)
    await db.commit()
    await db.refresh(course)
    return course

@app.put("/api/courses/{course_id}")
async def update_course(course_id: int, payload: CourseUpdate, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Course).where(Course.id == course_id))
    course = res.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    if payload.name is not None:
        course.name = payload.name
    if payload.code is not None:
        course.code = payload.code
    if payload.instructor is not None:
        course.instructor = payload.instructor
    if payload.color is not None:
        course.color = payload.color
    if payload.grade is not None:
        course.grade = payload.grade
    if payload.term_id is not None:
        course.term_id = payload.term_id
    if payload.status is not None:
        course.status = payload.status

    await db.commit()
    return {"success": True, "course": course}

@app.delete("/api/courses/{course_id}")
async def delete_course(course_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Course).where(Course.id == course_id))
    await db.commit()
    return {"success": True}

@app.post("/api/courses/{course_id}/deep-scan")
async def deep_scan_single_course(course_id: int, db: AsyncSession = Depends(get_db)):
    c_res = await db.execute(select(Course).where(Course.id == course_id))
    course = c_res.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    if not course.moodle_url:
        raise HTTPException(status_code=400, detail="Course does not have a Moodle URL configured")

    scraper = MoodleScraper()
    activities = await scraper.scrape_course_activities(course.moodle_url, headless=True)

    assignments_res = await db.execute(select(Assignment))
    assignments = assignments_res.scalars().all()

    matched_count = 0
    created_count = 0

    for mod in activities:
        mod_id = mod["mod_id"]
        mod_title = mod["title"]
        full_uid = f"{mod_id}@licenciatura.ucnl.edu.mx"

        matching_asgn = next((
            a for a in assignments 
            if (a.moodle_id and mod_id in str(a.moodle_id)) or 
               (mod_title.lower() == a.title.lower()) or
               (mod_title.lower() in a.title.lower() and len(mod_title) > 4) or
               (a.title.lower() in mod_title.lower() and len(a.title) > 4)
        ), None)

        if matching_asgn:
            matching_asgn.course_id = course.id
            matching_asgn.moodle_url = mod["url"]
            if not matching_asgn.description and mod["description"]:
                matching_asgn.description = mod["description"]
            matched_count += 1
        else:
            existing_db = await db.execute(select(Assignment).where(Assignment.moodle_id == full_uid))
            if not existing_db.scalar_one_or_none():
                if any(t in mod["url"] for t in ["/mod/assign/", "/mod/quiz/"]):
                    db.add(Assignment(
                        course_id=course.id,
                        title=mod["title"],
                        description=mod["description"] or f"Activity from course: {course.name}",
                        moodle_url=mod["url"],
                        moodle_id=full_uid,
                        source="course_page"
                    ))
                    created_count += 1

    await db.commit()
    return {
        "success": True,
        "course_name": course.name,
        "matched_count": matched_count,
        "created_count": created_count,
        "total_activities_found": len(activities)
    }

@app.post("/api/courses/deep-scan-all")
async def deep_scan_all_courses_endpoint(db: AsyncSession = Depends(get_db)):
    courses_res = await db.execute(select(Course))
    courses = courses_res.scalars().all()

    total_matched = 0
    total_created = 0
    scraper = MoodleScraper()

    for course in courses:
        if not course.moodle_url:
            continue
        try:
            activities = await scraper.scrape_course_activities(course.moodle_url, headless=True)
            assignments_res = await db.execute(select(Assignment))
            assignments = assignments_res.scalars().all()

            for mod in activities:
                mod_id = mod["mod_id"]
                mod_title = mod["title"]
                full_uid = f"{mod_id}@licenciatura.ucnl.edu.mx"

                matching_asgn = next((
                    a for a in assignments 
                    if (a.moodle_id and mod_id in str(a.moodle_id)) or 
                       (mod_title.lower() == a.title.lower()) or
                       (mod_title.lower() in a.title.lower() and len(mod_title) > 4) or
                       (a.title.lower() in mod_title.lower() and len(a.title) > 4)
                ), None)

                if matching_asgn:
                    matching_asgn.course_id = course.id
                    matching_asgn.moodle_url = mod["url"]
                    if not matching_asgn.description and mod["description"]:
                        matching_asgn.description = mod["description"]
                    total_matched += 1
                else:
                    existing_db = await db.execute(select(Assignment).where(Assignment.moodle_id == full_uid))
                    if not existing_db.scalar_one_or_none():
                        if any(t in mod["url"] for t in ["/mod/assign/", "/mod/quiz/"]):
                            db.add(Assignment(
                                course_id=course.id,
                                title=mod["title"],
                                description=mod["description"] or f"Activity from course: {course.name}",
                                moodle_url=mod["url"],
                                moodle_id=full_uid,
                                source="course_page"
                            ))
                            total_created += 1
            await db.commit()
        except Exception as e:
            print(f"Error deep scanning course {course.name}: {e}")

    return {
        "success": True,
        "total_matched": total_matched,
        "total_created": total_created
    }

# 3. Assignments
@app.get("/api/assignments")
async def list_assignments(
    course_id: Optional[int] = None, 
    term_id: Optional[int] = None,
    status: Optional[str] = None, 
    db: AsyncSession = Depends(get_db)
):
    query = select(Assignment, Course).outerjoin(Course, Assignment.course_id == Course.id).order_by(Assignment.due_date.asc())
    if course_id:
        query = query.where(Assignment.course_id == course_id)
    if term_id:
        query = query.where(Course.term_id == term_id)
    if status:
        query = query.where(Assignment.status == status)

    result = await db.execute(query)
    items = []
    for assignment, course in result.all():
        items.append({
            "id": assignment.id,
            "title": assignment.title,
            "description": assignment.description,
            "due_date": assignment.due_date.isoformat() if assignment.due_date else None,
            "due_date_raw": assignment.due_date_raw,
            "status": assignment.status,
            "priority": assignment.priority,
            "source": assignment.source or "manual",
            "moodle_url": assignment.moodle_url,
            "course_id": assignment.course_id,
            "course_name": course.name if course else "General",
            "course_code": course.code if course else "",
            "course_color": course.color if course else "#3b82f6"
        })
    return items

@app.post("/api/assignments")
async def create_assignment(payload: AssignmentCreate, db: AsyncSession = Depends(get_db)):
    parsed_due = None
    if payload.due_date:
        try:
            parsed_due = datetime.datetime.fromisoformat(payload.due_date.replace("Z", "+00:00"))
        except Exception:
            pass

    assignment = Assignment(
        course_id=payload.course_id,
        title=payload.title,
        description=payload.description,
        due_date=parsed_due,
        due_date_raw=payload.due_date_raw,
        priority=payload.priority or "medium",
        source=payload.source or "manual"
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    return assignment

@app.post("/api/assignments/extract-from-text")
async def extract_assignments_from_text(payload: ExtractDeadlinesRequest, db: AsyncSession = Depends(get_db)):
    course_name = None
    if payload.course_id:
        c_res = await db.execute(select(Course).where(Course.id == payload.course_id))
        c_obj = c_res.scalar_one_or_none()
        if c_obj:
            course_name = c_obj.name

    ai_service = AcademicAssistantService()
    extracted_tasks = await ai_service.extract_deadlines_from_text(payload.text, course_name=course_name)

    created_assignments = []
    for t in extracted_tasks:
        parsed_due = None
        if t.get("due_date_iso"):
            try:
                parsed_due = datetime.datetime.fromisoformat(t["due_date_iso"].replace("Z", "+00:00"))
            except Exception:
                pass

        assignment = Assignment(
            course_id=payload.course_id,
            title=t.get("title", "Extracted Task"),
            description=t.get("description", ""),
            due_date=parsed_due,
            due_date_raw=t.get("due_date_raw"),
            priority=t.get("priority", "medium"),
            source="teacher_post"
        )
        db.add(assignment)
        created_assignments.append(assignment)

    await db.commit()
    return {
        "success": True,
        "count": len(created_assignments),
        "tasks": extracted_tasks
    }

@app.post("/api/assignments/{assignment_id}/ai-breakdown")
async def get_assignment_ai_breakdown(assignment_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Assignment, Course).outerjoin(Course, Assignment.course_id == Course.id).where(Assignment.id == assignment_id))
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    assignment, course = row

    ai_service = AcademicAssistantService()
    breakdown = await ai_service.breakdown_assignment(
        title=assignment.title,
        description=assignment.description or "",
        course_name=course.name if course else "General"
    )

    return {"success": True, "breakdown": breakdown}

@app.put("/api/assignments/{assignment_id}")
async def update_assignment(assignment_id: int, payload: AssignmentUpdate, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Assignment).where(Assignment.id == assignment_id))
    assignment = res.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    if payload.status is not None:
        assignment.status = payload.status
    if payload.priority is not None:
        assignment.priority = payload.priority
    if payload.title is not None:
        assignment.title = payload.title
    if payload.description is not None:
        assignment.description = payload.description
    if payload.due_date_raw is not None:
        assignment.due_date_raw = payload.due_date_raw
    if payload.due_date is not None:
        try:
            assignment.due_date = datetime.datetime.fromisoformat(payload.due_date.replace("Z", "+00:00"))
        except Exception:
            pass

    await db.commit()
    return {"success": True, "assignment": {
        "id": assignment.id,
        "title": assignment.title,
        "status": assignment.status,
        "priority": assignment.priority
    }}

@app.delete("/api/assignments/{assignment_id}")
async def delete_assignment(assignment_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Assignment).where(Assignment.id == assignment_id))
    await db.commit()
    return {"success": True}

@app.post("/api/assignments/bulk-update")
async def bulk_update_assignments(payload: BulkUpdateAssignmentsRequest, db: AsyncSession = Depends(get_db)):
    if not payload.assignment_ids:
        return {"success": True, "count": 0}

    query = select(Assignment).where(Assignment.id.in_(payload.assignment_ids))
    result = await db.execute(query)
    assignments = result.scalars().all()

    for a in assignments:
        if payload.status is not None:
            a.status = payload.status
        if payload.priority is not None:
            a.priority = payload.priority

    await db.commit()
    return {"success": True, "count": len(assignments)}

@app.post("/api/assignments/bulk-delete")
async def bulk_delete_assignments(payload: BulkDeleteAssignmentsRequest, db: AsyncSession = Depends(get_db)):
    if not payload.assignment_ids:
        return {"success": True, "count": 0}

    await db.execute(delete(Assignment).where(Assignment.id.in_(payload.assignment_ids)))
    await db.commit()
    return {"success": True, "count": len(payload.assignment_ids)}

@app.get("/api/assignments/export/calendar.ics")
async def export_calendar_ics(db: AsyncSession = Depends(get_db)):
    query = select(Assignment, Course).outerjoin(Course, Assignment.course_id == Course.id).where(Assignment.status != "completed")
    result = await db.execute(query)
    
    assignments = []
    for assignment, course in result.all():
        assignments.append({
            "id": assignment.id,
            "title": assignment.title,
            "description": assignment.description or "",
            "due_date": assignment.due_date,
            "course_code": course.code if course else "CLASS"
        })

    ics_content = GoogleExportService.generate_ics_calendar(assignments)
    return Response(
        content=ics_content,
        media_type="text/calendar",
        headers={"Content-Disposition": "attachment; filename=school_agent_deadlines.ics"}
    )

@app.get("/api/assignments/{assignment_id}/gcal-link")
async def get_gcal_link(assignment_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Assignment, Course).outerjoin(Course, Assignment.course_id == Course.id).where(Assignment.id == assignment_id))
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    assignment, course = row
    if not assignment.due_date:
        raise HTTPException(status_code=400, detail="Assignment has no due date")

    url = GoogleExportService.generate_google_calendar_url(
        title=f"[{course.code if course else 'CLASS'}] {assignment.title}",
        description=assignment.description or "Assignment deadline tracked by School Agent",
        due_date=assignment.due_date
    )
    return {"url": url}


# 4. Notes & Study Studio
@app.get("/api/notes")
async def list_notes(course_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    query = select(Note, Course).outerjoin(Course, Note.course_id == Course.id).order_by(desc(Note.updated_at))
    if course_id:
        query = query.where(Note.course_id == course_id)

    result = await db.execute(query)
    items = []
    for note, course in result.all():
        items.append({
            "id": note.id,
            "title": note.title,
            "content": note.content,
            "summary": note.summary,
            "tags": note.tags,
            "created_at": note.created_at.isoformat(),
            "updated_at": note.updated_at.isoformat() if note.updated_at else None,
            "course_id": note.course_id,
            "course_name": course.name if course else "General",
            "course_color": course.color if course else "#3b82f6"
        })
    return items

@app.get("/api/notes/{note_id}")
async def get_note(note_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Note, Course).outerjoin(Course, Note.course_id == Course.id).where(Note.id == note_id))
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    note, course = row

    items_res = await db.execute(select(StudyItem).where(StudyItem.note_id == note_id))
    study_items = items_res.scalars().all()

    return {
        "id": note.id,
        "title": note.title,
        "content": note.content,
        "summary": note.summary,
        "tags": note.tags,
        "course_id": note.course_id,
        "course_name": course.name if course else "General",
        "created_at": note.created_at.isoformat(),
        "study_items": [
            {
                "id": s.id,
                "item_type": s.item_type,
                "question": s.question,
                "answer": s.answer
            }
            for s in study_items
        ]
    }

@app.post("/api/notes")
async def create_note(payload: NoteCreate, db: AsyncSession = Depends(get_db)):
    note = Note(
        course_id=payload.course_id,
        title=payload.title,
        content=payload.content,
        tags=payload.tags
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note

@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Note).where(Note.id == note_id))
    await db.commit()
    return {"success": True}

@app.post("/api/notes/summarize-ai")
async def summarize_notes_ai(payload: NoteSummarizeRequest, db: AsyncSession = Depends(get_db)):
    course_name = "General"
    if payload.course_id:
        c_res = await db.execute(select(Course).where(Course.id == payload.course_id))
        course = c_res.scalar_one_or_none()
        if course:
            course_name = course.name

    ai_service = AcademicAssistantService()
    summary_data = await ai_service.summarize_lecture_or_notes(payload.content, course_name=course_name)
    
    flashcards = []
    if payload.generate_flashcards:
        flashcards = await ai_service.generate_flashcards(payload.content)

    return {
        "summary": summary_data.get("summary", ""),
        "key_concepts": summary_data.get("key_concepts", []),
        "action_items": summary_data.get("action_items", []),
        "tags": ", ".join(summary_data.get("suggested_tags", [])),
        "flashcards": flashcards
    }

@app.post("/api/notes/{note_id}/generate-flashcards")
async def generate_note_flashcards(note_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Note).where(Note.id == note_id))
    note = res.scalar_one_or_none()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    ai_service = AcademicAssistantService()
    cards = await ai_service.generate_flashcards(note.content)

    created_items = []
    for card in cards:
        item = StudyItem(
            note_id=note.id,
            course_id=note.course_id,
            item_type="flashcard",
            question=card.get("question", ""),
            answer=card.get("answer", "")
        )
        db.add(item)
        created_items.append(item)

    await db.commit()
    return {"success": True, "count": len(created_items)}

@app.get("/api/notes/{note_id}/export-google-doc")
async def export_note_for_google_docs(note_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Note, Course).outerjoin(Course, Note.course_id == Course.id).where(Note.id == note_id))
    row = res.first()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    note, course = row

    html_content = GoogleExportService.format_html_for_google_docs(
        title=note.title,
        content=f"<h3>Executive Summary</h3>\n{note.summary or ''}\n\n<h3>Full Notes</h3>\n{note.content}",
        course_name=course.name if course else "General"
    )

    return Response(
        content=html_content,
        media_type="text/html",
        headers={"Content-Disposition": f"inline; filename={note.title.replace(' ', '_')}.html"}
    )


# 5. Study Items / Flashcards
@app.get("/api/study-items")
async def list_study_items(course_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    query = select(StudyItem).order_by(desc(StudyItem.created_at))
    if course_id:
        query = query.where(StudyItem.course_id == course_id)
    res = await db.execute(query)
    return res.scalars().all()

@app.delete("/api/study-items/{item_id}")
async def delete_study_item(item_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(StudyItem).where(StudyItem.id == item_id))
    await db.commit()
    return {"success": True}


# 6. Moodle Scraper & Sync
@app.post("/api/moodle/login-browser")
async def moodle_launch_browser():
    scraper = MoodleScraper()
    result = scraper.launch_login_browser_process()
    return result

@app.post("/api/moodle/sync")
async def moodle_run_sync(db: AsyncSession = Depends(get_db)):
    scraper = MoodleScraper()
    # Scrape Moodle
    result = await scraper.scrape_moodle(headless=True)
    if not result.get("success"):
        return result

    # Find active current term
    current_term_res = await db.execute(select(Term).where(Term.is_current == True))
    current_term = current_term_res.scalar_one_or_none()
    if not current_term:
        # Fallback to latest term or create default
        latest_term_res = await db.execute(select(Term).order_by(desc(Term.id)).limit(1))
        current_term = latest_term_res.scalar_one_or_none()
        if not current_term:
            current_term = Term(name="Periodo Activo (2026-3)", is_current=True)
            db.add(current_term)
            await db.commit()
            await db.refresh(current_term)
    
    current_term_id = current_term.id

    courses_created = 0
    courses_updated = 0
    active_courses_count = 0

    # 1. Save or update courses
    for c_data in result.get("courses", []):
        moodle_id = c_data["moodle_id"]
        grade_raw = c_data.get("grade")
        grade_clean = grade_raw if grade_raw not in [None, "-", "", " "] else None
        status = "completed" if grade_clean else "active"
        if status == "active":
            active_courses_count += 1

        existing = await db.execute(select(Course).where(Course.moodle_id == moodle_id))
        course_obj = existing.scalar_one_or_none()

        if not course_obj:
            # Check by matching name
            existing_by_name = await db.execute(select(Course).where(Course.name == c_data["name"]))
            course_obj = existing_by_name.scalar_one_or_none()

        if not course_obj:
            course_obj = Course(
                name=c_data["name"],
                code=c_data.get("code", "CURSO"),
                moodle_id=moodle_id,
                moodle_url=c_data.get("moodle_url"),
                grade=grade_clean,
                term_id=current_term_id if status == "active" else None,
                status=status
            )
            db.add(course_obj)
            courses_created += 1
        else:
            course_obj.name = c_data["name"]
            course_obj.moodle_id = moodle_id
            if c_data.get("code"):
                course_obj.code = c_data["code"]
            course_obj.grade = grade_clean
            if c_data.get("moodle_url"):
                course_obj.moodle_url = c_data["moodle_url"]
            course_obj.status = status
            if status == "active" and current_term_id:
                course_obj.term_id = current_term_id
            courses_updated += 1

    await db.commit()

    # Pre-fetch all courses to map IDs easily
    courses_res = await db.execute(select(Course))
    all_courses = courses_res.scalars().all()
    course_moodle_map = {c.moodle_id: c for c in all_courses if c.moodle_id}

    assignments_created = 0
    assignments_updated = 0

    # 2. Save activities scraped directly from course pages
    for c_data in result.get("courses", []):
        c_moodle_id = c_data.get("moodle_id")
        course_obj = course_moodle_map.get(c_moodle_id)
        cid = course_obj.id if course_obj else None

        for act in c_data.get("activities", []):
            mod_uid = act.get("moodle_id")
            parsed_due = None
            if act.get("due_date_iso"):
                try:
                    parsed_due = datetime.datetime.fromisoformat(act["due_date_iso"].replace("Z", "+00:00"))
                except Exception:
                    pass

            existing_asgn = None
            if mod_uid:
                ex_res = await db.execute(select(Assignment).where(Assignment.moodle_id == mod_uid))
                existing_asgn = ex_res.scalar_one_or_none()

            if not existing_asgn and cid:
                ex_res = await db.execute(select(Assignment).where(Assignment.course_id == cid, Assignment.title == act["title"]))
                existing_asgn = ex_res.scalar_one_or_none()

            if existing_asgn:
                if cid:
                    existing_asgn.course_id = cid
                if act.get("url"):
                    existing_asgn.moodle_url = act["url"]
                if mod_uid:
                    existing_asgn.moodle_id = mod_uid
                if act.get("description") and not existing_asgn.description:
                    existing_asgn.description = act["description"]
                if parsed_due and not existing_asgn.due_date:
                    existing_asgn.due_date = parsed_due
                if act.get("due_date_raw") and not existing_asgn.due_date_raw:
                    existing_asgn.due_date_raw = act["due_date_raw"]
                assignments_updated += 1
            else:
                db.add(Assignment(
                    course_id=cid,
                    title=act["title"],
                    description=act.get("description") or f"Actividad de curso: {c_data.get('name')}",
                    due_date=parsed_due,
                    due_date_raw=act.get("due_date_raw"),
                    moodle_url=act.get("url"),
                    moodle_id=mod_uid,
                    source="course_page"
                ))
                assignments_created += 1

    await db.commit()

    # 3. Save calendar deadlines
    for ev in result.get("assignments", []):
        ev_mid = ev.get("moodle_id")
        ev_cid = ev.get("course_moodle_id")
        course_obj = course_moodle_map.get(ev_cid)
        if not course_obj and ev.get("course_name"):
            ev_cname = ev["course_name"].lower()
            course_obj = next((c for c in all_courses if c.name.lower() in ev_cname or ev_cname in c.name.lower()), None)
        
        cid = course_obj.id if course_obj else None

        parsed_due = None
        if ev.get("due_date_iso"):
            try:
                parsed_due = datetime.datetime.fromisoformat(ev["due_date_iso"].replace("Z", "+00:00"))
            except Exception:
                pass

        existing_asgn = None
        if ev_mid:
            ex_res = await db.execute(select(Assignment).where(Assignment.moodle_id == ev_mid))
            existing_asgn = ex_res.scalar_one_or_none()

        if not existing_asgn and cid:
            ex_res = await db.execute(select(Assignment).where(Assignment.course_id == cid, Assignment.title == ev["title"]))
            existing_asgn = ex_res.scalar_one_or_none()

        if existing_asgn:
            if cid:
                existing_asgn.course_id = cid
            if ev.get("moodle_url"):
                existing_asgn.moodle_url = ev["moodle_url"]
            if ev_mid:
                existing_asgn.moodle_id = ev_mid
            if ev.get("description") and len(ev.get("description", "")) > len(existing_asgn.description or ""):
                existing_asgn.description = ev["description"]
            if parsed_due:
                existing_asgn.due_date = parsed_due
            if ev.get("due_date_raw"):
                existing_asgn.due_date_raw = ev["due_date_raw"]
            assignments_updated += 1
        else:
            db.add(Assignment(
                course_id=cid,
                title=ev["title"],
                description=ev.get("description") or f"Moodle Calendar Event: {ev['title']}",
                due_date=parsed_due,
                due_date_raw=ev.get("due_date_raw"),
                moodle_url=ev.get("moodle_url"),
                moodle_id=ev_mid,
                source="calendar"
            ))
            assignments_created += 1

    await db.commit()

    # 4. Process teacher announcements / forum posts using Gemini AI
    ai_service = AcademicAssistantService()
    for c_data in result.get("courses", []):
        announcements = c_data.get("raw_announcements")
        if announcements and len(announcements.strip()) > 30:
            c_obj = course_moodle_map.get(c_data["moodle_id"])
            cid = c_obj.id if c_obj else None

            try:
                extracted_tasks = await ai_service.extract_deadlines_from_text(announcements, course_name=c_data.get("name"))
                for task in extracted_tasks:
                    t_title = task.get("title", "Tarea Anunciada")
                    ex = await db.execute(select(Assignment).where(Assignment.course_id == cid, Assignment.title == t_title))
                    if not ex.scalar_one_or_none():
                        parsed_d = None
                        if task.get("due_date_iso"):
                            try:
                                parsed_d = datetime.datetime.fromisoformat(task["due_date_iso"].replace("Z", "+00:00"))
                            except Exception:
                                pass

                        ai_assignment = Assignment(
                            course_id=cid,
                            title=t_title,
                            description=task.get("description", ""),
                            due_date=parsed_d,
                            due_date_raw=task.get("due_date_raw"),
                            priority=task.get("priority", "medium"),
                            source="teacher_post"
                        )
                        db.add(ai_assignment)
                        assignments_created += 1
            except Exception as err:
                print("Notice during AI announcement extraction:", err)

    await db.commit()

    total_synced_courses = active_courses_count if active_courses_count > 0 else (courses_created + courses_updated)
    total_deliverables_synced = assignments_created + assignments_updated

    return {
        "success": True,
        "courses_synced": total_synced_courses,
        "assignments_synced": total_deliverables_synced,
        "courses_created": courses_created,
        "courses_updated": courses_updated,
        "assignments_created": assignments_created,
        "assignments_updated": assignments_updated,
        "raw_courses_found": len(result.get("courses", [])),
        "raw_assignments_found": len(result.get("assignments", []))
    }

@app.post("/api/moodle/sync-ical")
async def moodle_sync_ical(db: AsyncSession = Depends(get_db)):
    scraper = MoodleScraper()
    result = scraper.sync_from_ical_url()
    if not result.get("success"):
        return result

    # Pre-fetch all courses to link assignments by matching name / code
    courses_res = await db.execute(select(Course))
    all_courses = courses_res.scalars().all()

    events = result.get("events", [])
    added = 0

    for ev in events:
        moodle_uid = ev.get("moodle_id")
        existing = None
        if moodle_uid:
            res = await db.execute(select(Assignment).where(Assignment.moodle_id == moodle_uid))
            existing = res.scalar_one_or_none()
        
        if not existing:
            # Check matching course
            matched_course_id = None
            ev_course_name = ev.get("course_name", "").lower()
            ev_summary = ev.get("title", "").lower()

            for c in all_courses:
                c_name_clean = c.name.lower()
                c_code_clean = (c.code or "").lower()
                if (c_code_clean and c_code_clean in ev_course_name) or (c_name_clean and any(word in ev_course_name or word in ev_summary for word in c_name_clean.split() if len(word) > 4)):
                    matched_course_id = c.id
                    break

            parsed_due = None
            if ev.get("due_date"):
                try:
                    parsed_due = datetime.datetime.fromisoformat(ev["due_date"].replace("Z", "+00:00"))
                except Exception:
                    pass

            assignment = Assignment(
                course_id=matched_course_id,
                title=ev.get("title", "Moodle Deadline"),
                description=ev.get("description", ""),
                due_date=parsed_due,
                source="calendar",
                moodle_id=moodle_uid
            )
            db.add(assignment)
            added += 1

    await db.commit()
    return {"success": True, "added_count": added, "total_events": len(events)}


# 7. AI Chat & Assistant
@app.get("/api/chat/history")
async def get_chat_history(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(ChatMessage).order_by(ChatMessage.created_at.asc()).limit(50))
    messages = res.scalars().all()
    return [{"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat()} for m in messages]

@app.delete("/api/chat/history")
async def clear_chat_history(db: AsyncSession = Depends(get_db)):
    await db.execute(delete(ChatMessage))
    await db.commit()
    return {"success": True}

@app.post("/api/chat")
async def send_chat_message(
    message: str = Form(...),
    file: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db)
):
    # Save user message
    user_msg = ChatMessage(role="user", content=message)
    db.add(user_msg)
    await db.commit()

    # Load history
    history_res = await db.execute(select(ChatMessage).order_by(ChatMessage.created_at.asc()).limit(20))
    history = history_res.scalars().all()
    messages_payload = [{"role": m.role, "content": m.content} for m in history]

    # Academic Context
    academic_context = await get_academic_context(db)

    # Process file if attached
    file_bytes = None
    file_mime_type = None
    if file:
        file_bytes = await file.read()
        file_mime_type = file.content_type

    ai_service = AcademicAssistantService()
    try:
        reply_text = await ai_service.chat(
            messages=messages_payload,
            academic_context=academic_context,
            file_bytes=file_bytes,
            file_mime_type=file_mime_type
        )
    except Exception as e:
        reply_text = f"⚠️ AI Assistant Error: {str(e)}\n\nPlease ensure your `GEMINI_API_KEY` is properly configured in the settings tab."

    # Save assistant reply
    ai_msg = ChatMessage(role="assistant", content=reply_text)
    db.add(ai_msg)
    await db.commit()

    return {
        "reply": reply_text,
        "message_id": ai_msg.id
    }

@app.post("/api/chat/draft-email")
async def draft_professor_email_endpoint(payload: EmailDraftRequest):
    ai_service = AcademicAssistantService()
    draft = await ai_service.draft_professor_email(
        purpose=payload.purpose,
        professor_name=payload.professor_name,
        course_name=payload.course_name,
        details=payload.details,
        tone=payload.tone or "polite and professional"
    )
    return {"draft": draft}


# --- Serve Static UI ---
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return HTMLResponse("<h2>School Agent Backend is Running. Frontend is loading...</h2>")
