"""Inspect Phase 3.6 source-backed job matches without starting the worker.

    uv run --no-sync python -m job_hire_ai.match_cli --vacancy-id 1
"""

import argparse
import sys

from job_hire_ai.database.session import Database
from job_hire_ai.services.job_matching_service import JobMatchingService
from job_hire_ai.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect existing source-backed job matches")
    parser.add_argument("--vacancy-id", type=int, required=True)
    parser.add_argument("--document-id", type=int)
    args = parser.parse_args()
    if args.vacancy_id < 1 or (args.document_id is not None and args.document_id < 1):
        parser.error("IDs must be positive.")
    db = Database(Settings().database_url)
    try:
        matches = JobMatchingService(db).list_for_vacancy(args.vacancy_id)
        matches = [
            m for m in matches
            if args.document_id is None or m.document_id == args.document_id
        ]
        if not matches:
            print("No current approved-criteria matches found for this vacancy.")
        for record in matches:
            print(
                f"\nDocument {record.document_id} | profile {record.profile_id} | "
                f"approved criteria configuration {record.configuration_id}"
            )
            for criterion in record.criteria_results:
                print(
                    f"  {criterion['name']} ({criterion['requirement_type']}): "
                    f"{criterion['status']}"
                )
                for evidence in criterion["evidence"]:
                    print(
                        f"    Source {evidence['source_reference']} "
                        f"page={evidence['page_number']}: {evidence['source_excerpt']}"
                    )
        return 0
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
