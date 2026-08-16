#!/usr/bin/env python3
"""Start NanoEMR.

    python3 run.py                 # http://127.0.0.1:8765
    python3 run.py --port 9000
    python3 run.py --demo          # seed demo patients/visits/labs/bills first
    python3 run.py --reset --demo  # start from a clean database
    python3 run.py --base-uri /emr # serve under a sub-path, behind a proxy
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    parser = argparse.ArgumentParser(description="NanoEMR (NRCES/ABDM FHIR R4)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true",
                        help="seed demonstration clinical data")
    parser.add_argument("--reset", action="store_true",
                        help="delete the existing database first")
    parser.add_argument("--no-serve", action="store_true",
                        help="set up the database and exit")
    parser.add_argument("--base-uri", default=os.environ.get("NANOEMR_BASE_URI", ""),
                        metavar="PREFIX",
                        help="serve every route under a URL prefix, e.g. /emr "
                             "(default: the root; env NANOEMR_BASE_URI)")
    args = parser.parse_args()

    from emr.web.router import set_base

    try:
        base = set_base(args.base_uri)
    except ValueError as exc:
        parser.error(str(exc))
    if base:
        print(f"  mounted under {base}")

    from emr import db

    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            path = db.DB_PATH + suffix
            if os.path.exists(path):
                os.remove(path)
        print(f"  removed {db.DB_PATH}")

    from emr.app import create_app

    app = create_app()

    if args.demo:
        from emr import demo
        demo.seed_demo()

    if args.no_serve:
        print("  database ready at", db.DB_PATH)
        return

    from emr.web.router import serve
    serve(app, args.host, args.port)


if __name__ == "__main__":
    main()
