from job_hire_ai.ai.job_analysis import JobAnalysisService
from job_hire_ai.ai.ollama_provider import OllamaProvider
from job_hire_ai.ai.settings import OllamaSettings
from job_hire_ai.database.session import Database
from job_hire_ai.services.application_service import ApplicationService
from job_hire_ai.services.candidate_profile_service import CandidateProfileService
from job_hire_ai.services.candidate_service import CandidateService
from job_hire_ai.services.criteria_service import CriteriaService
from job_hire_ai.services.cv_analysis_service import CVAnalysisService
from job_hire_ai.services.dashboard_service import DashboardService
from job_hire_ai.services.document_service import DocumentService
from job_hire_ai.services.pipeline_service import PipelineService
from job_hire_ai.services.vacancy_service import VacancyService
from job_hire_ai.settings import Settings
from job_hire_ai.storage import LocalStorage


class Runtime:
    def __init__(self, settings: Settings):
        settings.directory.mkdir(parents=True, exist_ok=True)
        self.db = Database(settings.database_url)
        self.storage = LocalStorage(settings.directory / "uploads", settings.max_file_mb * 1024**2)
        self.vacancies = VacancyService(self.db)
        self.candidates = CandidateService(self.db)
        self.applications = ApplicationService(self.db, self.storage)
        self.documents = DocumentService(self.db, self.storage)
        self.pipeline = PipelineService(self.db, self.documents, settings.directory)
        self.dashboard = DashboardService(self.db)
        self.criteria = CriteriaService(self.db)
        self.ollama_settings = OllamaSettings()
        self.provider = OllamaProvider(self.ollama_settings)
        self.job_analysis = JobAnalysisService(self.db, self.provider, self.ollama_settings)
        self.profiles = CandidateProfileService(self.db)
        self.cv_analysis = CVAnalysisService(
            self.db,
            self.documents,
            self.provider,
            self.ollama_settings,
            settings.directory / "cv-worker.lock",
        )
