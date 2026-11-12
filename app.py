import logging
from importlib.metadata import version

import streamlit as st
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.exc import SQLAlchemyError

from job_hire_ai.cv_ui import cv_analysis_page
from job_hire_ai.dashboard_ui import pipeline_monitor
from job_hire_ai.errors import ATSError
from job_hire_ai.runtime import Runtime
from job_hire_ai.settings import ROOT, Settings
from job_hire_ai.ui import candidates, dashboard, jobs

st.set_page_config(page_title="Job Hire AI", page_icon="📁", layout="wide")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


@st.cache_resource
def runtime(app_version):
    return Runtime(Settings())


ats = runtime(version("job-hire-ai"))
try:
    with ats.db.engine.connect() as connection:
        revision = MigrationContext.configure(connection).get_current_revision()
    config = Config(str(ROOT / "alembic.ini"))
    if revision != ScriptDirectory.from_config(config).get_current_head():
        st.error("Initialize/update the database: uv run alembic upgrade head")
        st.stop()
except SQLAlchemyError:
    st.error("The local database could not be opened. Check your data directory permissions.")
    st.stop()

if not st.session_state.get("pipeline_worker_started"):
    try:
        ats.pipeline.ensure_worker()
        st.session_state.pipeline_worker_started = True
    except ATSError as exc:
        st.warning(str(exc))

st.sidebar.title("Job Hire AI")
st.sidebar.caption("Applicant tracking · Job and CV analysis")
page = st.sidebar.radio(
    "Navigation",
    ["Dashboard", "Pipeline Monitor", "Job Management", "Candidate Management", "CV Analysis"],
    key="navigation",
)
st.sidebar.caption("Local prototype · no authentication. Use on a trusted computer.")
if message := st.session_state.pop("notice", None):
    st.success(message)
try:
    {
        "Dashboard": dashboard,
        "Pipeline Monitor": pipeline_monitor,
        "Job Management": jobs,
        "Candidate Management": candidates,
        "CV Analysis": cv_analysis_page,
    }[page](ats)
except SQLAlchemyError:
    st.error("The database operation failed. Refresh and retry; check local database availability.")
except OSError:
    st.error("File storage is unavailable. Check free space and local directory permissions.")
except ATSError as exc:
    st.error(str(exc))
