"""Run the Android-facing workbench. WeChat automation is enabled on Windows by default."""
import argparse
import os
from pathlib import Path

import uvicorn

from .app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--no-automation",
        action="store_true",
        help="run the web/memory workbench without polling or sending through WeChat",
    )
    args = parser.parse_args()

    data = Path(os.environ.get("JUNSHI_DATA_DIR", str(Path.home() / ".wechat-junshi")))
    token = os.environ.get("JUNSHI_ADMIN_TOKEN", "")
    origin = os.environ.get("JUNSHI_PUBLIC_ORIGIN", f"http://127.0.0.1:{args.port}")
    if args.host not in ("127.0.0.1", "localhost") and not origin.startswith("https://"):
        parser.error("Network binding requires JUNSHI_PUBLIC_ORIGIN=https://your-host")
    try:
        app = create_app(
            data / "mobile.sqlite3",
            token,
            origin,
            automation=not args.no_automation,
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
