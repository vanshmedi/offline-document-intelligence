"""Run the API: python -m product_intel.api"""

from __future__ import annotations

import uvicorn

from product_intel.config import settings


def main() -> None:
    uvicorn.run(
        "product_intel.api.app:app",
        host="127.0.0.1",
        port=8000,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
