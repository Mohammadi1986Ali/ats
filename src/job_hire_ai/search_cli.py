"""Inspect real, source-backed semantic retrieval without changing the dashboard.

Example:
    uv run --no-sync python -m job_hire_ai.search_cli --vacancy-id 1 --query "Java concurrency"
"""

import argparse
import sys

from job_hire_ai.errors import ATSError
from job_hire_ai.runtime import Runtime
from job_hire_ai.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Search indexed CV or JD passages locally")
    parser.add_argument("--vacancy-id", type=int, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--source", choices=("CV", "JD"), default="CV")
    parser.add_argument("--document-id", type=int)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    settings = Settings()
    if not settings.embeddings_enabled:
        parser.error("Enable ATS_EMBEDDINGS_ENABLED=true in your local .env first.")
    runtime = Runtime(settings)
    try:
        results = runtime.semantic_search.search(
            args.query,
            vacancy_id=args.vacancy_id,
            source_type=args.source,
            document_id=args.document_id,
            limit=args.limit,
        )
        if not results:
            print("No matching indexed passages found for this job and source.")
        for index, hit in enumerate(results, 1):
            print(
                f"\n{index}. similarity={hit.similarity:.4f} "
                f"job={hit.vacancy_id} document={hit.document_id} "
                f"source={hit.source_type} reference={hit.reference}\n"
                f"{hit.text}"
            )
        return 0
    except (ATSError, ValueError) as exc:
        print(f"Semantic search failed: {exc}", file=sys.stderr)
        return 1
    finally:
        runtime.db.engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
