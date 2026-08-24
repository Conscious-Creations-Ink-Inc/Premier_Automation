"""Health and demo-data lifecycle."""
from fastapi import APIRouter, Depends

from api import config, deps, schemas
from api.demo import seed as demo_seed

router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/health", response_model=schemas.HealthResponse)
def health(conn=Depends(deps.get_conn)) -> schemas.HealthResponse:
    counts = demo_seed.summary(conn)
    return schemas.HealthResponse(
        status="ok",
        database=str(config.DEMO_DB_PATH),
        seeded=counts["po_lines"] > 0,
        counts=counts,
    )


@router.post("/admin/seed", response_model=schemas.SeedResponse)
def seed(reset: bool = False, conn=Depends(deps.get_conn)) -> schemas.SeedResponse:
    """Seed the demo. With `reset=true` the existing rows are cleared first, which returns the
    dataset to its documented baseline — useful after a demo has been clicked through."""
    return schemas.SeedResponse(seeded=True, counts=demo_seed.seed_demo(conn, reset_first=reset))


@router.post("/admin/reset", response_model=schemas.SeedResponse)
def reset(conn=Depends(deps.get_conn)) -> schemas.SeedResponse:
    return schemas.SeedResponse(seeded=True, counts=demo_seed.seed_demo(conn, reset_first=True))
