"""Datenbankverbindungen.

PROXIA: Lesezugriff mit dem User 'report' (Basistabellen erlaubt, siehe PROXIA_DB.md).
SCE:    Schreibzugriff ins Schema sce_mes.

Fuer Tests/Demo lassen sich beide Verbindungen per URL ueberschreiben
(PROXIA_DB_URL / SCE_DB_URL, z. B. sqlite:///...).
"""

import os
from urllib.parse import quote_plus

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from . import config  # noqa: F401  (laedt .env)


def _mssql_engine(prefix: str) -> Engine:
    driver = os.environ[f"{prefix}_SQL_DRIVER"]
    server = os.environ[f"{prefix}_SQL_SERVER"]
    database = os.environ[f"{prefix}_SQL_DATABASE"]
    user = os.environ[f"{prefix}_SQL_USER"]
    password = os.environ[f"{prefix}_SQL_PASSWORD"]
    conn_str = (
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
        f"UID={user};PWD={password};TrustServerCertificate=yes;"
        f"APP=mes_sync"
    )
    return create_engine(
        f"mssql+pyodbc:///?odbc_connect={quote_plus(conn_str)}",
        fast_executemany=True,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def _sqlite_engine(url: str, attach_schema: str | None = None) -> Engine:
    engine = create_engine(url)
    if attach_schema:
        # SQLite kennt keine Schemas — als angehaengte DB nachbilden,
        # damit 'sce_mes.tabelle' identisch funktioniert.
        db_file = url.replace("sqlite:///", "")
        attach_file = db_file.replace(".sqlite", f".{attach_schema}.sqlite") if db_file else ":memory:"

        @event.listens_for(engine, "connect")
        def _attach(dbapi_conn, _):
            dbapi_conn.execute(f"ATTACH DATABASE '{attach_file}' AS {attach_schema}")

    return engine


def get_proxia_engine() -> Engine:
    url = os.environ.get("PROXIA_DB_URL")
    if url:
        return _sqlite_engine(url, attach_schema="dbo")
    # Proxia ist das produktive MES: ohne Lesesperren lesen, damit der Poller
    # niemals Buchungen an den Terminals blockiert (Proxia-Views nutzen selbst NOLOCK).
    return _mssql_engine("PROXIA").execution_options(isolation_level="READ UNCOMMITTED")


def get_sce_engine() -> Engine:
    url = os.environ.get("SCE_DB_URL")
    if url:
        return _sqlite_engine(url, attach_schema="sce_mes")
    return _mssql_engine("SCE")
