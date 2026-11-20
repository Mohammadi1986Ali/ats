"""Job-first dashboard and live pipeline monitor backed only by persisted ATS records."""

import pandas as pd
import streamlit as st

from job_hire_ai.dashboard_view import (
    LABELS,
    STAGES,
    aggregate,
    current_stage,
    document_rows,
    failure_rows,
    job_pulses,
    stage_breakdown,
)
from job_hire_ai.errors import ATSError
from job_hire_ai.services.llama_scoring_service import (
    evidence_classification,
    score_band,
)


def _open_job(vacancy_id: int) -> None:
    st.session_state.navigation = "Job Management"
    st.session_state.open_vacancy_id = vacancy_id
    st.session_state.job_status_filter = "All"


def _open_monitor(vacancy_id: int) -> None:
    st.session_state.monitor_job = vacancy_id
    st.session_state.navigation = "Pipeline Monitor"


def _format_job(job) -> str:
    return f"{job.id} · {job.title} ({job.status})"


def _job_table(pulses):
    return pd.DataFrame(
        [
            {
                "Job ID": p.vacancy_id,
                "Position": p.job,
                "Job status": p.job_status,
                "CVs": p.uploaded,
                "Queued": p.queued,
                "Running": p.running,
                "Text extracted": p.completed,
                "Failed": p.failed,
                "Text progress": f"{p.progress}%",
                "Pipeline": p.state,
            }
            for p in pulses
        ]
    )


def _kpis(pulses):
    data = aggregate(pulses)
    for column, (label, value) in zip(st.columns(5), data.items(), strict=True):
        column.metric(label, value)
    st.caption(
        "Text complete counts only text extraction. JD/CV vector comparison "
        "runs automatically when source embeddings are ready; HR review is separate."
    )


@st.fragment(run_every="4s")
def dashboard_live(ats):
    vacancies = ats.vacancies.list()
    _, items = ats.pipeline.overview()
    pulses = job_pulses(vacancies, items)
    _kpis(pulses)
    st.subheader("Jobs and their CV pipelines")
    if not vacancies:
        st.info("No jobs yet. Create a vacancy in Job Management to begin.")
        return
    st.dataframe(_job_table(pulses), hide_index=True, width="stretch")
    with st.expander("Delete selected Jobs and JDs", expanded=False):
        options = {job.id: job.title for job in vacancies}
        chosen_jobs = st.multiselect(
            "Select Jobs to permanently delete (including their CVs)",
            list(options), format_func=lambda job_id: f"Job {job_id} · {options[job_id]}",
            key="dashboard_delete_jobs",
        )
        confirm_jobs = st.checkbox(
            f"I confirm permanent deletion of {len(chosen_jobs)} selected Job(s), "
            "their JD, CVs, and analysis records.",
            key="dashboard_delete_jobs_confirm",
        )
        if st.button(
            "Delete selected Jobs", type="primary",
            disabled=not chosen_jobs or not confirm_jobs,
            key="dashboard_delete_jobs_action",
        ):
            try:
                deleted = ats.deletion.delete_vacancies(chosen_jobs)
                st.session_state.pop("dashboard_delete_jobs", None)
                st.session_state.pop("dashboard_delete_jobs_confirm", None)
                st.success(f"Deleted {deleted} selected Jobs and their associated data.")
                st.rerun()
            except ATSError as exc:
                st.error(str(exc))
    st.caption(
        "Every row is a Job. CV totals are scoped to that Job; "
        "archived jobs remain visible."
    )
    choice = st.selectbox(
        "Inspect a Job", vacancies, format_func=_format_job, key="dashboard_job_select"
    )
    selected = next(p for p in pulses if p.vacancy_id == choice.id)
    with st.container(border=True):
        st.markdown(f"**{choice.title}** · {selected.uploaded} CVs · {selected.state}")
        st.progress(selected.completed / selected.uploaded if selected.uploaded else 0,
                    text=f"{selected.completed} / {selected.uploaded} text extractions completed")
        columns = st.columns(4)
        for col, (name, n) in zip(columns, [("Queued", selected.queued),
                                             ("Running", selected.running),
                                             ("Complete", selected.completed),
                                             ("Failed", selected.failed)], strict=True):
            col.metric(name, n)
        st.button("Open this Job's CV pipeline", key=f"open_pipeline_{choice.id}",
                  type="primary", on_click=_open_monitor, args=(choice.id,))
    st.subheader("CV scores & classifications")
    # The pre-existing evidence-coverage score is a real stored result even when
    # local Llama has not completed. Never label that score as Llama output.
    baseline = {}
    for saved in ats.candidate_scoring.list_for_vacancy(choice.id):
        baseline.setdefault(saved.document_id, saved)
    score_rows = []
    for item in items:
        if item["Job ID"] != choice.id:
            continue
        outcome = ats.llama_scoring.current(item["CV"])
        llama_done = outcome is not None and outcome.status == "COMPLETED"
        previous = baseline.get(item["CV"])
        points = outcome.score if llama_done else (
            previous.documented_weight if previous is not None else None
        )
        review = outcome.review_weight if llama_done else (
            previous.review_weight if previous is not None else None
        )
        score_rows.append({
            "CV ID": item["CV"],
            "Filename": item["Filename"],
            "Score / 100": points,
            "Classification": evidence_classification(
                points, review, choice.good_threshold, choice.medium_threshold
            ),
            "Method": (
                "Llama + verified evidence" if llama_done
                else "Rule-based evidence" if previous is not None else "Not scored"
            ),
            "Llama status": outcome.status if outcome else "QUEUED",
            "Review required (%)": review,
            "Error": outcome.error_summary if outcome else None,
        })
    if score_rows:
        st.dataframe(pd.DataFrame(score_rows), hide_index=True, width="stretch")
        st.caption(
            "Score 0 means zero JD weight has confirmed supporting evidence; it does "
            "not mean the candidate has zero qualifications. Needs review means "
            "unresolved JD evidence could change the classification. "
            "Scores measure documented JD evidence, not hiring suitability. "
            "The Method column distinguishes rule-based scores from completed Llama scores. "
            "Open Pipeline Monitor for source references and threshold configuration."
        )
    else:
        st.caption("No CVs uploaded for this Job.")

    failures = failure_rows(items)
    if failures:
        st.subheader(f"Needs attention · {len(failures)} failed stages")
        st.dataframe(pd.DataFrame(failures[:20]), hide_index=True, width="stretch")
        st.caption("Showing up to 20 failures; open Pipeline Monitor for Job-specific details.")


def dashboard(ats):
    st.title("Dashboard")
    st.caption("Job-based recruitment overview · live data from the local ATS database")
    dashboard_live(ats)
    with st.expander("Job description readiness and configuration"):
        info = ats.dashboard.snapshot()
        columns = st.columns(4)
        for col, (label, value) in zip(columns, info["criteria_metrics"].items(), strict=True):
            col.metric(label, value)
        if info["unfinished_configurations"]:
            st.dataframe(pd.DataFrame(info["unfinished_configurations"]),
                         hide_index=True, width="stretch")
            for row in info["unfinished_configurations"]:
                st.button(f"Configure {row['ID']} · {row['Vacancy']}",
                          key=f"configure_{row['ID']}", on_click=_open_job,
                          args=(row["ID"],))
        else:
            st.caption("No unfinished job configurations.")


@st.fragment(run_every="4s")
def monitor_live(ats):
    vacancies = ats.vacancies.list()
    if not vacancies:
        st.info("No jobs available. Create a vacancy first.")
        return
    current = st.session_state.get("monitor_job")
    index = next((i for i, job in enumerate(vacancies) if job.id == current), 0)
    selected = st.selectbox("Job position", vacancies, index=index, format_func=_format_job,
                            key="monitor_job_choice")
    st.session_state.monitor_job = selected.id
    _, all_items = ats.pipeline.overview(selected.id)
    pulse = job_pulses([selected], all_items)[0]
    st.markdown(f"### {selected.title}")
    st.caption(f"Job ID {selected.id} · {selected.status} · {pulse.uploaded} uploaded CVs")
    st.progress(pulse.completed / pulse.uploaded if pulse.uploaded else 0,
                text=f"Text extraction: {pulse.progress}% ({pulse.completed}/{pulse.uploaded})")
    for col, (label, number) in zip(st.columns(4),
                                   [("Queued", pulse.queued), ("Running", pulse.running),
                                    ("Text complete", pulse.completed), ("Failed", pulse.failed)],
                                   strict=True):
        col.metric(label, number)
    st.subheader("Pipeline stages")
    st.dataframe(pd.DataFrame(stage_breakdown(all_items)), hide_index=True,
                 width="stretch")
    st.caption(
        "Embedding, profile extraction, evidence checks, matching and evidence-coverage "
        "scoring run when enabled. HR review remains a separate future phase."
    )
    st.subheader("Llama evidence scoring")
    st.caption(
        "A 0–100 weighted JD evidence score, not an automated hiring decision. "
        "Good / Medium / Weak labels are configurable evidence-coverage bands. "
        "Human review is required, especially for partial CV profiles."
    )
    with st.expander("Configure score bands", expanded=False):
        with st.form(f"score_bands_{selected.id}"):
            medium = st.number_input(
                "Medium starts at (%)", min_value=1, max_value=99,
                value=selected.medium_threshold, step=1,
            )
            good = st.number_input(
                "Good starts at (%)", min_value=2, max_value=100,
                value=selected.good_threshold, step=1,
            )
            apply_bands = st.form_submit_button("Save thresholds")
        if apply_bands:
            try:
                ats.vacancies.set_score_bands(selected.id, int(good), int(medium))
                st.success("Score thresholds saved for this Job.")
                st.rerun()
            except ATSError as exc:
                st.error(str(exc))
    baseline = {}
    for saved in ats.candidate_scoring.list_for_vacancy(selected.id):
        baseline.setdefault(saved.document_id, saved)
    score_rows = []
    for cv in all_items:
        outcome = ats.llama_scoring.current(cv["CV"])
        llama_done = outcome is not None and outcome.status == "COMPLETED"
        previous = baseline.get(cv["CV"])
        points = outcome.score if llama_done else (
            previous.documented_weight if previous is not None else None
        )
        review = outcome.review_weight if llama_done else (
            previous.review_weight if previous is not None else None
        )
        score_rows.append({
            "CV ID": cv["CV"],
            "Filename": cv["Filename"],
            "Score / 100": points,
            "Classification": evidence_classification(
                points, review, selected.good_threshold, selected.medium_threshold
            ),
            "Method": (
                "Llama + verified evidence" if llama_done
                else "Rule-based evidence" if previous is not None else "Not scored"
            ),
            "Review required (%)": review,
            "Llama status": outcome.status if outcome else "QUEUED",
            "Model": outcome.model_name if outcome else
            ats.llama_scoring.provider.settings.model,
            "Error": outcome.error_summary if outcome else None,
        })
    if score_rows:
        st.dataframe(pd.DataFrame(score_rows), hide_index=True, width="stretch")
        st.caption(
            "A zero documented score with 100% review required is not a Weak CV. "
            "It means none of the job criteria have confirmed evidence yet. "
            "Open the CV details to inspect the JD findings and source quotes."
        )
    else:
        st.info("No CVs uploaded. Upload CVs to start automatic scoring.")
    st.subheader("CV monitor")
    filter_col, search_col = st.columns([1, 2])
    with filter_col:
        state = st.selectbox("Filter by status", ["All", "Queued", "Running", "Completed",
                                                   "Failed", "Not active yet"], key="monitor_state")
    with search_col:
        query = st.text_input("Search by CV ID, application ID or filename", key="monitor_query")
    rows = document_rows(all_items, query, state)
    st.caption(f"Showing {len(rows)} of {len(all_items)} CVs for this Job")
    if rows:
        with st.expander("Delete selected CVs", expanded=False):
            options = {row["CV ID"]: row["Filename"] for row in rows}
            chosen = st.multiselect(
                "Select CV records to delete",
                list(options),
                format_func=lambda doc_id: f"CV {doc_id} · {options[doc_id]}",
                key=f"delete_cv_ids_{selected.id}",
            )
            confirm = st.checkbox(
                f"I confirm permanent deletion of {len(chosen)} selected CV(s) "
                "and their extracted results.",
                key=f"confirm_cv_delete_{selected.id}",
            )
            if st.button(
                "Delete selected CVs", type="primary",
                disabled=not chosen or not confirm,
                key=f"delete_cv_action_{selected.id}",
            ):
                try:
                    count = ats.deletion.delete_documents(chosen, selected.id)
                    st.session_state.pop(f"delete_cv_ids_{selected.id}", None)
                    st.session_state.pop(f"confirm_cv_delete_{selected.id}", None)
                    st.success(f"Deleted {count} selected CV(s).")
                    st.rerun()
                except ATSError as exc:
                    st.error(str(exc))
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     height=min(640, max(210, 36 * (len(rows) + 1))))
        selected_id = st.selectbox("Inspect an individual CV", [r["CV ID"] for r in rows],
                                   format_func=lambda n: next(
                                       f"CV {n} · {r['Filename']}" for r in rows if r["CV ID"] == n
                                   ), key="monitor_cv")
        detail = next(r for r in all_items if r["CV"] == selected_id)
        # Show persisted results for this CV; never infer success from stage flags alone.
        with st.expander("Stored CV profile", expanded=True):
            profiles = ats.profiles.list(document_id=selected_id)
            if profiles:
                profile = profiles[0]
                st.caption(
                    f"Profile ID {profile.id} · Review status: "
                    f"{ats.profiles.status(profile)} · Read from the database"
                )
                st.json(ats.profiles.effective(profile))
            else:
                st.warning("No stored candidate profile found for this CV.")

        with st.expander("Verified criteria matching", expanded=True):
            matches = [
                match for match in ats.job_matching.list_for_vacancy(selected.id)
                if match.document_id == selected_id
            ]
            if matches:
                match = matches[0]
                st.caption(
                    f"Match ID {match.id} · Matches are evidence checks, "
                    "not hiring suitability scores."
                )
                st.dataframe(
                    pd.DataFrame([
                        {
                            "Criterion": item.get("name"),
                            "Evidence status": item.get("status"),
                            "Evidence references": len(item.get("evidence") or []),
                        }
                        for item in match.criteria_results
                    ]),
                    hide_index=True, width="stretch",
                )
                with st.expander("Full stored matching evidence"):
                    st.json(match.criteria_results)
            else:
                st.info(
                    "No approved-criteria match is stored for this CV. "
                    "A completed Matching stage may represent semantic similarity only."
                )

        with st.expander("Stored evidence-coverage scoring", expanded=True):
            scores = [
                score for score in ats.candidate_scoring.list_for_vacancy(selected.id)
                if score.document_id == selected_id
            ]
            if scores:
                score = scores[0]
                st.caption(
                    f"Score snapshot {score.id} · This measures documented evidence "
                    "coverage, not candidate suitability."
                )
                st.write({
                    "Documented weight": score.documented_weight,
                    "Not evidenced weight": score.unverified_weight,
                    "Review required weight": score.review_weight,
                })
                st.dataframe(pd.DataFrame(score.criterion_breakdown),
                             hide_index=True, width="stretch")
            elif not ats.job_matching.ready(selected.id):
                st.info(
                    "Scoring is waiting for activated JD criteria for the current "
                    "job description. Choose default or manual weights in Job Management."
                )
            else:
                st.info(
                    "No scoring result is stored for this CV yet. "
                    "Inspect the Scoring stage below for status and errors."
                )

        with st.expander("Llama matching & evidence score", expanded=True):
            outcome = ats.llama_scoring.current(selected_id)
            if outcome is None:
                st.info(
                    "Local Llama has not scored this CV yet. Check the matching/scoring "
                    "stages and ensure the configured Ollama scoring model is installed."
                )
            elif outcome.status == "FAILED":
                st.error(outcome.error_summary or "Local Llama evaluation failed.")
                if st.button("Retry Llama evaluation", key=f"retry_llama_{selected_id}"):
                    try:
                        ats.llama_scoring.retry(selected_id)
                        st.rerun()
                    except ATSError as exc:
                        st.error(str(exc))
            elif outcome.status == "RUNNING":
                st.info("Local Llama evaluation is running.")
            else:
                st.metric("JD evidence score", f"{outcome.score}/100")
                st.write(
                    "Classification:",
                    evidence_classification(
                        outcome.score, outcome.review_weight,
                        selected.good_threshold, selected.medium_threshold
                    ),
                )
                st.caption(
                    f"Model: {outcome.model_name} · "
                    f"Needs review: {outcome.review_weight}% of JD criteria weight. "
                    "Results are evidence matches for human review, not hiring verdicts."
                )
                st.dataframe(
                    pd.DataFrame(outcome.breakdown), hide_index=True, width="stretch"
                )
                with st.expander("Source-backed Llama reasoning"):
                    st.json(outcome.breakdown)

        # Similarity is JD-to-CV text retrieval, not evidence of a qualification
        # or an automated hiring/ranking score. No LLM calls in this view.
        semantic = ats.semantic_matching.current_result(selected_id)
        if semantic is not None:
            with st.expander("JD ↔ CV semantic passages (review only)", expanded=True):
                st.caption(
                    "Stored embeddings only. Cosine similarity measures text relevance, "
                    "NOT candidate suitability, qualification proof or a hiring score."
                )
                passages = []
                for item in semantic.comparisons:
                    for hit in item["cv_passages"]:
                        passages.append({
                            "JD passage": item["jd_excerpt"][:250],
                            "JD source": item["jd_reference"],
                            "CV passage": hit["source_excerpt"][:350],
                            "CV source": hit["source_reference"],
                            "Cosine similarity": hit["cosine_similarity"],
                        })
                if passages:
                    st.dataframe(pd.DataFrame(passages), hide_index=True, width="stretch")
                st.caption(
                    "Every CV passage remains unverified as a qualification; "
                    "consult the original PDF and HR-reviewed criteria."
                )
        with st.container(border=True):
            stage_name, stage_status = current_stage(detail)
            st.markdown(f"**CV {selected_id} · {detail['Filename']}**")
            st.caption(f"Application {detail['Application']} · {stage_name}: {stage_status}")
            for stage in STAGES:
                status = detail.get(stage, "WAITING")
                title = f"{LABELS[stage]} · {status.replace('_', ' ').title()}"
                with st.expander(title, expanded=status in ("RUNNING", "FAILED")):
                    duration = detail.get(f"{stage}_duration_ms")
                    st.write(f"Attempts: {detail.get(f'{stage}_attempts', 0)}")
                    st.write(f"Duration: {round(duration / 1000, 2)} s" if duration is not None
                             else "Duration: not available")
                    if message := detail.get(f"{stage}_error"):
                        if status == "FAILED":
                            st.error(message)
                        else:
                            st.warning(message)
                    elif status == "WAITING":
                        st.caption("Not running in the current application phase.")
    else:
        st.info("No CVs match these filters." if all_items else
                "No CVs uploaded for this Job yet. Upload CVs in Candidate Management.")
    failures = failure_rows(all_items)
    if failures:
        with st.expander(f"Failure log · {len(failures)}", expanded=False):
            st.dataframe(pd.DataFrame(failures), hide_index=True, width="stretch")


def pipeline_monitor(ats):
    st.title("Pipeline Monitor")
    st.caption("Per-Job, per-CV live stage tracking · automatically refreshed every four seconds")
    monitor_live(ats)
