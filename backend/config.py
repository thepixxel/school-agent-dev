import os
from pathlib import Path
from dotenv import load_dotenv

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Load environment variables
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

def get_gemini_api_key() -> str:
    """Get latest Gemini API key, reloading from .env if needed."""
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=True)
    key = os.getenv("GEMINI_API_KEY", "")
    return key.strip()

def get_gemini_model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

def get_moodle_url() -> str:
    return os.getenv("MOODLE_URL", "").strip()

def get_moodle_ical_url() -> str:
    return os.getenv("MOODLE_ICAL_URL", "").strip()

# Paths and database
BROWSER_USER_DATA_DIR = str(DATA_DIR / "browser_profile")
DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR / 'school_agent.db'}"
SQLITE_DB_PATH = DATA_DIR / "school_agent.db"
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))


def update_env_variable(key: str, value: str):
    """Update or append an environment variable in the .env file."""
    lines = []
    found = False
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    
    os.environ[key] = value
