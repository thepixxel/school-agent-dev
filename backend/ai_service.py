import json
import asyncio
import time
import datetime
from typing import List, Dict, Any, Optional
from google import genai
from google.genai import types
from google.genai.errors import ServerError, ClientError, APIError
from backend.config import get_gemini_api_key, get_gemini_model

FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.7-flash"]

def get_genai_client(api_key: Optional[str] = None) -> genai.Client:
    key = api_key or get_gemini_api_key()
    if not key:
        raise ValueError("Gemini API key is not configured. Please add it in settings or .env file.")
    return genai.Client(api_key=key)

class AcademicAssistantService:
    def __init__(self, api_key: Optional[str] = None, model_name: Optional[str] = None):
        self.api_key = api_key
        self.model_name = model_name

    def _get_client(self) -> genai.Client:
        return get_genai_client(self.api_key or get_gemini_api_key())

    @property
    def current_model(self) -> str:
        return self.model_name or get_gemini_model()

    def _generate_with_fallback(self, client: genai.Client, contents: Any, config: Optional[types.GenerateContentConfig] = None):
        """
        Executes generation with automatic fallback if the requested model experiences 503 high demand spikes.
        """
        primary = self.current_model
        # Build candidate list starting with primary model
        models_to_try = [primary]
        for fb in FALLBACK_MODELS:
            if fb not in models_to_try:
                models_to_try.append(fb)

        last_error = None
        for model_candidate in models_to_try:
            try:
                response = client.models.generate_content(
                    model=model_candidate,
                    contents=contents,
                    config=config,
                )
                return response
            except (ServerError, ClientError, APIError, Exception) as e:
                err_str = str(e)
                # If 503 high demand or 429 rate limit or 404 deprecated model, try next fallback
                if any(code in err_str for code in ["503", "429", "404", "UNAVAILABLE", "ResourceExhausted", "NOT_FOUND"]):
                    last_error = e
                    time.sleep(0.5)
                    continue
                else:
                    raise e

        if last_error:
            raise last_error
        raise RuntimeError("No response could be generated from Gemini API models.")

    async def chat(
        self,
        messages: List[Dict[str, str]],
        academic_context: Optional[Dict[str, Any]] = None,
        file_bytes: Optional[bytes] = None,
        file_mime_type: Optional[str] = None,
    ) -> str:
        """
        Chat with academic assistant, injecting real-time context of courses, assignments, and notes.
        """
        client = self._get_client()

        # Build comprehensive system prompt
        context_str = ""
        if academic_context:
            current_term = academic_context.get("current_term")
            active_courses = academic_context.get("active_courses", [])
            completed_courses = academic_context.get("completed_courses", [])
            courses = academic_context.get("courses", [])
            assignments = academic_context.get("assignments", [])
            notes = academic_context.get("notes", [])

            context_str += "\n\n=== CURRENT STUDENT CONTEXT ==="
            if current_term:
                context_str += f"\n- Current Academic Term: {current_term.get('name', 'Active Term')}"
            
            if active_courses:
                context_str += f"\n- Active Enrolled Courses ({len(active_courses)}):"
                for c in active_courses:
                    context_str += f"\n  * {c.get('name')} (Code: {c.get('code', 'N/A')})"
            elif courses:
                context_str += f"\n- Enrolled Courses ({len(courses)}):"
                for c in courses:
                    grade_info = f" [Grade: {c.get('grade')}]" if c.get('grade') else " [Active]"
                    context_str += f"\n  * {c.get('name')}{grade_info}"

            if assignments:
                context_str += f"\n- Tracked Assignments / Deliverables / Deadlines ({len(assignments)}):"
                for a in assignments[:25]:
                    due = a.get('due_date') or a.get('due_date_raw') or 'No due date'
                    status = a.get('status', 'pending')
                    course_name = a.get('course_name', 'General')
                    desc = a.get('description', '')
                    desc_snip = f" | Info: {desc[:120]}..." if desc and len(desc) > 10 else ""
                    context_str += f"\n  * [{course_name}] {a.get('title')} (Due: {due}, Status: {status}, Priority: {a.get('priority', 'medium')}){desc_snip}"

            if completed_courses:
                context_str += f"\n- Completed Historical Courses ({len(completed_courses)}):"
                for c in completed_courses[:8]:
                    context_str += f"\n  * {c.get('name')} - Grade: {c.get('grade', 'N/A')}"

            if notes:
                context_str += f"\n- Recent Notes Topics: " + ", ".join([n.get('title', '') for n in notes[:5]])
            context_str += "\n==============================="

        system_instruction = (
            "You are an elite, proactive AI Personal Academic Assistant for an online college/school student.\n"
            "Your duties:\n"
            "1. Help the student organize their time, prioritize assignments, and track deadlines from Moodle.\n"
            "2. When asked about current courses, upcoming deliverables, homework, or exam dates, refer directly to the student context provided above.\n"
            "3. Break down complex homework and projects into actionable steps.\n"
            "4. Help draft respectful, well-structured emails to professors/TAs.\n"
            "5. Explain difficult concepts with clear examples, analogies, and step-by-step explanations.\n"
            "6. Provide study plans and active recall questions.\n"
            "7. Always be encouraging, concise, structured, and helpful. Use clear markdown formatting, tables, and bullet points where appropriate."
            + context_str
        )

        # Build contents
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=msg["content"])]
                )
            )

        if file_bytes and file_mime_type:
            # Add file to last user message part
            file_part = types.Part.from_bytes(data=file_bytes, mime_type=file_mime_type)
            if contents and contents[-1].role == "user":
                contents[-1].parts.append(file_part)
            else:
                contents.append(types.Content(role="user", parts=[file_part]))

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.7,
        )

        response = self._generate_with_fallback(client, contents=contents, config=config)
        return response.text or "I couldn't generate a response. Please try again."

    async def summarize_lecture_or_notes(self, text: str, course_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Summarize lecture notes, extract key takeaways, definitions, and action items.
        """
        client = self._get_client()

        prompt = f"""
Analyze the following lecture notes/text for course "{course_name or 'General'}".
Produce a comprehensive study guide in JSON format containing:
1. "summary": A well-structured, clear executive summary in Markdown format.
2. "key_concepts": A list of strings representing the main core ideas or definitions.
3. "action_items": A list of strings representing homework, reading, or review items identified.
4. "suggested_tags": A list of 3-5 tags for organizing these notes.

Return ONLY a valid JSON object matching this schema.

Lecture Content:
{text}
"""
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
        )

        response = self._generate_with_fallback(client, contents=prompt, config=config)

        try:
            return json.loads(response.text)
        except Exception:
            return {
                "summary": response.text,
                "key_concepts": [],
                "action_items": [],
                "suggested_tags": ["study-notes"]
            }

    async def generate_flashcards(self, text: str, num_cards: int = 6) -> List[Dict[str, str]]:
        """
        Generate flashcards (question + answer) and multiple-choice questions from notes.
        """
        client = self._get_client()

        prompt = f"""
From the following study material, generate {num_cards} high-quality flashcards for active recall study.
Return ONLY a JSON list of objects with the keys:
- "question": The front of the flashcard / question
- "answer": The concise back of the flashcard / correct answer and explanation
- "item_type": "flashcard"

Material:
{text}
"""
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.4,
        )

        response = self._generate_with_fallback(client, contents=prompt, config=config)

        try:
            data = json.loads(response.text)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and "flashcards" in data:
                return data["flashcards"]
            return []
        except Exception:
            return []

    async def draft_professor_email(
        self,
        purpose: str,
        professor_name: str,
        course_name: str,
        details: str,
        tone: str = "polite and professional"
    ) -> str:
        """
        Draft an email to a professor or TA.
        """
        client = self._get_client()

        prompt = f"""
Draft an email to a professor with the following requirements:
- Professor Name: {professor_name}
- Course: {course_name}
- Purpose: {purpose} (e.g., Extension request, Question about assignment, Clarification, Grade inquiry)
- Details/Context: {details}
- Tone: {tone}

Include a clear Subject line and formatted Email Body ready to send.
"""
        response = self._generate_with_fallback(client, contents=prompt)
        return response.text or ""

    async def extract_deadlines_from_text(
        self,
        raw_text: str,
        course_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Extracts structured deadlines, homework assignments, exams, and quizzes from unstructured teacher posts, forum messages, or emails.
        """
        if not raw_text or len(raw_text.strip()) < 10:
            return []

        client = self._get_client()
        current_year = datetime.datetime.now().year

        prompt = f"""
You are an expert academic assistant. Read the following teacher post, forum message, or course syllabus and extract all deliverables, deadlines, quizzes, tasks, and homework assignments into a structured JSON list.
Reference current context: Year {current_year}.

Return ONLY a JSON list of objects with the following schema for each deliverable:
- "title": Concise, clean title of the task/activity (e.g. "Actividad Formativa 2: Ensayo de Algoritmos")
- "course_name": Name of the course (Use '{course_name or ""}' if not mentioned in text)
- "due_date_raw": Extracted original textual date (e.g. "Domingo 15 de Septiembre a las 23:59")
- "due_date_iso": Best estimate ISO 8601 string (e.g. "2026-09-15T23:59:00"). If only a day is mentioned, assume 23:59.
- "description": Key instructions, required format (PDF, MP4, SQL, Word), size limits, and submission rules.
- "priority": "high" (for exams, final projects, regularizations), "medium" (for regular activities/homework), "low" (for extra points/optional tasks)
- "source": "teacher_post"

Text to analyze:
\"\"\"
{raw_text}
\"\"\"
"""
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        )

        try:
            response = self._generate_with_fallback(client, contents=prompt, config=config)
            data = json.loads(response.text)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and "tasks" in data:
                return data["tasks"]
            elif isinstance(data, dict) and "deadlines" in data:
                return data["deadlines"]
            return []
        except Exception as e:
            print("Deadline extraction error:", e)
            return []

    async def breakdown_assignment(
        self,
        title: str,
        description: str,
        course_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generates an actionable step-by-step checklist, estimated time, and tips for completing an assignment.
        """
        client = self._get_client()

        prompt = f"""
You are a top academic coach. Break down the following school assignment into a clear, actionable 3-to-4 step execution plan and checklist for a university student.

Assignment:
- Title: {title}
- Course: {course_name or "General"}
- Instructions / Description: {description}

Return ONLY a JSON object with:
- "summary": Brief 1-2 sentence overview of what is expected
- "estimated_time": e.g. "2 - 3 hours"
- "steps": List of step objects, each with:
    - "step_number": integer
    - "title": Short step name (e.g. "Step 1: Research & Outline")
    - "detail": Specific actionable instructions
    - "estimated_minutes": integer
- "pro_tips": List of 2-3 tips to get full marks (10/10)
- "checklist": List of short checklist strings (e.g. ["Include bibliography in APA format", "Verify file is under 30MB", "Check rubric requirements"])
"""
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
        )

        try:
            response = self._generate_with_fallback(client, contents=prompt, config=config)
            return json.loads(response.text)
        except Exception as e:
            return {
                "summary": description,
                "estimated_time": "1 - 2 hours",
                "steps": [{"step_number": 1, "title": "Complete Deliverable", "detail": description, "estimated_minutes": 60}],
                "pro_tips": ["Review grading rubric", "Submit before the deadline"],
                "checklist": ["Review instructions", "Upload deliverable"]
            }
