"""Phase 4.1: read-only, source-grounded HR candidate inspection.

No hiring decisions or evidence edits are performed on this screen.
"""
import pandas as pd
import streamlit as st

from job_hire_ai.errors import ATSError
from job_hire_ai.services.llama_scoring_service import evidence_classification


def review_summary(rows, outcomes, snapshots, good, medium):
    """Build a CV-specific overview without inventing scores or review decisions."""
    result = []
    for item in rows:
        doc_id = item["CV"]
        outcome = outcomes.get(doc_id)
        score = snapshots.get(doc_id)
        completed = outcome is not None and outcome.status == "COMPLETED"
        points = outcome.score if completed else (
            score.documented_weight if score is not None else None
        )
        review = outcome.review_weight if completed else (
            score.review_weight if score is not None else None
        )
        result.append({
            "CV ID": doc_id,
            "Application": item["Application"],
            "Filename": item["Filename"],
            "Evidence score": points,
            "Classification": evidence_classification(points, review, good, medium),
            "Review needed (%)": review,
            "Scoring source": "Llama + evidence" if completed else (
                "Literal evidence" if score is not None else "Not scored"
            ),
            "Llama status": outcome.status if outcome is not None else "Not evaluated",
            "Pipeline scoring": item.get("SCORING", "WAITING"),
        })
    return result


def criterion_details(breakdown):
    """Display source excerpts as separate rows, with clear criterion provenance."""
    results = []
    for finding in breakdown or []:
        excerpts = finding.get("evidence") or []
        if not excerpts:
            results.append({
                "Criterion": finding.get("criterion") or finding.get("name"),
                "Weight": finding.get("weight"),
                "Status": finding.get("status"),
                "Reason": finding.get("reason", ""),
                "Source": None,
                "Page": None,
                "Verified excerpt": None,
            })
        for evidence in excerpts:
            results.append({
                "Criterion": finding.get("criterion") or finding.get("name"),
                "Weight": finding.get("weight"),
                "Status": finding.get("status"),
                "Reason": finding.get("reason", ""),
                "Source": evidence.get("source_reference"),
                "Page": evidence.get("page_number"),
                "Verified excerpt": evidence.get("source_excerpt"),
            })
    return results


def candidate_review(ats):
    st.title("HR Candidate Review")
    st.caption(
        "Phase 4.1 · Inspect stored CVs, source evidence and JD coverage. "
        "This page does not approve, reject, rank or edit applicants."
    )
    vacancies = ats.vacancies.list()
    if not vacancies:
        st.info("Create a job in Job Management before reviewing candidates.")
        return
    requested = st.session_state.get("hr_review_job")
    index = next((i for i, job in enumerate(vacancies) if job.id == requested), 0)
    job = st.selectbox(
        "Job position", vacancies, index=index,
        format_func=lambda v: f"{v.id} · {v.title} ({v.status})",
        key="hr_review_job_choice",
    )
    st.session_state.hr_review_job = job.id
    _, all_items = ats.pipeline.overview(job.id)
    if not all_items:
        st.info("No uploaded CVs for this job.")
        return
    snapshots = {}
    for snapshot in ats.candidate_scoring.list_for_vacancy(job.id):
        snapshots.setdefault(snapshot.document_id, snapshot)
    outcomes = {
        item["CV"]: ats.llama_scoring.current(item["CV"]) for item in all_items
    }
    summary = review_summary(
        all_items, outcomes, snapshots, job.good_threshold, job.medium_threshold
    )
    search = st.text_input(
        "Find CV (filename, application or ID)", key="hr_review_search"
    ).strip().casefold()
    filtered = [
        row for row in summary
        if not search or search in (
            f'{row["Filename"]} {row["Application"]} {row["CV ID"]}'
        ).casefold()
    ]
    st.caption(f"{len(filtered)} of {len(summary)} uploaded CVs")
    if not filtered:
        st.info("No matching CVs.")
        return
    st.dataframe(pd.DataFrame(filtered), hide_index=True, width="stretch")
    options = {row["CV ID"]: row for row in filtered}
    selected = st.selectbox(
        "Inspect CV", list(options),
        format_func=lambda doc_id: (
            f'CV {doc_id} · {options[doc_id]["Filename"]}'
        ), key=f"hr_review_cv_{job.id}",
    )
    item = options[selected]
    st.subheader(item["Filename"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Documented JD coverage", (
        f'{item["Evidence score"]}/100'
        if item["Evidence score"] is not None else "Not scored"
    ))
    c2.metric("Review required", (
        f'{item["Review needed (%)"]}%'
        if item["Review needed (%)"] is not None else "Unknown"
    ))
    c3.metric("Classification", item["Classification"])
    st.caption(
        f'Application {item["Application"]} · {item["Scoring source"]} · '
        f'Local Llama: {item["Llama status"]}. '
        "Scores are documented JD evidence coverage, not hiring suitability."
    )
    if item["Llama status"] == "FAILED":
        st.warning(
            "Llama scoring failed. The displayed score may be an older rule-based "
            "snapshot. Inspect the Pipeline Monitor for the error and retry."
        )
    try:
        filename, data, kind = ats.documents.download(selected)
        mime = (
            "application/pdf" if kind == "pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        st.download_button(
            "Download original CV", data=data, file_name=filename,
            mime=mime, key=f"hr_review_download_{selected}",
        )
    except ATSError as exc:
        st.warning(f"Original CV unavailable: {exc}")
    with st.expander("Stored candidate profile", expanded=True):
        profiles = ats.profiles.list(document_id=selected)
        if profiles:
            profile = profiles[0]
            st.caption(
                f"Profile {profile.id}, version {profile.extraction_version} · "
                f"review status {ats.profiles.status(profile)}"
            )
            st.json(ats.profiles.effective(profile))
            with st.expander("Verified original CV evidence"):
                evidence = [
                    {
                        "Field": e.field_path,
                        "Status": e.verification_status,
                        "Source": e.source_reference,
                        "Page": e.page_number,
                        "Excerpt": e.source_excerpt,
                    }
                    for e in profile.evidence
                ]
                if evidence:
                    st.dataframe(pd.DataFrame(evidence), hide_index=True, width="stretch")
                else:
                    st.info("No original CV evidence references have been saved.")
        else:
            st.info("Candidate profile is not available yet.")
    with st.expander("JD criteria and matched evidence", expanded=True):
        outcome = outcomes[selected]
        if outcome is not None and outcome.status == "COMPLETED" and outcome.breakdown:
            st.caption("Local Llama findings; each source quote must be checked by HR.")
            details = criterion_details(outcome.breakdown)
        else:
            matches = [
                row for row in ats.job_matching.list_for_vacancy(job.id)
                if row.document_id == selected
            ]
            if matches:
                st.caption("Conservative rule-based matching; Llama score unavailable.")
                details = criterion_details(matches[0].criteria_results)
            else:
                details = []
        if details:
            st.dataframe(pd.DataFrame(details), hide_index=True, width="stretch")
        else:
            st.info("No current JD evidence matching is stored for this CV.")
    st.info(
        "Read-only review: correcting evidence, recalculating after HR corrections, "
        "recording HR decisions and review history belong to phases 4.2–4.5."
    )
