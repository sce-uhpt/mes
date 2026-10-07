"""Aufruf:
    uv run python -m mes_sync                 Dauerschleife (systemd-Service)
    uv run python -m mes_sync --once          ein Durchlauf, Statistik ausgeben
    uv run python -m mes_sync init-db         Schema sce_mes + Tabellen anlegen (idempotent)
    uv run python -m mes_sync ddl             T-SQL-DDL nach sql/001_sce_mes.sql schreiben
    uv run python -m mes_sync export-arbeitsplaetze   Excel zur Sektorpflege
    uv run python -m mes_sync bestand-reset   Bestandsjournal leeren (naechster Lauf baut neu auf)
"""

import argparse
import logging
import sys

from .config import PROJECT_ROOT, Settings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mes_sync")
    p.add_argument("befehl", nargs="?", default="run",
                   choices=["run", "init-db", "ddl", "export-arbeitsplaetze", "bestand-reset"])
    p.add_argument("--once", action="store_true", help="nur ein Durchlauf")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    if a.befehl == "ddl":
        from .tools.ddl import write_ddl
        path = write_ddl(PROJECT_ROOT / "sql" / "001_sce_mes.sql")
        print(f"geschrieben: {path}")
        return 0

    from .db import get_proxia_engine, get_sce_engine

    if a.befehl == "init-db":
        from .state import init_db
        init_db(get_sce_engine())
        print("sce_mes ist eingerichtet.")
        return 0

    if a.befehl == "bestand-reset":
        from .bestand_lauf import zuruecksetzen
        zuruecksetzen(get_sce_engine())
        print("Bestandsjournal geleert — der naechste Lauf laedt neu nach.")
        return 0

    if a.befehl == "export-arbeitsplaetze":
        from .tools.export_arbeitsplaetze import export
        print(f"geschrieben: {export(get_sce_engine())}")
        return 0

    from .runner import run_bestand, run_forever, run_once
    settings = Settings.from_env()
    proxia_engine, sce_engine = get_proxia_engine(), get_sce_engine()
    if a.once:
        stat = run_once(settings, proxia_engine, sce_engine)
        for k, v in stat.items():
            print(f"{k:>24}: {v}")
        if settings.bestand_aktiv:
            print("── Bestand ──")
            b = run_bestand(settings, proxia_engine, sce_engine) or {"fehler": "siehe Log"}
            for k, v in b.items():
                print(f"{k:>24}: {v}")
        return 0
    run_forever(settings, proxia_engine, sce_engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
