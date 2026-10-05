"""End-to-End: simuliertes Proxia (SQLite) -> mes_sync -> sce_mes (SQLite)."""

import pandas as pd
import pytest
from sqlalchemy import text

from mes_sync import state
from mes_sync.config import Settings
from mes_sync.db import _sqlite_engine
from mes_sync.runner import run_once
from mes_sync.tools import fake_proxia

SETTINGS = Settings(poll_sekunden=30, standard_modus="teil", fallback_regel="arbeitsplatz",
                    backfill_stunden=12, ueberlappung_minuten=10)


@pytest.fixture
def umgebung(tmp_path):
    proxia_file = tmp_path / "proxia.sqlite"
    fake_proxia.init(str(proxia_file), auftraege=30, stunden=5, seed=7)
    proxia = _sqlite_engine(f"sqlite:///{proxia_file}", attach_schema="dbo")
    sce = _sqlite_engine(f"sqlite:///{tmp_path / 'sce.sqlite'}", attach_schema="sce_mes")
    state.init_db(sce)
    return proxia_file, proxia, sce


def q(engine, sql):
    with engine.connect() as con:
        return pd.read_sql(text(sql), con)


def test_lauf_ist_idempotent_und_vollstaendig(umgebung):
    proxia_file, proxia, sce = umgebung
    s1 = run_once(SETTINGS, proxia, sce)
    assert s1["events_neu"] > 0
    t = q(sce, "SELECT * FROM sce_mes.transport_auftrag")
    assert len(t) > 0
    # Von/Nach immer gefuellt, nie eine Person als Standort
    assert t["von_res"].notna().all() and t["nach_res"].notna().all()
    assert not t["von_res"].str.startswith("Werker").any()
    assert not t["nach_res"].str.startswith("Werker").any()
    # jeder Transport hat ein 'erstellt'-Event
    ev = q(sce, "SELECT transport_id FROM sce_mes.transport_event WHERE event='erstellt'")
    assert set(ev["transport_id"]) == set(t["id"])

    s2 = run_once(SETTINGS, proxia, sce)
    assert s2["events_neu"] == 0 and s2["transporte_neu"] == 0
    assert len(q(sce, "SELECT id FROM sce_mes.transport_auftrag")) == len(t)


def test_neue_rueckmeldungen_und_auto_erledigung(umgebung):
    proxia_file, proxia, sce = umgebung
    run_once(SETTINGS, proxia, sce)
    vorher = len(q(sce, "SELECT id FROM sce_mes.transport_auftrag"))
    for i in range(15):
        fake_proxia.tick(str(proxia_file), anzahl=4, seed=100 + i)
    s = run_once(SETTINGS, proxia, sce)
    assert s["events_neu"] > 0
    nachher = q(sce, "SELECT status FROM sce_mes.transport_auftrag")
    assert len(nachher) >= vorher
    assert (nachher["status"] == "auto_erledigt").any()
    hb = q(sce, "SELECT * FROM sce_mes.poller_status")
    assert hb.loc[0, "letzter_erfolg"] is not None and hb.loc[0, "wasserzeichen"] is not None


def test_arbeitsplaetze_werden_angelegt_und_pflege_bleibt_erhalten(umgebung):
    proxia_file, proxia, sce = umgebung
    run_once(SETTINGS, proxia, sce)
    with sce.begin() as con:
        con.execute(text("UPDATE sce_mes.arbeitsplatz SET sektor='A', transport_modus='voll' "
                         "WHERE work_cntr='10000009'"))
    fake_proxia.tick(str(proxia_file), anzahl=10, seed=5)
    run_once(SETTINGS, proxia, sce)
    ap = q(sce, "SELECT * FROM sce_mes.arbeitsplatz WHERE work_cntr='10000009'")
    if len(ap):
        assert ap.loc[0, "sektor"] == "A" and ap.loc[0, "transport_modus"] == "voll"
    assert len(q(sce, "SELECT * FROM sce_mes.arbeitsplatz")) > 5
