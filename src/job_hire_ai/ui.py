import io

import pandas as pd
import pymupdf
import streamlit as st
from docx import Document
from pydantic import ValidationError

from job_hire_ai.ai_ui import render_ai_requirements
from job_hire_ai.errors import ATSError
from job_hire_ai.schemas import VacancyInput


def vacancy_label(v):
    return f"{v.id} · {v.title} ({v.status})"


def notify(message):
    st.session_state.notice = message
    st.rerun()


def dashboard(ats):
    st.title("Dashboard")
    st.caption("Recruitment activity from your local database.")
    vacancies = ats.vacancies.list()
    selected = st.selectbox(
        "Vacancy filter",
        [None, *vacancies],
        format_func=lambda v: "All vacancies" if v is None else vacancy_label(v),
    )
    data = ats.dashboard.snapshot(selected.id if selected else None)
    for column, (name, count) in zip(st.columns(5), data["metrics"].items(), strict=True):
        column.metric(name, count)
    st.caption("Applications and CVs count current uploads. Candidate records include history.")
    cv_data = ats.cv_analysis.dashboard(selected.id if selected else None)
    st.subheader("CV analysis activity")
    for column, (name, count) in zip(st.columns(6), cv_data["metrics"].items(), strict=True):
        column.metric(name, count)
    st.caption(
        "Failed CVs use the latest job; older profiles can still exist. Average duration excludes "
        "cache reuse. Missing information is not a confirmed lack of qualification."
    )
    if cv_data["vacancies"]:
        st.dataframe(pd.DataFrame(cv_data["vacancies"]), hide_index=True, width="stretch")
    _pipeline_live(ats, selected.id if selected else None)
    st.subheader("Job requirements readiness")
    for column, (name, count) in zip(st.columns(4), data["criteria_metrics"].items(), strict=True):
        column.metric(name, count)
    st.caption(
        "Approved counts require a matching job description. Draft and outdated counts may overlap."
    )
    if data["unfinished_configurations"]:
        with st.expander("Unfinished job configurations"):
            st.dataframe(pd.DataFrame(data["unfinished_configurations"]), hide_index=True)
            for row in data["unfinished_configurations"]:
                st.button(
                    f"Configure {row['ID']} · {row['Vacancy']}",
                    key=f"configure_{row['ID']}",
                    on_click=_open_job,
                    args=(row["ID"],),
                )
    if not vacancies:
        st.info("No vacancies yet. Open Job Management to create your first vacancy.")
    st.subheader("Applications per vacancy")
    if data["counts"]:
        frame = pd.DataFrame(data["counts"])
        st.bar_chart(frame.set_index("Vacancy"))
        st.dataframe(frame, hide_index=True, width="stretch")
    else:
        st.info("No vacancy activity to show.")
    for label, rows in [
        ("Recent vacancies", data["recent_vacancies"]),
        ("Recent CV uploads", data["recent_uploads"]),
    ]:
        st.subheader(label)
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.info("No records yet.")


@st.fragment(run_every="4s")
def _pipeline_live(ats, vacancy_id=None):
    summary, items = ats.pipeline.overview(vacancy_id)
    st.subheader("Automatic CV pipeline — live")
    st.caption("Upload starts text extraction automatically. Later AI, matching, scoring and HR stages are not active in Phase 1.")
    if summary:
        st.dataframe(pd.DataFrame(summary), hide_index=True, width="stretch")
        if items:
            cols = ["Job", "Application", "CV", "Filename", "TEXT_EXTRACTION", "Current stage", "TEXT_EXTRACTION_error"]
            st.dataframe(pd.DataFrame(items)[cols], hide_index=True, width="stretch")
    else:
        st.info("No CV pipeline jobs for this selection yet.")


def vacancy_fields(prefix, vacancy=None):
    title = st.text_input("Job title *", value=vacancy.title if vacancy else "", key=f"{prefix}_t")
    department = st.text_input(
        "Department (optional)",
        value=(vacancy.department or "") if vacancy else "",
        key=f"{prefix}_d",
    )
    description = st.text_area(
        "Job description *",
        value=vacancy.description if vacancy else "",
        height=180,
        key=f"{prefix}_desc",
    )
    return title, department, description


def _open_job(vacancy_id):
    st.session_state.navigation = "Job Management"
    st.session_state.open_vacancy_id = vacancy_id
    st.session_state.job_status_filter = "All"


def validated_vacancy(title, department, description):
    try:
        return VacancyInput(title=title, department=department or None, description=description)
    except ValidationError:
        raise ATSError(
            "Enter a title (1–200 characters) and a description (1–100,000 characters)."
        ) from None


def jobs(ats):
    st.title("Job Management")
    with st.expander("Create vacancy", expanded=not ats.vacancies.list()):
        with st.form("create_vacancy", clear_on_submit=True):
            title, department, description = vacancy_fields("new")
            submitted = st.form_submit_button("Create vacancy", type="primary")
        if submitted:
            try:
                ats.vacancies.create(validated_vacancy(title, department, description))
                notify("Vacancy created.")
            except ATSError as exc:
                st.error(str(exc))
    status = st.selectbox("Status filter", ["All", "Active", "Archived"], key="job_status_filter")
    vacancies = ats.vacancies.list(None if status == "All" else status)
    if not vacancies:
        st.info("No vacancies match this filter.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "ID": v.id,
                    "Title": v.title,
                    "Department": v.department,
                    "Status": v.status,
                    "Created": v.created_at,
                    "Updated": v.updated_at,
                }
                for v in vacancies
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    requested = st.session_state.get("open_vacancy_id")
    initial = next((index for index, v in enumerate(vacancies) if v.id == requested), 0)
    vacancy = st.selectbox(
        "View / edit vacancy",
        vacancies,
        format_func=vacancy_label,
        index=initial,
        key=f"selected_job_{status}_{requested}",
    )
    st.subheader(vacancy.title)
    st.text(vacancy.description)
    st.caption(f"Status: {vacancy.status} · ID: {vacancy.id}")
    with st.form(f"edit_{vacancy.id}"):
        title, department, description = vacancy_fields(f"edit_{vacancy.id}", vacancy)
        saved = st.form_submit_button("Save changes", type="primary")
    if saved:
        try:
            ats.vacancies.edit(vacancy.id, validated_vacancy(title, department, description))
            notify("Vacancy updated.")
        except ATSError as exc:
            st.error(str(exc))
    target = "Archived" if vacancy.status == "Active" else "Active"
    if st.button("Archive vacancy" if target == "Archived" else "Reactivate vacancy"):
        try:
            ats.vacancies.set_status(vacancy.id, target)
            notify(f"Vacancy is now {target.lower()}. Historical applications are retained.")
        except ATSError as exc:
            st.error(str(exc))
    render_ai_requirements(ats, vacancy)


def candidates(ats):
    st.title("Candidate Management")
    vacancies = ats.vacancies.list()
    if not vacancies:
        st.info("Create a vacancy in Job Management before uploading CVs.")
        return
    vacancy = st.selectbox("Select vacancy", vacancies, format_func=vacancy_label)
    if vacancy.status == "Active":
        _upload(ats, vacancy)
    else:
        st.info(
            "Archived vacancy: historical applications are available below. Reactivate to upload."
        )
    _pipeline_live(ats, vacancy.id)
    _applications(ats, vacancy)


def _upload(ats, vacancy):
    st.subheader("Upload CVs")
    st.caption(
        f"PDF or DOCX · up to 100 files · {ats.storage.max_bytes // 1024**2} MB per file. "
        "Personal details are left unknown; no AI or OCR runs."
    )
    existing = ats.candidates.list()
    chosen = st.selectbox(
        "Candidate association",
        [None, *existing],
        format_func=lambda c: (
            "Create a separate candidate for each file" if c is None else c.reference
        ),
    )
    st.caption("Select an existing reference only when you know the CVs belong to that applicant.")
    generation = st.session_state.get("upload_generation", 0)
    files = st.file_uploader(
        "Choose CV files",
        type=["pdf", "docx"],
        accept_multiple_files=True,
        key=f"files_{vacancy.id}_{generation}",
    )
    prepared, preview = [], []
    for file in files:
        data = file.getvalue()
        prepared.append((file.name, data))
        try:
            _, text_status = ats.documents.validator.validate(file.name, data)
            verdict = f"Valid · text {text_status.lower()}"
        except ATSError as exc:
            verdict = str(exc)
        preview.append({"Filename": file.name, "Bytes": len(data), "Validation": verdict})
    if preview:
        st.dataframe(pd.DataFrame(preview), hide_index=True, width="stretch")
    if len(files) > 100:
        st.error("Select at most 100 files per batch.")
    if st.button("Upload batch", type="primary", disabled=not files or len(files) > 100):
        with st.spinner("Validating and storing CVs…"):
            results = ats.documents.upload_batch(
                vacancy.id, prepared, chosen.id if chosen else None
            )
        st.session_state.upload_results = [r.model_dump() for r in results]
        ats.pipeline.ensure_worker()
        st.session_state.upload_generation = generation + 1
        st.rerun()
    if results := st.session_state.get("upload_results"):
        success = sum(r["success"] for r in results)
        st.info(
            f"Submitted: {len(results)} · Uploaded: {success} · Rejected: {len(results) - success}"
        )
        st.dataframe(pd.DataFrame(results), hide_index=True, width="stretch")


def _applications(ats, vacancy):
    st.subheader("Applications")
    search = st.text_input("Search candidate reference, application ID or filename").strip().lower()
    status = st.selectbox("Application status", ["Uploaded", "Removed", "All"])
    applications = [
        a
        for a in ats.applications.list(vacancy.id)
        if (status == "All" or a.status == status)
        and (
            not search
            or search
            in f"{a.id} {a.candidate.reference} "
            f"{' '.join(d.original_filename for d in a.documents)}".lower()
        )
    ]
    if not applications:
        st.info("No applications match your filters.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Application": a.id,
                    "Candidate": a.candidate.reference,
                    "Name": a.candidate.display_name,
                    "Email": a.candidate.email,
                    "Status": a.status,
                    "CVs": len(a.documents),
                    "Created": a.created_at,
                }
                for a in applications
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    application = st.selectbox(
        "View application",
        applications,
        format_func=lambda a: f"Application {a.id} · {a.candidate.reference}",
    )
    for document in application.documents:
        with st.expander(document.original_filename, expanded=True):
            st.write(
                {
                    "Document ID": document.id,
                    "Type": document.file_type.upper(),
                    "Bytes": document.file_size,
                    "SHA-256": document.checksum,
                    "Uploaded": str(document.upload_timestamp),
                    "Text extraction": document.text_extraction_status,
                }
            )
            if document.text_extraction_status == "Unavailable":
                st.warning("No extractable text. This document may be scanned; OCR is unavailable.")
            if application.status == "Uploaded":
                try:
                    filename, data, kind = ats.documents.download(document.id)
                    mime = (
                        "application/pdf"
                        if kind == "pdf"
                        else (
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                        )
                    )
                    st.download_button(
                        "Download original CV",
                        data,
                        filename,
                        mime=mime,
                        key=f"download_{document.id}",
                    )
                    if st.checkbox("Preview CV", key=f"preview_{document.id}"):
                        _preview(data, kind, document.id)
                except ATSError as exc:
                    st.error(str(exc))
    if application.status == "Removed" and not application.documents:
        st.caption("Application removed. Its files and document metadata have been deleted.")
        return
    confirmed = st.checkbox(
        "I confirm permanent deletion of all CV files for this application.",
        key=f"confirm_{application.id}",
    )
    if st.button(
        "Remove application" if application.status == "Uploaded" else "Retry file deletion",
        disabled=not confirmed,
    ):
        try:
            ats.applications.remove(application.id)
            notify("Application removed and associated CV files deleted.")
        except ATSError as exc:
            st.error(str(exc))


def _preview(data, kind, document_id):
    if kind == "pdf":
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            number = st.number_input(
                "Preview page",
                min_value=1,
                max_value=pdf.page_count,
                value=1,
                step=1,
                key=f"page_{document_id}",
            )
            page = pdf[int(number) - 1]
            scale = min(1.5, 1400 / max(page.rect.width, page.rect.height, 1))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            st.image(pixmap.tobytes("png"))
    else:
        st.caption("DOCX text preview. Download the original for full layout and images.")
        document = Document(io.BytesIO(data))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        table_text = "\n".join(
            " | ".join(cell.text for cell in row.cells)
            for table in document.tables
            for row in table.rows
        )
        st.text((text + "\n" + table_text).strip() or "No text available in this document.")
