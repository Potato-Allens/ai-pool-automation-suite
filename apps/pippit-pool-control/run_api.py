import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("API_PORTS", "22023,22021")
os.environ.setdefault("API_WORKERS", "1")
import uvicorn
uvicorn.run("server:app", host=os.environ.get("API_HOST", "127.0.0.1"),
            port=int(os.environ.get("API_PORT", "8787")), log_level="info")

