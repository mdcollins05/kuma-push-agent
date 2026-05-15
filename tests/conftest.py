import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.database import Base
from app.dependencies import get_db, require_api_key, require_auth
from app.main import app
from app.models import AppSettings

# StaticPool ensures all connections share the same in-memory SQLite database
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


async def override_require_api_key():
    return


async def override_require_auth():
    return "testuser"


@pytest.fixture(scope="session")
def client():
    Base.metadata.create_all(bind=engine)

    db = TestingSessionLocal()
    db.add(AppSettings(
        id=1,
        configured=False,   # kuma_url is None — skips live Kuma calls in routes
        api_key="test-key",
        ui_username="admin",
        ui_password_hash="x",
    ))
    db.commit()
    db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_api_key] = override_require_api_key
    app.dependency_overrides[require_auth] = override_require_auth

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c

    app.dependency_overrides.clear()


HEADERS = {"X-API-Key": "test-key"}
