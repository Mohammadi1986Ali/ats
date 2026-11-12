import json

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from job_hire_ai.ai.cv_schemas import PROFILE_SECTIONS, CVExtraction, ReviewInput
from job_hire_ai.errors import ATSError


def _notice(message):
    st.session_state.notice = message
    st.rerun()


def cv_analysis_page(ats):
    st.title("CV Analysis")
    st.caption(
        "Local qualification extraction and HR review. No matching, scores or hiring decisions."
    )
    vacancies = ats.vacancies.list()
    if not vacancies:
        st.info("Create a vacancy and upload CVs in Candidate Management to start.")
        return
    vacancy = st.selectbox(
        "Vacancy",
        vacancies,
        format_func=lambda v: f"{v.id} · {v.title} ({v.status})",
        key="cv_vacancy",
    )
    rows = ats.cv_analysis.list_documents(vacancy.id)
    status = ats.provider.check()
    st.write(f"Local model: {status.model}")
    if status.available:
        st.success("Ollama connected · configured model available")
    else:
        st.warning(status.message)
    if not rows:
        st.info("No uploaded CVs for this vacancy. Upload PDF/DOCX files in Candidate Management.")
    else:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        choices = {row["Document"]: row for row in rows}
        selected = st.multiselect(
            "Select CVs",
            list(choices),
            format_func=lambda key: (
                f"{key} · {choices[key]['Candidate']} · {choices[key]['Filename']}"
            ),
            key=f"cv_selection_{vacancy.id}",
        )
        if st.button("Analyze Selected", disabled=not selected, type="primary"):
            _queue(ats, vacancy.id, selected)
        pending = [r["Document"] for r in rows if r["Profile"] is None and r["Status"] == "PENDING"]
        if st.button("Analyze All Pending", disabled=not pending):
            _queue(ats, vacancy.id, pending[:100])
        st.caption("Analysis creates a persistent batch and processes CVs sequentially.")
        if len(pending) > 100:
            st.info(
                "The first 100 pending CVs are queued; process them before queuing the remainder."
            )
    _batches(ats, vacancy.id)
    _profiles(ats, vacancy.id)


def _queue(ats, vacancy_id, document_ids, force=False):
    try:
        batch_id = ats.cv_analysis.create_batch(vacancy_id, document_ids, force)
        st.session_state[f"active_cv_batch_{vacancy_id}"] = batch_id
        _execute(ats, batch_id)
    except ATSError as exc:
        st.error(str(exc))


def _execute(ats, batch_id, progress=None):
    if progress is None:
        bar, current, counters = st.progress(0), st.empty(), st.empty()

        def progress(values, document_id, stage):
            finished = values["Completed"] + values["Failed"] + values["Needs review"]
            bar.progress(finished / values["Total"])
            current.info(f"Current document: {document_id or '—'} · {stage or 'Saved progress'}")
            counters.write(values)

    with st.spinner("Processing CVs sequentially with the local model…", show_time=True):
        ats.cv_analysis.process_batch(batch_id, progress)
    _notice("Batch processing finished. Inspect saved profiles and any failed documents.")


def _batches(ats, vacancy_id):
    st.subheader("Analysis progress")
    batches = ats.cv_analysis.batches(vacancy_id)
    if not batches:
        st.info("No analysis batches yet.")
        return
    requested = st.session_state.get(f"active_cv_batch_{vacancy_id}")
    initial = next((i for i, b in enumerate(batches) if b.id == requested), 0)
    batch = st.selectbox(
        "Batch history",
        batches,
        index=initial,
        format_func=lambda b: f"Batch {b.id} · {len(b.jobs)} CVs · {b.created_at} UTC",
        key=f"batch_history_{vacancy_id}_{requested}",
    )
    summary = ats.cv_analysis.summary(batch.id)
    st.write(summary)
    jobs = ats.cv_analysis.jobs(batch.id)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Job": j.id,
                    "Document": j.document_id,
                    "Status": j.status,
                    "Attempts": j.attempts,
                    "Reused extraction": j.reused_profile,
                    "Duration (ms)": j.duration_ms,
                    "Failure details": j.error_summary,
                    "Chunks completed": j.runs[-1].chunks_completed if j.runs else 0,
                    "Total chunks": j.runs[-1].chunk_count if j.runs else 0,
                    "Model calls": j.runs[-1]
                    .inference_options.get("metrics", {})
                    .get("model_calls")
                    if j.runs
                    else None,
                    "Output tokens": j.runs[-1]
                    .inference_options.get("metrics", {})
                    .get("output_tokens")
                    if j.runs
                    else None,
                }
                for j in jobs
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    progress_bar = st.progress(
        (summary["Completed"] + summary["Failed"] + summary["Needs review"]) / summary["Total"]
    )
    current = st.empty()
    elapsed = st.empty()

    def report(values, document_id, stage):
        finished = values["Completed"] + values["Failed"] + values["Needs review"]
        progress_bar.progress(finished / values["Total"])
        current.info(f"Current document: {document_id or '—'} · {stage or 'Saved progress'}")
        elapsed.write(values)

    if st.button("Run / Resume Batch", disabled=not (summary["Pending"] or summary["Processing"])):
        try:
            _execute(ats, batch.id, report)
        except ATSError as exc:
            st.error(str(exc))
    if st.button("Retry Failed", disabled=not summary["Failed"]):
        try:
            ats.cv_analysis.retry_failed(batch.id)
            _execute(ats, batch.id, report)
        except ATSError as exc:
            st.error(str(exc))


def _profiles(ats, vacancy_id):
    st.subheader("Candidate profiles and extraction history")
    profiles = ats.profiles.list(vacancy_id)
    if not profiles:
        st.info("No extracted profiles yet. Run a CV analysis batch to create profiles.")
        return
    profile = st.selectbox(
        "Extraction version",
        profiles,
        format_func=lambda p: (
            f"{p.candidate.reference} · {p.document.original_filename} · "
            f"version {p.extraction_version} · profile {p.id}"
        ),
        key=f"profile_version_{vacancy_id}",
    )
    status = ats.profiles.status(profile)
    run = profile.analysis_run
    st.write(
        f"Candidate: **{profile.candidate.reference}** · "
        f"Source: {profile.document.original_filename}"
    )
    st.write(
        f"Extraction status: **{status}** · extraction version **{profile.extraction_version}**"
    )
    st.caption(
        f"Model: {run.model_name} · digest: {run.model_digest} · prompt: {run.prompt_version} · "
        f"schema: {run.schema_version} · duration: {run.duration_ms} ms · chunks: {run.chunk_count}"
    )
    if metrics := run.inference_options.get("metrics"):
        st.write(
            {
                "Model calls (including retries)": metrics["model_calls"],
                "Input tokens": metrics["input_tokens"],
                "Output tokens": metrics["output_tokens"],
                "Inference duration (ms)": metrics["duration_ms"],
                "Truncated responses": metrics["truncated_responses"],
            }
        )
        st.caption("Missing token counts mean the server did not report usage for every attempt.")
    for warning in profile.warnings:
        st.warning(warning)
    if profile.reviews:
        st.info(
            "HR corrections are shown below. Original extraction and quoted evidence are retained."
        )
    effective = ats.profiles.effective(profile)
    for section in PROFILE_SECTIONS:
        with st.expander(section.replace("_", " ").title(), expanded=section == "technical_skills"):
            if not effective[section]:
                st.caption("NOT_DOCUMENTED · This does not establish absence of a qualification.")
            else:
                for index, record in enumerate(effective[section]):
                    corrected = any(
                        event.field_path == section
                        or event.field_path.startswith(f"{section}.{index}.")
                        for event in profile.reviews
                    )
                    st.caption(
                        f"Record {index} · "
                        f"{'HR corrected (inspect source)' if corrected else 'AI extraction'}"
                    )
                    st.json(record)
    with st.expander("Source evidence viewer"):
        source = ats.profiles.source(profile.id)
        references = {segment.reference: segment for segment in source.segments}
        st.caption(
            "Quotation verification establishes text location, not semantic truth or suitability."
        )
        for evidence in profile.evidence:
            st.write(
                f"{evidence.field_path} · {evidence.verification_status} · "
                f"page {evidence.page_number or '—'} · {evidence.source_reference or 'unspecified'}"
            )
            st.text(evidence.source_excerpt)
            st.caption(
                f"Normalized character offsets: {evidence.character_start}–{evidence.character_end}"
            )
        reference = st.selectbox(
            "Inspect source location", list(references), key=f"source_{profile.id}"
        )
        if reference:
            st.text(references[reference].text)
        st.caption(
            "Source text is local personal data. Application removal deletes all extraction data."
        )
    _corrections(ats, profile, effective)
    if st.checkbox(
        "Create a new extraction version, preserving this profile", key=f"recheck_{profile.id}"
    ):
        if st.button("Re-analyze", key=f"reanalyze_{profile.id}"):
            _queue(ats, vacancy_id, [profile.document_id], force=True)


def _corrections(ats, profile, effective):
    with st.expander("HR corrections and validation"):
        st.caption(
            "Use a field path such as technical_skills.0.name. To add an omitted record, replace "
            "a section such as education with a complete JSON array following the profile schema. "
            "Values must be JSON: quote strings, use brackets for lists and null for unknowns."
        )
        with st.expander("Profile JSON schema / original AI extraction"):
            st.json(CVExtraction.model_json_schema())
            st.json(profile.original_extraction)
        with st.form(f"correction_{profile.id}_{len(profile.reviews)}"):
            path = st.text_input("Field corrected", key=f"field_{profile.id}")
            value = st.text_area("Corrected value (JSON)", key=f"value_{profile.id}")
            reviewer = st.text_input(
                "Reviewer identifier (optional)", max_chars=200, key=f"cv_reviewer_{profile.id}"
            )
            note = st.text_area("Review note", max_chars=2000, key=f"review_note_{profile.id}")
            saved = st.form_submit_button("Save Correction")
        if saved:
            try:
                correction = ReviewInput(
                    field_path=path,
                    corrected_value=json.loads(value),
                    reviewer=reviewer or None,
                    note=note,
                )
                ats.profiles.correct(profile.id, correction, len(profile.reviews))
                _notice("Correction saved separately. Review and validate the corrected profile.")
            except (ValidationError, json.JSONDecodeError):
                st.error(
                    "Use JSON matching the field type and enter a review note. No correction saved."
                )
            except ATSError as exc:
                st.error(str(exc))
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Field": event.field_path,
                        "Original value": json.dumps(event.original_value),
                        "Corrected value": json.dumps(event.corrected_value),
                        "Reviewer": event.reviewer,
                        "Timestamp (UTC)": event.created_at,
                        "Note": event.note,
                    }
                    for event in profile.reviews
                ]
            ),
            hide_index=True,
        )
        validation_note = st.text_input(
            "Profile validation note", key=f"validate_note_{profile.id}"
        )
        validation_reviewer = st.text_input(
            "Validation reviewer (optional)", max_chars=200, key=f"validate_reviewer_{profile.id}"
        )
        acknowledged = st.checkbox(
            "I reviewed the source and all claims, corrections, warnings and unverified evidence. "
            "Validation acknowledges these issues; it does not verify unsupported claims.",
            key=f"validate_ack_{profile.id}_{len(profile.reviews)}",
        )
        if st.button(
            "Save Validated Profile", disabled=not acknowledged or not validation_note.strip()
        ):
            try:
                ats.profiles.validate(
                    profile.id,
                    validation_reviewer or None,
                    validation_note,
                    acknowledged,
                    len(profile.reviews),
                )
                _notice(
                    "HR validation recorded. Original extraction and evidence remain unchanged."
                )
            except ATSError as exc:
                st.error(str(exc))
