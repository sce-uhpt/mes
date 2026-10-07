"""Schreib-/Lesezugriffe auf sce_mes (SCE-Warehouse).

Bewusst nur SQLAlchemy Core ohne T-SQL-Spezialitaeten, damit derselbe Code
gegen SQL Server (Produktion) und SQLite (Tests/Demo) laeuft.
Alle Zeitstempel: naive UTC.
"""

import socket
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import and_, func, insert, inspect, select, update
from sqlalchemy.engine import Connection, Engine

from .schema import (
    SCHEMA,
    arbeitsplatz,
    bestand_status,
    fahrer,
    metadata,
    poller_status,
    pps_rueckmeldung,
    pps_vorgang,
    transport_auftrag,
    transport_event,
)

CHUNK = 500
OFFEN = ("offen", "uebernommen")


def utcnow() -> datetime:
    d = datetime.now(timezone.utc).replace(tzinfo=None)
    return d.replace(microsecond=d.microsecond // 1000 * 1000)


def _none(v):
    """pandas-NA/NaT/NaN -> None, numpy-Typen -> Python-Typen."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if hasattr(v, "item"):
        return v.item()
    return v


def _records(df: pd.DataFrame, cols) -> list[dict]:
    return [{c: _none(r[c]) for c in cols} for r in df[cols].to_dict("records")]


def _chunks(seq, n=CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ── Setup ────────────────────────────────────────────────────────────────────

def init_db(engine: Engine) -> None:
    """Schema + Tabellen anlegen (idempotent) und Startwerte setzen."""
    if engine.dialect.name == "mssql":
        with engine.begin() as con:
            con.exec_driver_sql(
                "IF SCHEMA_ID('sce_mes') IS NULL EXEC('CREATE SCHEMA sce_mes')")
    metadata.create_all(engine, checkfirst=True)
    _ergaenze_spalten(engine)
    with engine.begin() as con:
        if con.execute(select(func.count()).select_from(poller_status)).scalar() == 0:
            con.execute(insert(poller_status).values(id=1))
        if con.execute(select(func.count()).select_from(bestand_status)).scalar() == 0:
            con.execute(insert(bestand_status).values(id=1))
        if con.execute(select(func.count()).select_from(fahrer)).scalar() == 0:
            con.execute(insert(fahrer), [
                {"name": "Fahrer 1", "aktiv": True},
                {"name": "Fahrer 2", "aktiv": True},
            ])


def _ergaenze_spalten(engine: Engine) -> None:
    """Spalten, die nach dem ersten init-db dazugekommen sind, per ALTER TABLE nachziehen.

    create_all legt nur fehlende Tabellen an, keine fehlenden Spalten.
    """
    insp = inspect(engine)
    for table in metadata.sorted_tables:
        if not insp.has_table(table.name, schema=SCHEMA):
            continue
        vorhanden = {c["name"].lower() for c in insp.get_columns(table.name, schema=SCHEMA)}
        for col in table.columns:
            if col.name.lower() in vorhanden:
                continue
            typ = col.type.compile(dialect=engine.dialect)
            with engine.begin() as con:
                con.exec_driver_sql(f"ALTER TABLE {SCHEMA}.{table.name} ADD {col.name} {typ} NULL")
        vorhandene_idx = {i["name"].lower() for i in insp.get_indexes(table.name, schema=SCHEMA) if i.get("name")}
        for idx in table.indexes:
            if idx.name and idx.name.lower() not in vorhandene_idx:
                idx.create(bind=engine)


# ── Lesen ────────────────────────────────────────────────────────────────────

def get_wasserzeichen(con: Connection):
    return con.execute(select(poller_status.c.wasserzeichen).where(poller_status.c.id == 1)).scalar()


def neue_events(con: Connection, events: pd.DataFrame) -> pd.DataFrame:
    """Nur Rueckmeldungen, die noch nicht in sce_mes.pps_rueckmeldung stehen."""
    if events.empty:
        return events
    bekannt = set()
    for ids in _chunks(events["rueck_id"].unique()):
        bekannt |= set(con.execute(
            select(pps_rueckmeldung.c.rueck_id).where(pps_rueckmeldung.c.rueck_id.in_(ids))
        ).scalars())
    return events[~events["rueck_id"].isin(bekannt)].copy()


def offene_transporte(con: Connection) -> pd.DataFrame:
    t = transport_auftrag.c
    return pd.read_sql(
        select(t.id, t.pps_order, t.nach_wt_id, t.rueck_ts, t.status).where(t.status.in_(OFFEN)),
        con,
    )


def bisherige_transporte(con: Connection, von_wt_ids) -> pd.DataFrame:
    t = transport_auftrag.c
    frames = [pd.read_sql(
        select(t.von_wt_id, t.quelle_key, t.menge, t.status).where(t.von_wt_id.in_(ids)), con)
        for ids in _chunks(von_wt_ids)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["von_wt_id", "quelle_key", "menge", "status"])


def lade_arbeitsplatz(con: Connection) -> pd.DataFrame:
    a = arbeitsplatz.c
    return pd.read_sql(select(a.work_cntr, a.sektor, a.transport_modus), con)


def events_an_vorgaengen(con: Connection, wt_ids) -> pd.DataFrame:
    r = pps_rueckmeldung.c
    frames = [pd.read_sql(
        select(r.wt_id, r.rueck_ts, r.rueck_type_id).where(r.wt_id.in_(ids)), con)
        for ids in _chunks(wt_ids)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["wt_id", "rueck_ts", "rueck_type_id"])


# ── Schreiben ────────────────────────────────────────────────────────────────

def speichere_events(con: Connection, events: pd.DataFrame, jetzt: datetime) -> int:
    if events.empty:
        return 0
    df = events.copy()
    df["geladen_am"] = jetzt
    cols = [c.name for c in pps_rueckmeldung.columns if c.name in df.columns]
    con.execute(insert(pps_rueckmeldung), _records(df, cols))
    return len(df)


def speichere_vorgaenge(con: Connection, vorgaenge: pd.DataFrame, jetzt: datetime) -> int:
    """Upsert per Delete+Insert (portabel; Datenmengen je Lauf sind klein)."""
    if vorgaenge.empty:
        return 0
    df = vorgaenge.copy()
    df["aktualisiert_am"] = jetzt
    for ids in _chunks(df["wt_id"].unique()):
        con.execute(pps_vorgang.delete().where(pps_vorgang.c.wt_id.in_(ids)))
    cols = [c.name for c in pps_vorgang.columns if c.name in df.columns]
    con.execute(insert(pps_vorgang), _records(df, cols))
    return len(df)


def ergaenze_arbeitsplaetze(con: Connection, vorgaenge: pd.DataFrame, jetzt: datetime) -> int:
    """Neue Arbeitsplaetze anlegen. Gepflegte Spalten (sektor, modus, lagerort) bleiben unberuehrt."""
    if vorgaenge.empty:
        return 0
    ap = (vorgaenge.dropna(subset=["work_cntr"])
          .sort_values("plan_res")
          .drop_duplicates("work_cntr")[["work_cntr", "plan_res", "plan_res_typ"]])
    bekannt = set(con.execute(select(arbeitsplatz.c.work_cntr)).scalars())
    neu = ap[~ap["work_cntr"].isin(bekannt)]
    if neu.empty:
        return 0
    con.execute(insert(arbeitsplatz), [
        {"work_cntr": _none(r.work_cntr), "bezeichnung": _none(r.plan_res),
         "res_typ": _none(r.plan_res_typ), "aktualisiert_am": jetzt}
        for r in neu.itertuples(index=False)
    ])
    return len(neu)


def speichere_transporte(con: Connection, transporte: pd.DataFrame, jetzt: datetime) -> int:
    if transporte.empty:
        return 0
    df = transporte.copy()
    vorhanden = set()
    for keys in _chunks(df["quelle_key"].unique()):
        vorhanden |= set(con.execute(
            select(transport_auftrag.c.quelle_key).where(transport_auftrag.c.quelle_key.in_(keys))
        ).scalars())
    df = df[~df["quelle_key"].isin(vorhanden)]
    if df.empty:
        return 0
    df["status"] = "offen"
    df["erstellt_am"] = jetzt
    df["geaendert_am"] = jetzt
    cols = [c.name for c in transport_auftrag.columns if c.name in df.columns]
    con.execute(insert(transport_auftrag), _records(df, cols))

    ids = []
    for keys in _chunks(df["quelle_key"]):
        ids += list(con.execute(
            select(transport_auftrag.c.id).where(transport_auftrag.c.quelle_key.in_(keys))
        ).scalars())
    con.execute(insert(transport_event), [
        {"transport_id": i, "event": "erstellt", "fahrer": None, "ts": jetzt, "info": None}
        for i in ids
    ])
    return len(df)


def auto_erledigen(con: Connection, treffer: pd.DataFrame, jetzt: datetime) -> int:
    n = 0
    t = transport_auftrag.c
    for r in treffer.itertuples(index=False):
        ts = _none(r.erledigt_ts) or jetzt
        res = con.execute(
            update(transport_auftrag)
            .where(and_(t.id == int(r.id), t.status.in_(OFFEN)))
            .values(status="auto_erledigt", erledigt_am=ts, erledigt_von="automatisch",
                    geaendert_am=jetzt)
        )
        if res.rowcount:
            con.execute(insert(transport_event).values(
                transport_id=int(r.id), event="auto_erledigt", fahrer=None, ts=jetzt,
                info=str(r.grund)[:400]))
            n += 1
    return n


def heartbeat(con: Connection, jetzt: datetime, erfolg: bool, meldung: str,
              wasserzeichen=None, anzahl_neu: int | None = None) -> None:
    werte = {"letzter_lauf": jetzt, "meldung": (meldung or "")[:1000],
             "host": socket.gethostname()[:100]}
    if erfolg:
        werte["letzter_erfolg"] = jetzt
        werte["anzahl_neu"] = anzahl_neu
        if wasserzeichen is not None:
            werte["wasserzeichen"] = wasserzeichen
    con.execute(update(poller_status).where(poller_status.c.id == 1).values(**werte))
