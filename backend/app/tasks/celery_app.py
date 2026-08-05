from celery import Celery

from app.config import get_settings


def create_celery_app() -> Celery:
    settings = get_settings()
    app = Celery(
        "contract_review",
        broker=settings.redis_url,
        backend=settings.redis_url,
    )
    app.conf.update(
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_default_queue="review",
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        task_routes={
            "contract_review.ingest_knowledge": {"queue": "ingestion"},
            "contract_review.review_contract": {"queue": "review"},
        },
        imports=("app.tasks.review_contract", "app.tasks.ingest_knowledge"),
    )
    return app


celery_app = create_celery_app()
