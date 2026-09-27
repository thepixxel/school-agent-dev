import datetime
from typing import List, Optional
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Float, text
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from backend.config import DATABASE_URL

Base = declarative_base()

class Term(Base):
    __tablename__ = "terms"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False) # e.g. "3er Tetramestre (2026-2)"
    is_current = Column(Boolean, default=True)
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    courses = relationship("Course", back_populates="term", cascade="all, delete-orphan")


class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True, index=True)
    term_id = Column(Integer, ForeignKey("terms.id"), nullable=True)
    code = Column(String(50), nullable=True)
    name = Column(String(200), nullable=False)
    instructor = Column(String(100), nullable=True)
    moodle_id = Column(String(100), nullable=True, unique=True)
    moodle_url = Column(String(500), nullable=True)
    color = Column(String(30), default="#3b82f6") # Hex badge color
    grade = Column(String(50), nullable=True) # Final or current course grade
    status = Column(String(50), default="completed") # active, completed
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    term = relationship("Term", back_populates="courses")
    assignments = relationship("Assignment", back_populates="course", cascade="all, delete-orphan")
    notes = relationship("Note", back_populates="course", cascade="all, delete-orphan")


class Assignment(Base):
    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=True)
    title = Column(String(250), nullable=False)
    description = Column(Text, nullable=True)
    due_date = Column(DateTime, nullable=True)
    status = Column(String(50), default="pending") # pending, in_progress, completed
    priority = Column(String(20), default="medium") # low, medium, high
    source = Column(String(50), default="manual") # calendar, teacher_post, forum, manual
    due_date_raw = Column(String(200), nullable=True) # Text date e.g. "Domingo 23:59"
    moodle_id = Column(String(100), nullable=True, unique=True)
    moodle_url = Column(String(500), nullable=True)
    points = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    course = relationship("Course", back_populates="assignments")


class Note(Base):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=True)
    title = Column(String(250), nullable=False)
    content = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    tags = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    course = relationship("Course", back_populates="notes")
    study_items = relationship("StudyItem", back_populates="note", cascade="all, delete-orphan")


class StudyItem(Base):
    __tablename__ = "study_items"

    id = Column(Integer, primary_key=True, index=True)
    note_id = Column(Integer, ForeignKey("notes.id"), nullable=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=True)
    item_type = Column(String(50), default="flashcard") # flashcard, quiz, summary_point
    question = Column(Text, nullable=False) # Front of card / question
    answer = Column(Text, nullable=False) # Back of card / explanation
    options = Column(Text, nullable=True) # JSON list of options for multiple choice
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    note = relationship("Note", back_populates="study_items")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    role = Column(String(20), nullable=False) # user, assistant, system
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
        # Check SQLite migrations
        def check_columns(sync_conn):
            # Courses table migrations
            result = sync_conn.execute(text("PRAGMA table_info(courses);")).fetchall()
            cols = [row[1] for row in result]
            if "grade" not in cols:
                sync_conn.execute(text("ALTER TABLE courses ADD COLUMN grade VARCHAR(50);"))
            if "term_id" not in cols:
                sync_conn.execute(text("ALTER TABLE courses ADD COLUMN term_id INTEGER REFERENCES terms(id);"))
            if "status" not in cols:
                sync_conn.execute(text("ALTER TABLE courses ADD COLUMN status VARCHAR(50) DEFAULT 'completed';"))

            # Assignments table migrations
            res_a = sync_conn.execute(text("PRAGMA table_info(assignments);")).fetchall()
            cols_a = [row[1] for row in res_a]
            if "source" not in cols_a:
                sync_conn.execute(text("ALTER TABLE assignments ADD COLUMN source VARCHAR(50) DEFAULT 'manual';"))
            if "due_date_raw" not in cols_a:
                sync_conn.execute(text("ALTER TABLE assignments ADD COLUMN due_date_raw VARCHAR(200);"))

            # Create default 3rd Tetramestre term if terms table is empty
            terms_count = sync_conn.execute(text("SELECT COUNT(*) FROM terms;")).scalar()
            if terms_count == 0:
                sync_conn.execute(text("INSERT INTO terms (name, is_current) VALUES ('3er Tetramestre (2026-2)', 1);"))
                term_id = sync_conn.execute(text("SELECT id FROM terms WHERE name = '3er Tetramestre (2026-2)';")).scalar()
                sync_conn.execute(text(f"UPDATE courses SET term_id = {term_id} WHERE term_id IS NULL;"))

        await conn.run_sync(check_columns)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
