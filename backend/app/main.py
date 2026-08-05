from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.admin.routes import router as admin_router
from app.contracts.routes import router as contracts_router
from app.contracts.service import JobNotFound


def create_app(a2a_handler=None) -> FastAPI:
    app = FastAPI(title="Contract Review Agent")
    app.include_router(contracts_router)
    app.include_router(admin_router)

    from app.a2a.server import build_a2a_application

    build_a2a_application(a2a_handler).add_routes_to_app(app, rpc_url="/a2a")

    @app.exception_handler(JobNotFound)
    async def job_not_found_handler(request, exc):
        return JSONResponse(
            status_code=404,
            content={"detail": "review job does not exist"},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ready"}

    return app


app = create_app()
