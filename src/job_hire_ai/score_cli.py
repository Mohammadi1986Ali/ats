"""Read saved evidence-coverage breakdowns, never rank or reject candidates.

Usage: uv run --no-sync python -m job_hire_ai.score_cli --vacancy-id 1
"""

import argparse
import sys

from job_hire_ai.database.session import Database
from job_hire_ai.services.candidate_scoring_service import CandidateScoringService
from job_hire_ai.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect evidence coverage for approved criteria")
    parser.add_argument("--vacancy-id", type=int, required=True)
    parser.add_argument("--document-id", type=int)
    args = parser.parse_args()
    if args.vacancy_id < 1 or args.document_id is not None and args.document_id < 1:
        parser.error("IDs must be positive.")
    db = Database(Settings().database_url)
    try:
        records = CandidateScoringService(db).list_for_vacancy(args.vacancy_id)
        records = [
            row for row in records
            if args.document_id is None or row.document_id == args.document_id
        ]
        if not records:
            print("No current approved-criteria evidence coverage snapshots found.")
        for row in records:
            print(
                f"\nCV {row.document_id} | profile={row.profile_id} "
                f"criteria={row.configuration_id} | method={row.method_version}"
            )
            print(
                f"Documented weight: {row.documented_weight}% | "
                f"Not evidenced: {row.unverified_weight}% | "
                f"Needs review: {row.review_weight}%"
            )
            print("Not evidenced is unknown, not a negative evaluation.")
            for criterion in row.criterion_breakdown:
                print(
                    f"  {criterion['name']} [{criterion['requirement_type']}] "
                    f"weight={criterion['weight']}% "
                    f"status={criterion['evidence_status']}"
                )
                for evidence in criterion["evidence"]:
                    print(
                        f"    {evidence['source_reference']}: "
                        f"{evidence['source_excerpt']}"
                    )
        return 0
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
