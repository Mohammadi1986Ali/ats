"""Job-first dashboard and live pipeline monitor backed only by persisted ATS records."""

import pandas as pd
import streamlit as st

from job_hire_ai.dashboard_view import (
    STAGES,
    LABELS,
    aggregate,
    current_stage,
    document_rows,
    failure_rows,
    job_pulses,
    stage_breakdown,
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
        "Text complete counts only text extraction. AI matching, scoring and HR review "
        "are not yet running."
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
    st.caption("Stages after text extraction are placeholders until later phases are implemented.")
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
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     height=min(640, max(210, 36 * (len(rows) + 1))))
        selected_id = st.selectbox("Inspect an individual CV", [r["CV ID"] for r in rows],
                                   format_func=lambda n: next(
                                       f"CV {n} · {r['Filename']}" for r in rows if r["CV ID"] == n
                                   ), key="monitor_cv")
        detail = next(r for r in all_items if r["CV"] == selected_id)
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
                        st.error(message)
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
