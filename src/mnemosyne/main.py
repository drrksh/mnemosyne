"""
Development entrypoint: `python3 -m mnemosyne.main` (or `make run`).

Production deployments should invoke uvicorn/gunicorn directly against
`mnemosyne.api.app:create_app` (an application factory) rather than
this module — this exists purely for local convenience.
"""
from __future__ import annotations

import uvicorn

from mnemosyne.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "mnemosyne.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.app_env == "development",
    )


if __name__ == "__main__":
    main()
