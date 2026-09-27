import sys
import asyncio
import uvicorn
from backend.config import HOST, PORT

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    print(f"🚀 Starting School Agent Assistant...")
    print(f"📍 Web Dashboard: http://{HOST}:{PORT}")
    uvicorn.run("backend.app:app", host=HOST, port=PORT, reload=True)
