"""Wait for PostgreSQL and verify TCP credentials. Does not print secrets.

Exit 0: authenticated. Exit 2: password rejected. Exit 3: database unavailable.
"""
import os
import sys
import time

import psycopg

for attempt in range(30):
    try:
        with psycopg.connect(
            host="127.0.0.1",
            port=5433,
            dbname="job_hire_ai",
            user="ats",
            password=os.environ["ATS_POSTGRES_PASSWORD"],
            connect_timeout=3,
        ):
            pass
        print("PostgreSQL is ready and authentication succeeded.")
        sys.exit(0)
    except psycopg.OperationalError as exc:
        message = str(exc).lower()
        if "password authentication failed" in message:
            print("PostgreSQL rejected the saved password.", file=sys.stderr)
            sys.exit(2)
        if attempt == 29:
            print(
                "PostgreSQL is not reachable on localhost:5433. Check Docker logs.",
                file=sys.stderr,
            )
            sys.exit(3)
        time.sleep(1)
