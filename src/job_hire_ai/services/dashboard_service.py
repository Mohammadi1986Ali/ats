from job_hire_ai.database.models import Candidate, CriteriaConfiguration
from job_hire_ai.database.repositories import Repository
from job_hire_ai.services.criteria_service import description_hash


class DashboardService:
    def __init__(self, db):
        self.db = db

    def snapshot(self, vacancy_id=None):
        from sqlalchemy import func, select

        with self.db.transaction() as session:
            vacancies = Repository.vacancies(session)
            if vacancy_id is not None:
                vacancies = [v for v in vacancies if v.id == vacancy_id]
            applications = Repository.applications(session, vacancy_id)
            current = [a for a in applications if a.status == "Uploaded"]
            if vacancy_id is None:
                candidates = session.scalar(select(func.count(Candidate.id)))
            else:
                candidates = len({a.candidate_id for a in applications})
            documents = [d for a in current for d in a.documents]
            configurations = list(session.scalars(select(CriteriaConfiguration)))
            approved, awaiting, outdated, unfinished = 0, 0, 0, []
            for vacancy in vacancies:
                versions = [c for c in configurations if c.vacancy_id == vacancy.id]
                current_config = next((c for c in versions if c.status == "APPROVED"), None)
                draft = next((c for c in versions if c.status == "DRAFT"), None)
                is_outdated = bool(
                    current_config
                    and current_config.job_description_hash != description_hash(vacancy.description)
                )
                approved += bool(current_config and not is_outdated)
                awaiting += bool(draft)
                outdated += is_outdated
                if not current_config or draft or is_outdated:
                    unfinished.append(
                        {
                            "ID": vacancy.id,
                            "Vacancy": vacancy.title,
                            "Criteria status": "Outdated"
                            if is_outdated
                            else "Draft available"
                            if draft
                            else "Not analyzed",
                        }
                    )
            return {
                "criteria_metrics": {
                    "Total vacancies": len(vacancies),
                    "Approved AI criteria": approved,
                    "Awaiting HR approval": awaiting,
                    "Outdated criteria": outdated,
                },
                "unfinished_configurations": unfinished,
                "metrics": {
                    "Active vacancies": sum(v.status == "Active" for v in vacancies),
                    "Archived vacancies": sum(v.status == "Archived" for v in vacancies),
                    "Candidate records": candidates,
                    "Job applications": len(current),
                    "Uploaded CVs": len(documents),
                },
                "recent_vacancies": [
                    {"ID": v.id, "Title": v.title, "Status": v.status, "Created": v.created_at}
                    for v in vacancies[:10]
                ],
                "recent_uploads": [
                    {
                        "Filename": d.original_filename,
                        "Application": d.application_id,
                        "Uploaded": d.upload_timestamp,
                    }
                    for d in sorted(documents, key=lambda d: d.upload_timestamp, reverse=True)[:10]
                ],
                "counts": [
                    {
                        "Vacancy": f"{v.id} · {v.title}",
                        "Applications": sum(a.vacancy_id == v.id for a in current),
                    }
                    for v in vacancies
                ],
            }
