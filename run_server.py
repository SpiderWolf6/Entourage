"""local dev server — hot-reload on source changes, no frontend.

in production use: uvicorn server.main:app --host 0.0.0.0 --port 8000 --workers 1
(see Dockerfile). this file is only for local development.

reload_dirs is explicit to prevent uvicorn from watching the entire repo —
workspaces/ and .venv/ would cause constant restarts otherwise.
"""

import os
import uvicorn

ROOT = os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    uvicorn.run(
        "server.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=[
            os.path.join(ROOT, "server"),
            os.path.join(ROOT, "agents"),
            os.path.join(ROOT, "llm"),
            os.path.join(ROOT, "orchestrator"),
            os.path.join(ROOT, "execution"),
        ],
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "uvicorn.logging.DefaultFormatter",
                    "fmt": "%(levelprefix)s %(message)s",
                },
                "access": {
                    "()": "uvicorn.logging.AccessFormatter",
                    "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                },
            },
            "handlers": {
                "default": {"class": "logging.StreamHandler", "formatter": "default", "stream": "ext://sys.stderr"},
                "access":  {"class": "logging.StreamHandler", "formatter": "access",  "stream": "ext://sys.stdout"},
            },
            "loggers": {
                "uvicorn":        {"handlers": ["default"], "level": "INFO",    "propagate": False},
                # silence per-request access log — too noisy with websocket heartbeats
                "uvicorn.access": {"handlers": ["access"],  "level": "WARNING", "propagate": False},
                "uvicorn.error":  {"handlers": ["default"], "level": "INFO",    "propagate": False},
            },
        },
    )
