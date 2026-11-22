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
        "Phase 4.3 · Inspect, review and recalculate documented CV evidence. "
        "Decisions about hiring are not automated; original CV and AI results stay intact."
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
    overview = pd.DataFrame([{
        "CV ID": row["CV ID"],
        "Filename": row["Filename"],
        "JD coverage": (
            f'{row["Evidence score"]}/100'
            if row["Evidence score"] is not None else "Not scored"
        ),
        "Review needed": (
            f'{row["Review needed (%)"]}%'
            if row["Review needed (%)"] is not None else "Unknown"
        ),
        "Classification": row["Classification"],
        "Llama": row["Llama status"],
    } for row in filtered])
    st.dataframe(
        overview, hide_index=True, width="stretch",
        column_config={
            "CV ID": st.column_config.NumberColumn(width="small"),
            "Filename": st.column_config.TextColumn(width="large"),
            "JD coverage": st.column_config.TextColumn(width="small"),
            "Review needed": st.column_config.TextColumn(width="small"),
            "Classification": st.column_config.TextColumn(width="medium"),
            "Llama": st.column_config.TextColumn(width="small"),
        },
    )
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
    st.subheader("Candidate profile")
    profiles = ats.profiles.list(document_id=selected)
    if profiles:
        profile = profiles[0]
        st.caption(
            f"Profile version {profile.extraction_version} · "
            f"Review status: {ats.profiles.status(profile).replace('_', ' ').title()}"
        )
        payload = ats.profiles.effective(profile)
        sections = (
            ("employment_history", "Work experience"),
            ("technical_skills", "Technical skills"),
            ("education", "Education"),
            ("projects", "Projects"),
            ("certifications", "Certifications"),
            ("languages", "Languages"),
        )
        for field, label in sections:
            entries = payload.get(field) or []
            if not entries:
                continue
            with st.expander(f"{label} ({len(entries)})", expanded=field in (
                "employment_history", "technical_skills",
            )):
                for index, entry in enumerate(entries, 1):
                    if not isinstance(entry, dict):
                        st.write(str(entry))
                        continue
                    # Evidence is shown separately; never expose nested JSON here.
                    title = next((
                        str(entry[key]) for key in (
                            "name", "job_title", "degree", "project_name",
                            "institution", "original_wording",
                        ) if entry.get(key)
                    ), f"{label} {index}")
                    st.markdown(f"**{index}. {title}**")
                    for key, value in entry.items():
                        if key in {
                            "evidence", "claim_status", "uncertainty",
                            "name", "job_title", "degree", "project_name",
                            "institution", "original_wording",
                        } or value in (None, "", [], {}):
                            continue
                        friendly = key.replace("_", " ").capitalize()
                        if isinstance(value, (str, int, float, bool)):
                            st.write(f"{friendly}: {value}")
                        elif isinstance(value, list) and all(
                            isinstance(v, (str, int, float)) for v in value
                        ):
                            st.write(f"{friendly}: {', '.join(map(str, value))}")
                    if entry.get("claim_status") != "SUPPORTED":
                        st.caption("Extracted claim requires human verification.")
                    if index != len(entries):
                        st.divider()
        other = [
            (key, value) for key, value in payload.items()
            if key not in {section for section, _ in sections}
            and isinstance(value, list) and value
            and key != "extraction_warnings"
        ]
        for key, entries in other:
            with st.expander(f"{key.replace('_', ' ').title()} ({len(entries)})"):
                for entry in entries:
                    if isinstance(entry, dict):
                        st.write({
                            k.replace("_", " ").title(): v
                            for k, v in entry.items() if k != "evidence"
                        })
                    else:
                        st.write(str(entry))
        with st.expander("Original CV source quotations"):
            evidence = list(profile.evidence)
            if not evidence:
                st.info("No source quotations saved for this CV.")
            for e in evidence:
                label = e.field_path.split(".")[0].replace("_", " ").title()
                st.markdown(f"**{label}** · {e.verification_status.replace('_', ' ').title()}")
                st.write(e.source_excerpt)
                st.caption(
                    f"Page {e.page_number or 'unknown'} · "
                    f"{e.source_reference or 'Source reference unavailable'}"
                )
                st.divider()
        st.subheader("HR evidence review")
        st.caption(
            "Approve, reject or correct a source quotation. Every action is "
            "recorded with a reviewer and reason. Existing AI evidence and "
            "scores remain unchanged until the separate recalculation phase."
        )
        evidence_list = sorted(
            profile.evidence, key=lambda e: (e.field_path, e.source_reference or "", e.id)
        )
        if not evidence_list:
            st.info("No extracted source evidence is available for HR review.")
        else:
            selected_evidence = st.selectbox(
                "Select source quotation",
                evidence_list,
                format_func=lambda e: (
                    f"{e.field_path.replace('_', ' ')} · "
                    f"{e.source_excerpt[:90]} · page {e.page_number or '?'}"
                ),
                key=f"hr_evidence_choice_{selected}_{profile.id}",
            )
            st.markdown("**Original source quotation**")
            st.write(selected_evidence.source_excerpt)
            st.caption(
                f"Original verification: {selected_evidence.verification_status} · "
                f"Source: {selected_evidence.source_reference or 'unknown'}"
            )
            history = ats.evidence_reviews.history(profile.id, selected_evidence.id)
            if history:
                last = history[-1]
                latest = last.corrected_value or {}
                st.info(
                    f"Latest HR review: {latest.get('decision', 'Unknown')} · "
                    f"{last.reviewer} · {last.note}"
                )
                if latest.get("corrected_excerpt"):
                    st.write("HR-corrected quotation:", latest["corrected_excerpt"])
            with st.form(f"hr_evidence_review_{profile.id}_{selected_evidence.id}"):
                decision = st.radio(
                    "HR assessment",
                    ["APPROVE", "REJECT", "CORRECT"],
                    format_func=lambda v: v.title(),
                    horizontal=True,
                )
                corrected = st.text_area(
                    "Corrected quotation (only for Correct)",
                    help="Must appear in the same original CV source block.",
                    key=f"hr_corrected_quote_{selected_evidence.id}",
                )
                reviewer = st.text_input(
                    "Reviewer *", key=f"hr_reviewer_{selected}"
                )
                note = st.text_area(
                    "Reason / review note *",
                    key=f"hr_review_note_{selected_evidence.id}",
                )
                saved = st.form_submit_button("Save HR evidence review", type="primary")
            if saved:
                try:
                    ats.evidence_reviews.record(
                        profile.id, selected_evidence.id, decision, reviewer, note,
                        corrected_excerpt=corrected,
                        expected_last_event_id=history[-1].id if history else None,
                    )
                    st.success("HR evidence review saved to the audit history.")
                    st.rerun()
                except ATSError as exc:
                    st.error(str(exc))
            with st.expander(f"Evidence review history ({len(history)})"):
                for event in reversed(history):
                    value = event.corrected_value or {}
                    st.markdown(
                        f"**{value.get('decision', 'Unknown')}** · "
                        f"{event.reviewer} · {event.created_at}"
                    )
                    st.write(event.note)
                    if value.get("corrected_excerpt"):
                        st.write(value["corrected_excerpt"])
        with st.expander("Advanced: original structured profile (JSON)"):
            st.json(payload)
    else:
        st.info("Candidate profile is not available yet.")

    st.subheader("Job requirements and CV evidence")
    outcome = outcomes[selected]
    if outcome is not None and outcome.status == "COMPLETED" and outcome.breakdown:
        findings = outcome.breakdown
        st.caption("AI-assisted findings from saved CV evidence; not an employment decision.")
    else:
        matches = [
            row for row in ats.job_matching.list_for_vacancy(job.id)
            if row.document_id == selected
        ]
        findings = matches[0].criteria_results if matches else []
        if findings:
            st.caption("Rule-based evidence findings; local Llama score is unavailable.")
    if findings:
        # One criterion per row, not a repeated table row for every excerpt.
        summary_rows = [{
            "Requirement": row.get("criterion") or row.get("name") or "Unnamed",
            "Weight (%)": row.get("weight"),
            "Evidence status": str(row.get("status", "UNKNOWN")).replace("_", " ").title(),
            "Source excerpts": len(row.get("evidence") or []),
        } for row in findings]
        st.dataframe(
            pd.DataFrame(summary_rows), hide_index=True, width="stretch",
            column_config={
                "Requirement": st.column_config.TextColumn(width="medium"),
                "Weight (%)": st.column_config.NumberColumn(width="small"),
                "Evidence status": st.column_config.TextColumn(width="medium"),
                "Source excerpts": st.column_config.NumberColumn(width="small"),
            },
        )
        st.caption(
            "Select a requirement below to read its actual CV quotations. "
            "An isolated skill name does not prove years of experience or proficiency."
        )
        for index, finding in enumerate(findings):
            name = finding.get("criterion") or finding.get("name") or "Unnamed requirement"
            status = str(finding.get("status", "UNKNOWN")).replace("_", " ").title()
            evidence = finding.get("evidence") or []
            with st.expander(
                f"{name} · {status} · {len(evidence)} source excerpt(s)"
            ):
                st.write(f"**JD weight:** {finding.get('weight', 'Not specified')}%")
                reason = (finding.get("reason") or "").strip()
                if reason:
                    st.caption(f"Model explanation: {reason}")
                if not evidence:
                    st.warning("No supporting source excerpt available for this requirement.")
                for n, quote in enumerate(evidence, 1):
                    st.markdown(f"**CV excerpt {n}**")
                    st.write(quote.get("source_excerpt") or "No excerpt text")
                    st.caption(
                        f"Page {quote.get('page_number') or 'unknown'} · "
                        f"{quote.get('source_reference') or 'Unspecified source'}"
                    )
        with st.expander("Advanced: full matching details"):
            st.json(findings)
    else:
        st.info("No current JD evidence matching is stored for this CV.")
    st.subheader("HR-reviewed evidence score")
    st.caption(
        "Recalculate JD evidence coverage using saved HR source reviews. "
        "This does not change the original AI score, hire/reject anyone, "
        "or infer qualifications beyond the cited original CV evidence."
    )
    score_history = []
    if outcome is not None and outcome.status == "COMPLETED":
        try:
            score_history = ats.hr_scores.history(selected)
        except ATSError as exc:
            st.warning(str(exc))
        if score_history:
            most_recent = score_history[0]
            result = most_recent.corrected_value or {}
            before = most_recent.original_value or {}
            left, right, pending = st.columns(3)
            left.metric("Original AI score", f"{before.get('score', 0)}/100")
            right.metric("HR-reviewed score", f"{result.get('score', 0)}/100")
            pending.metric("Still needs review", f"{result.get('review_weight', 0)}%")
            st.caption(
                f"Recalculated by {most_recent.reviewer} · "
                f"{most_recent.created_at} · Audit snapshot {most_recent.id}"
            )
            if result.get("breakdown"):
                st.dataframe(
                    pd.DataFrame(result["breakdown"]),
                    hide_index=True, width="stretch",
                    column_config={
                        "criterion": st.column_config.TextColumn("JD requirement"),
                        "AI status": st.column_config.TextColumn(width="small"),
                        "HR status": st.column_config.TextColumn(width="small"),
                        "weight": st.column_config.NumberColumn("Weight (%)", width="small"),
                    },
                )
            with st.expander(f"Previous HR score calculations ({len(score_history)})"):
                for snapshot in score_history:
                    value = snapshot.corrected_value or {}
                    st.write(
                        f"{snapshot.created_at} · {snapshot.reviewer} · "
                        f"Score {value.get('score', '?')}/100 · "
                        f"Review {value.get('review_weight', '?')}%"
                    )
                    st.caption(snapshot.note)
        else:
            st.info("No HR-reviewed score has been recorded for this CV.")
        with st.form(f"hr_recalculate_{selected}"):
            reviewer = st.text_input("Reviewer name *", key=f"hr_score_reviewer_{selected}")
            reason = st.text_area(
                "Reason for recalculation *", key=f"hr_score_note_{selected}"
            )
            submitted = st.form_submit_button(
                "Recalculate using HR evidence reviews", type="primary"
            )
        if submitted:
            try:
                ats.hr_scores.calculate(selected, reviewer, reason)
                st.success("HR-reviewed score calculation saved.")
                st.rerun()
            except ATSError as exc:
                st.error(str(exc))
    else:
        st.info(
            "A completed current Llama evidence assessment is required "
            "before HR score recalculation."
        )
    st.caption(
        "Original model scores remain unchanged. HR-reviewed evidence scores "
        "are separate auditable snapshots. Human hiring decisions are Phase 4.4."
    )
