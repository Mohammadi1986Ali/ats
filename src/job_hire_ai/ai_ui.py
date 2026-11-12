import math
import time
from decimal import Decimal

import pandas as pd
import streamlit as st
from pydantic import ValidationError
from sqlalchemy.orm.exc import StaleDataError

from job_hire_ai.ai.schemas import Category, CriterionInput, RequirementType, verify_evidence
from job_hire_ai.errors import ATSError
from job_hire_ai.services.criteria_service import criterion_input, description_hash


def _notice(message):
    st.session_state.notice = message
    st.rerun()


def render_ai_requirements(ats, vacancy):
    st.divider()
    st.header("AI Job Requirements")
    st.caption(
        "Llama proposes job requirements only. HR reviews evidence, sets weights and approves. "
        "Candidate screening and scoring are not implemented."
    )
    st.write(f"Current model: {ats.ollama_settings.model}")
    cache_key = f"model_status_{ats.ollama_settings.base_url}_{ats.ollama_settings.model}"
    refresh = st.button("Check Ollama connection", key=f"check_model_{vacancy.id}")
    cached = st.session_state.get(cache_key)
    if refresh or not cached or time.monotonic() - cached[0] > 15:
        cached = (time.monotonic(), ats.provider.check())
        st.session_state[cache_key] = cached
    model_status = cached[1]
    if model_status.available:
        st.success(model_status.message)
    elif model_status.connected:
        st.warning(f"Ollama connected · {model_status.message}")
    else:
        st.warning(model_status.message)

    history = ats.criteria.history(vacancy.id)
    draft = next((c for c in history if c.status == "DRAFT"), None)
    approved = next((c for c in history if c.status == "APPROVED"), None)
    outdated = bool(
        approved and approved.job_description_hash != description_hash(vacancy.description)
    )
    runs = ats.job_analysis.runs(vacancy.id)
    state = (
        "Outdated"
        if outdated
        else "Draft available"
        if draft
        else "Approved"
        if approved
        else (
            "Failed"
            if runs and runs[0].status == "FAILED"
            else "Analyzing"
            if runs and runs[0].status == "RUNNING"
            else "Not analyzed"
        )
    )
    st.write(f"Analysis status: **{state}**")
    st.write(f"Approved criteria version: **{approved.version if approved else 'None'}**")
    if outdated:
        st.warning(
            "The approved configuration is based on an older job description. "
            "It is not ready for future screening. Re-analyze and approve a new version."
        )
    replace = (
        st.checkbox(
            "Replace the saved draft with a new analysis (unsaved edits will be lost)",
            key=f"replace_draft_{vacancy.id}",
        )
        if draft
        else False
    )
    if st.button(
        "Analyze Job Description",
        disabled=not model_status.available or bool(draft and not replace),
        key=f"analyze_{vacancy.id}",
        type="primary",
    ):
        try:
            with st.spinner("Analyzing job description with local Llama…"):
                ats.job_analysis.analyze(vacancy.id, replace_draft=replace)
            _notice("Analysis complete. Review the draft, assign weights, then explicitly approve.")
        except ATSError as exc:
            st.error(str(exc))
    if runs and runs[0].status == "FAILED":
        st.error(f"Last analysis failed: {runs[0].error_summary}")
    if not draft and st.button("Create New Draft", key=f"new_draft_{vacancy.id}"):
        try:
            ats.criteria.create_draft(vacancy.id)
            _notice("Draft created. The approved version remains unchanged.")
        except ATSError as exc:
            st.error(str(exc))
    if approved:
        with st.expander(
            f"Current approved criteria · version {approved.version}", expanded=not draft
        ):
            _read_only(approved)
    if draft:
        _editor(ats, vacancy, draft)
    with st.expander("View Version History"):
        if not history:
            st.info("No criteria versions yet.")
        else:
            chosen = st.selectbox(
                "Configuration version",
                history,
                format_func=lambda c: f"Version {c.version} · {c.status}",
                key=f"history_{vacancy.id}",
            )
            _read_only(chosen)
    with st.expander("Analysis run history"):
        if not runs:
            st.info("No model runs yet.")
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Run": r.id,
                            "Model": r.model_name,
                            "Digest": r.model_digest,
                            "Prompt": r.prompt_version,
                            "Input SHA-256": r.input_description_hash,
                            "Started (UTC)": r.started_at,
                            "Completed (UTC)": r.completed_at,
                            "Duration (ms)": r.duration_ms,
                            "Status": r.status,
                            "Error": r.error_summary,
                        }
                        for r in runs
                    ]
                ),
                hide_index=True,
            )


def _read_only(configuration):
    st.caption(
        f"{configuration.status} · created {configuration.created_at} UTC · "
        f"approved {configuration.approved_at or '—'} · "
        f"reviewer {configuration.approved_by or 'not recorded'}"
    )
    st.code(configuration.job_description_hash, language=None)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    **criterion_input(c).model_dump(mode="json"),
                    "origin": c.created_by,
                    "evidence_status": c.evidence_status,
                }
                for c in configuration.criteria
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    with st.expander("Job description snapshot for this version"):
        st.text(configuration.job_description_snapshot)


def _clean_records(frame):
    records = frame.to_dict("records") if isinstance(frame, pd.DataFrame) else list(frame)
    cleaned = []
    for record in records:
        value = dict(record)
        for key, item in value.items():
            if item is None or isinstance(item, float) and math.isnan(item):
                value[key] = None if key in {"weight", "criterion_id"} else ""
        for key in ("weight", "display_order"):
            item = value.get(key)
            if isinstance(item, float) and item.is_integer():
                value[key] = int(item)
        value["evidence_acknowledged"] = value.get("evidence_acknowledged") is True
        cleaned.append(CriterionInput.model_validate(value).model_dump(mode="json"))
    return cleaned


def _editor(ats, vacancy, draft):
    st.subheader(f"Draft criteria · version {draft.version}")
    st.caption(
        "Add/delete rows using the table controls. Set unique display_order values to reorder. "
        "Weights are whole percentage points; zero is allowed, blanks are unassigned. "
        "Save Draft before approving. Approval uses the saved version, never unsaved table edits."
    )
    columns = list(CriterionInput.model_fields)
    records = [criterion_input(c).model_dump(mode="json") for c in draft.criteria]
    frame = pd.DataFrame(records, columns=columns)
    frame["weight"] = pd.array(frame["weight"], dtype="Int64")
    frame["display_order"] = pd.array(frame["display_order"], dtype="Int64")
    frame["evidence_acknowledged"] = pd.array(frame["evidence_acknowledged"], dtype="boolean")
    edited = st.data_editor(
        frame,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key=f"criteria_editor_{draft.id}_{draft.revision}",
        disabled=["criterion_id"],
        column_config={
            "criterion_id": None,
            "category": st.column_config.SelectboxColumn(options=[c.value for c in Category]),
            "requirement_type": st.column_config.SelectboxColumn(
                options=[t.value for t in RequirementType]
            ),
            "weight": st.column_config.NumberColumn(min_value=0, max_value=100, step=1),
            "display_order": st.column_config.NumberColumn(min_value=1, max_value=50, step=1),
            "source_excerpt": st.column_config.TextColumn(width="large"),
            "evidence_acknowledged": st.column_config.CheckboxColumn(default=False),
        },
    )
    total = sum(
        (
            Decimal(str(value))
            for value in edited["weight"]
            if value is not None and not pd.isna(value)
        ),
        Decimal(0),
    )
    st.metric("Draft weight total", f"{total:g}%")
    if total != 100:
        st.warning("Assign weights totaling exactly 100% before approval.")
    st.caption(
        "Source verification checks exact quotations; it does not prove the criterion's meaning."
    )
    for index, row in enumerate(edited.to_dict("records"), 1):
        excerpt = str(row.get("source_excerpt") or "")
        evidence = verify_evidence(excerpt, draft.job_description_snapshot)
        st.caption(f"Row {index} evidence: {evidence}")
    if records:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Name": c.name,
                        "Origin": c.created_by,
                        "Evidence": c.evidence_status,
                        "Uncertainty": c.uncertainty,
                    }
                    for c in draft.criteria
                ]
            ),
            hide_index=True,
        )
    if st.button("Save Draft", key=f"save_draft_{draft.id}"):
        try:
            ats.criteria.save_draft(draft.id, _clean_records(edited), draft.revision)
            _notice("Draft saved. Review saved evidence flags and approval validation below.")
        except ValidationError:
            st.error(
                "Complete name, category, description, requirement type and display order for each "
                "row. Use whole-number nonnegative weights from 0 to 100."
            )
        except (ATSError, StaleDataError) as exc:
            st.error(str(exc) if isinstance(exc, ATSError) else "Draft changed. Refresh and retry.")
    issues = ats.criteria.issues(draft, vacancy.description)
    for issue in issues:
        st.warning(f"Saved draft: {issue}")
    reviewer = st.text_input(
        "Reviewer identifier (optional)", max_chars=200, key=f"reviewer_{draft.id}"
    )
    acknowledge = st.checkbox(
        "I reviewed criteria, evidence and weights, and approve this saved configuration.",
        key=f"approve_confirmation_{draft.id}_{draft.revision}",
    )
    # Prevent approving old saved data when there are unsaved table edits.
    try:
        unsaved = _clean_records(edited) != records
    except ValidationError:
        unsaved = True
    if unsaved:
        st.info("Save your table changes before approving.")
    if st.button(
        "Approve Configuration",
        disabled=bool(issues) or not acknowledge or unsaved,
        key=f"approve_{draft.id}",
    ):
        try:
            version = ats.criteria.approve(draft.id, draft.revision, reviewer)
            _notice(
                f"Configuration version {version} approved. Earlier approvals remain in history."
            )
        except (ATSError, StaleDataError) as exc:
            st.error(str(exc) if isinstance(exc, ATSError) else "Draft changed. Refresh and retry.")
