import json

from curio.api.app import create_app
from curio.db.engine import create_engine, create_session_factory, to_sqlalchemy_url
from curio.db.models import Base
from curio.settings import Settings


def test_to_sqlalchemy_url_normalizes_postgres_schemes():
    assert (
        to_sqlalchemy_url("postgresql://u:p@host:5432/db")
        == "postgresql+psycopg://u:p@host:5432/db"
    )
    assert (
        to_sqlalchemy_url("postgres://u:p@host:5432/db") == "postgresql+psycopg://u:p@host:5432/db"
    )
    assert (
        to_sqlalchemy_url("postgresql+psycopg://u:p@host:5432/db")
        == "postgresql+psycopg://u:p@host:5432/db"
    )


def test_create_session_factory_disables_expire_on_commit():
    settings = Settings(_env_file=None, database_url="postgresql://unused@127.0.0.1:1/unused")
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    assert factory.kw["expire_on_commit"] is False


def test_models_register_all_expected_tables():
    assert set(Base.metadata.tables.keys()) == {
        "users",
        "login_tokens",
        "sessions",
        "user_api_keys",
        "key_decrypt_audit",
        "media_items",
        "presets",
        "slots",
        "catalog_candidates",
        "watch_history",
        "vetoes",
        "jobs",
        "runs",
        "run_events",
        "served_titles",
        "sources",
        "user_sources",
        "source_items",
        "import_issues",
    }


def test_per_user_tables_lead_primary_key_with_user_id():
    for table_name in (
        "login_tokens",
        "sessions",
        "user_api_keys",
        "key_decrypt_audit",
        "presets",
        "slots",
        "catalog_candidates",
        "watch_history",
        "vetoes",
        "user_sources",
        "import_issues",
    ):
        pk_cols = [col.name for col in Base.metadata.tables[table_name].primary_key.columns]
        assert pk_cols[0] == "user_id", f"{table_name} PK does not start with user_id: {pk_cols}"


def test_openapi_schema_never_exposes_encrypted_key():
    app = create_app(
        Settings(_env_file=None, database_url="postgresql://unused@127.0.0.1:1/unused")
    )
    schema_json = json.dumps(app.openapi())
    assert "encrypted_key" not in schema_json
