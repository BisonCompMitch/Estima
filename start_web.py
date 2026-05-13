"""Launch the BisonScope web server."""

from __future__ import annotations

import sys


def main() -> None:
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is required. Install web dependencies with:")
        print("  pip install -r requirements.txt")
        sys.exit(1)

    print("BisonScope web server starting…")
    print("Open http://127.0.0.1:8000 in your browser.")
    uvicorn.run(
        "web.app:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
