"""Erzeugt aus schema.py ein lesbares T-SQL-Skript (Referenz fuer DBeaver).

Massgeblich ist `python -m mes_sync init-db`; die Datei dient zum Nachlesen/Review.
"""

from pathlib import Path

from sqlalchemy.dialects import mssql
from sqlalchemy.schema import CreateIndex, CreateTable

from ..schema import metadata

HEADER = """-- sce_mes — Tabellen fuer das Stapler-Leitsystem (Milk Run)
-- GENERIERT aus src/mes_sync/schema.py via `uv run python -m mes_sync ddl` — nicht von Hand aendern.
-- Anlegen bitte ueber `uv run python -m mes_sync init-db` (idempotent).

IF SCHEMA_ID('sce_mes') IS NULL EXEC('CREATE SCHEMA sce_mes');
GO
"""


def write_ddl(path: Path) -> Path:
    dialect = mssql.dialect()
    dialect.server_version_info = (15,)  # SQL Server 2019 — sonst wird DATE zu DATETIME
    parts = [HEADER]
    for table in metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";\nGO\n")
        for idx in sorted(table.indexes, key=lambda i: i.name):
            parts.append(str(CreateIndex(idx).compile(dialect=dialect)).strip() + ";\nGO\n")
    parts.append("INSERT INTO sce_mes.poller_status (id) VALUES (1);\nGO\n")
    parts.append("INSERT INTO sce_mes.bestand_status (id) VALUES (1);\nGO\n")
    parts.append("INSERT INTO sce_mes.fahrer (name, aktiv) VALUES (N'Fahrer 1', 1), (N'Fahrer 2', 1);\nGO\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
