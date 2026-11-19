"""Selective deletion of chosen ATS CVs or jobs; unrelated records survive."""

from sqlalchemy import delete, select

from job_hire_ai.database.models import (
    Candidate, CandidateDocument, CandidateProfile, CandidateScoreSnapshot,
    CriteriaConfiguration, CVAnalysisBatch, CVAnalysisJob, CVAnalysisRun,
    DocumentPipelineStage, DocumentTextExtraction, EmbeddingChunk,
    EvidenceReference, JobAnalysisRun, JobApplication, JobCriterion,
    JobMatchResult, ProfileReviewEvent, SemanticMatchResult, Vacancy,
)
from job_hire_ai.errors import ATSError


class SelectionDeletionService:
    def __init__(self, db, storage):
        self.db, self.storage = db, storage

    def delete_documents(self, document_ids: list[int], vacancy_id: int) -> int:
        ids = list(dict.fromkeys(document_ids))
        if not ids:
            raise ATSError("Select at least one CV.")
        with self.db.transaction() as session:
            docs = list(session.scalars(select(CandidateDocument).where(
                CandidateDocument.id.in_(ids),
                CandidateDocument.vacancy_id == vacancy_id,
            )))
            if len(docs) != len(ids):
                raise ATSError("Selected CVs changed. Refresh and select again.")
            if session.scalar(select(DocumentPipelineStage.id).where(
                DocumentPipelineStage.document_id.in_(ids),
                DocumentPipelineStage.status == "RUNNING",
            ).limit(1)):
                raise ATSError("A selected CV is processing. Stop ATS worker before deletion.")
            keys = [doc.storage_key for doc in docs]
            apps = {doc.application_id for doc in docs}
            self._delete_documents(session, ids)
            self._clean_orphans(session, apps)
        for key in keys:
            self.storage.delete(key)
        return len(ids)

    def delete_vacancies(self, vacancy_ids: list[int]) -> int:
        ids = list(dict.fromkeys(vacancy_ids))
        if not ids:
            raise ATSError("Select at least one job.")
        with self.db.transaction() as session:
            jobs = list(session.scalars(select(Vacancy).where(Vacancy.id.in_(ids))))
            if len(jobs) != len(ids):
                raise ATSError("Selected jobs changed. Refresh and select again.")
            docs = list(session.scalars(select(CandidateDocument).where(
                CandidateDocument.vacancy_id.in_(ids)
            )))
            doc_ids = [doc.id for doc in docs]
            keys = [doc.storage_key for doc in docs]
            if doc_ids and session.scalar(select(DocumentPipelineStage.id).where(
                DocumentPipelineStage.document_id.in_(doc_ids),
                DocumentPipelineStage.status == "RUNNING",
            ).limit(1)):
                raise ATSError("A selected job has processing CVs. Stop worker first.")
            candidate_ids = list(session.scalars(select(JobApplication.candidate_id).where(
                JobApplication.vacancy_id.in_(ids)
            )))
            app_ids = list(session.scalars(select(JobApplication.id).where(
                JobApplication.vacancy_id.in_(ids)
            )))
            if doc_ids:
                self._delete_documents(session, doc_ids)
            # Delete vacancy-level analysis after all dependent document snapshots.
            batch_ids = list(session.scalars(select(CVAnalysisBatch.id).where(
                CVAnalysisBatch.vacancy_id.in_(ids)
            )))
            if batch_ids:
                self._delete_batches(session, batch_ids)
            config_ids = list(session.scalars(select(CriteriaConfiguration.id).where(
                CriteriaConfiguration.vacancy_id.in_(ids)
            )))
            if config_ids:
                session.execute(delete(JobCriterion).where(
                    JobCriterion.configuration_id.in_(config_ids)
                ))
                session.execute(delete(CriteriaConfiguration).where(
                    CriteriaConfiguration.id.in_(config_ids)
                ))
            session.execute(delete(JobAnalysisRun).where(JobAnalysisRun.vacancy_id.in_(ids)))
            session.execute(delete(EmbeddingChunk).where(EmbeddingChunk.vacancy_id.in_(ids)))
            session.execute(delete(JobApplication).where(JobApplication.vacancy_id.in_(ids)))
            session.execute(delete(Vacancy).where(Vacancy.id.in_(ids)))
            self._clean_candidates(session, candidate_ids)
        for key in keys:
            self.storage.delete(key)
        return len(ids)

    @staticmethod
    def _delete_batches(session, batch_ids):
        job_ids = list(session.scalars(select(CVAnalysisJob.id).where(
            CVAnalysisJob.batch_id.in_(batch_ids)
        )))
        if job_ids:
            session.execute(delete(CVAnalysisRun).where(CVAnalysisRun.job_id.in_(job_ids)))
            session.execute(delete(CVAnalysisJob).where(CVAnalysisJob.id.in_(job_ids)))
        session.execute(delete(CVAnalysisBatch).where(CVAnalysisBatch.id.in_(batch_ids)))

    @staticmethod
    def _delete_documents(session, ids):
        profile_ids = list(session.scalars(select(CandidateProfile.id).where(
            CandidateProfile.document_id.in_(ids)
        )))
        analysis_job_ids = list(session.scalars(select(CVAnalysisJob.id).where(
            CVAnalysisJob.document_id.in_(ids)
        )))
        session.execute(delete(CandidateScoreSnapshot).where(
            CandidateScoreSnapshot.document_id.in_(ids)
        ))
        session.execute(delete(JobMatchResult).where(JobMatchResult.document_id.in_(ids)))
        session.execute(delete(SemanticMatchResult).where(
            SemanticMatchResult.document_id.in_(ids)
        ))
        if profile_ids:
            session.execute(delete(ProfileReviewEvent).where(
                ProfileReviewEvent.profile_id.in_(profile_ids)
            ))
            session.execute(delete(EvidenceReference).where(
                EvidenceReference.profile_id.in_(profile_ids)
            ))
            session.execute(delete(CandidateProfile).where(
                CandidateProfile.id.in_(profile_ids)
            ))
        if analysis_job_ids:
            session.execute(delete(CVAnalysisRun).where(
                CVAnalysisRun.job_id.in_(analysis_job_ids)
            ))
            session.execute(delete(CVAnalysisJob).where(
                CVAnalysisJob.id.in_(analysis_job_ids)
            ))
        session.execute(delete(DocumentPipelineStage).where(
            DocumentPipelineStage.document_id.in_(ids)
        ))
        session.execute(delete(EmbeddingChunk).where(EmbeddingChunk.document_id.in_(ids)))
        session.execute(delete(DocumentTextExtraction).where(
            DocumentTextExtraction.document_id.in_(ids)
        ))
        session.execute(delete(CandidateDocument).where(CandidateDocument.id.in_(ids)))
        # Existing jobs/batches may have no remaining CV analysis jobs.
        batches = list(session.scalars(select(CVAnalysisBatch.id).where(
            ~CVAnalysisBatch.id.in_(select(CVAnalysisJob.batch_id))
        )))
        if batches:
            session.execute(delete(CVAnalysisBatch).where(CVAnalysisBatch.id.in_(batches)))

    @classmethod
    def _clean_orphans(cls, session, app_ids):
        if not app_ids:
            return
        emptied = list(session.scalars(select(JobApplication.id).where(
            JobApplication.id.in_(app_ids),
            ~JobApplication.id.in_(select(CandidateDocument.application_id)),
        )))
        if emptied:
            candidates = list(session.scalars(select(JobApplication.candidate_id).where(
                JobApplication.id.in_(emptied)
            )))
            session.execute(delete(JobApplication).where(JobApplication.id.in_(emptied)))
            cls._clean_candidates(session, candidates)

    @staticmethod
    def _clean_candidates(session, candidates):
        if candidates:
            session.execute(delete(Candidate).where(
                Candidate.id.in_(candidates),
                ~Candidate.id.in_(select(JobApplication.candidate_id)),
            ))
