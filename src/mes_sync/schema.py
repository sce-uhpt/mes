"""Tabellen im SCE-Warehouse, Schema sce_mes.

Einzige Quelle fuer die Tabellendefinitionen: `python -m mes_sync init-db`
legt sie an (idempotent), `sql/001_sce_mes.sql` wird daraus generiert.

Alle Zeitstempel sind UTC (Proxia speichert RUECK_TS in UTC, siehe README).
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Table,
    Unicode,
)
from sqlalchemy.dialects.mssql import DATETIME2

SCHEMA = "sce_mes"
metadata = MetaData(schema=SCHEMA)

TS = DateTime().with_variant(DATETIME2(3), "mssql")

STATUS_WERTE = ("offen", "uebernommen", "erledigt", "auto_erledigt", "storniert")

# ── Rohdaten aus Proxia ──────────────────────────────────────────────────────

pps_rueckmeldung = Table(
    "pps_rueckmeldung",
    metadata,
    Column("rueck_id", String(40), primary_key=True),
    Column("wt_id", String(40), nullable=False),
    Column("pps_order", String(20)),
    Column("afo_nr", String(10)),
    Column("rueck_ts", TS, nullable=False),  # UTC
    Column("rueck_type_id", String(20), nullable=False),
    Column("wkpl_res_id", String(40)),
    Column("ist_res", Unicode(200)),
    Column("ist_res_typ", String(20)),
    Column("geladen_am", TS, nullable=False),
    Index("ix_rueck_wt_ts", "wt_id", "rueck_ts"),
    Index("ix_rueck_ts", "rueck_ts"),
    Index("ix_rueck_geladen", "geladen_am"),
)

pps_vorgang = Table(
    "pps_vorgang",
    metadata,
    Column("wt_id", String(40), primary_key=True),
    Column("pps_order", String(20), nullable=False),
    Column("afo_nr", String(10), nullable=False),
    Column("vorgang_text", Unicode(400)),
    Column("wt_status_id", String(20)),
    Column("work_cntr", String(20)),
    Column("plan_res_id", String(40)),
    Column("plan_res", Unicode(200)),
    Column("plan_res_typ", String(20)),
    Column("qty_soll", Float),
    Column("qty_gut", Float),
    Column("material_nr", Unicode(40)),
    Column("material_text", Unicode(400)),
    Column("werk", String(10)),
    Column("psp", Unicode(40)),
    Column("aktualisiert_am", TS, nullable=False),
    Index("ix_vorgang_order", "pps_order"),
)

# ── Stammdaten (gepflegt) ────────────────────────────────────────────────────

arbeitsplatz = Table(
    "arbeitsplatz",
    metadata,
    Column("work_cntr", String(20), primary_key=True),
    Column("bezeichnung", Unicode(200)),
    Column("res_typ", String(20)),
    # gepflegt von Fertigungssteuerung — der Poller fasst diese Spalten nie an:
    Column("sektor", Unicode(50)),
    Column("transport_modus", String(10)),
    Column("lagerort_code", Unicode(50)),
    Column("aktualisiert_am", TS, nullable=False),
    # Art fuer den Bestandsmonitor: intern | extern | pseudo | fremd_standort.
    # Der Poller setzt nur einen Vorschlag, solange die Spalte leer ist (siehe bestand.py).
    Column("art", String(20)),
    CheckConstraint(
        "transport_modus IS NULL OR transport_modus IN ('teil','voll')",
        name="ck_arbeitsplatz_modus",
    ),
)

fahrer = Table(
    "fahrer",
    metadata,
    Column("name", Unicode(100), primary_key=True),
    Column("aktiv", Boolean, nullable=False, default=True),
)

# ── Transporte ───────────────────────────────────────────────────────────────

transport_auftrag = Table(
    "transport_auftrag",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("quelle_key", String(60), nullable=False, unique=True),
    Column("modus", String(10), nullable=False),
    Column("pps_order", String(20), nullable=False),
    Column("material_nr", Unicode(40)),
    Column("material_text", Unicode(400)),
    Column("psp", Unicode(40)),
    Column("von_wt_id", String(40), nullable=False),
    Column("von_afo", String(10)),
    Column("von_vorgang_text", Unicode(400)),
    Column("von_work_cntr", String(20)),
    Column("von_res", Unicode(200)),
    Column("von_res_typ", String(20)),
    Column("ist_res", Unicode(200)),
    Column("nach_wt_id", String(40), nullable=False),
    Column("nach_afo", String(10)),
    Column("nach_vorgang_text", Unicode(400)),
    Column("nach_work_cntr", String(20)),
    Column("nach_res", Unicode(200)),
    Column("nach_res_typ", String(20)),
    Column("menge", Float),
    Column("menge_soll", Float),
    Column("ist_teilmenge", Boolean, nullable=False, default=False),
    Column("rueck_id", String(40)),
    Column("rueck_ts", TS),  # UTC, Zeitpunkt der ausloesenden Rueckmeldung
    Column("status", String(20), nullable=False, default="offen"),
    Column("erstellt_am", TS, nullable=False),
    Column("uebernommen_von", Unicode(100)),
    Column("uebernommen_am", TS),
    Column("erledigt_von", Unicode(100)),
    Column("erledigt_am", TS),
    Column("geaendert_am", TS, nullable=False),
    CheckConstraint(
        "status IN ('offen','uebernommen','erledigt','auto_erledigt','storniert')",
        name="ck_transport_status",
    ),
    CheckConstraint("modus IN ('teil','voll')", name="ck_transport_modus"),
    Index("ix_transport_status", "status"),
    Index("ix_transport_erstellt", "erstellt_am"),
    Index("ix_transport_von_wt", "von_wt_id"),
)

transport_event = Table(
    "transport_event",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("transport_id", Integer, ForeignKey(f"{SCHEMA}.transport_auftrag.id"), nullable=False),
    Column("event", String(20), nullable=False),
    Column("fahrer", Unicode(100)),
    Column("ts", TS, nullable=False),
    Column("info", Unicode(400)),
    Index("ix_event_transport", "transport_id"),
    Index("ix_event_ts", "ts"),
)

# ── Bestandsmonitor (Ware in Arbeit) ─────────────────────────────────────────
# Puffer = Uebergang von Vorgang A (von_wt_id) zum Nachfolger B. Sein Bestand ist
# Gutmenge(A) minus Verbrauch(B) = Gut + Ausschuss an B. Transporte verteilen ihn
# auf die Orte bereit (bei A) / unterwegs / an_b.

bestand_vorgang = Table(
    "bestand_vorgang",
    metadata,
    Column("wt_id", String(40), primary_key=True),
    Column("pps_order", String(20), nullable=False),
    Column("afo_nr", String(10), nullable=False),
    Column("vorgang_text", Unicode(400)),
    Column("wt_status_id", String(20)),
    Column("work_cntr", String(20)),
    Column("plan_res", Unicode(200)),
    Column("plan_res_typ", String(20)),
    Column("vor_wt_id", String(40)),
    Column("nach_wt_id", String(40)),
    Column("qty_soll", Float),
    Column("qty_gut", Float),        # wirksame Gutmenge (Fehlbuchungen herausgerechnet)
    Column("qty_gut_proxia", Float), # Rohwert QTY_CONFIRMED_GUT
    Column("qty_aus", Float),
    Column("qty_nach", Float),
    Column("einheit", String(20)),
    Column("conf_nr", String(20)),
    Column("begin_scheduled", TS),
    Column("material_nr", Unicode(40)),
    Column("material_text", Unicode(400)),
    Column("psp", Unicode(40)),
    Column("erste_meldung_ts", TS),   # erste Proxia-Rueckmeldung (egal welcher Typ)
    Column("letzte_meldung_ts", TS),  # letzte Teil-/Fertigmeldung
    Column("plausi", Unicode(200)),
    Column("abgeschlossen", Boolean, nullable=False, default=False),
    Column("aktualisiert_am", TS, nullable=False),
    Index("ix_bvorgang_order", "pps_order"),
    Index("ix_bvorgang_wc", "work_cntr"),
)

wip_bewegung = Table(
    "wip_bewegung",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("quelle_key", String(100), nullable=False, unique=True),
    Column("ts", TS, nullable=False),  # UTC, fachlicher Zeitpunkt
    Column("pps_order", String(20), nullable=False),
    Column("von_wt_id", String(40), nullable=False),
    Column("nach_wt_id", String(40)),
    Column("von_work_cntr", String(20)),
    Column("nach_work_cntr", String(20)),
    Column("ort", String(12), nullable=False),
    Column("menge", Float, nullable=False),
    Column("art", String(20), nullable=False),
    Column("zeit_quelle", String(20), nullable=False),
    Column("transport_id", Integer),
    Column("info", Unicode(400)),
    Column("erfasst_am", TS, nullable=False),
    CheckConstraint("ort IN ('bereit','unterwegs','an_b')", name="ck_wip_ort"),
    Index("ix_wip_von", "von_wt_id"),
    Index("ix_wip_ts", "ts"),
    Index("ix_wip_order", "pps_order"),
    Index("ix_wip_transport", "transport_id"),
)

bestand_puffer = Table(
    "bestand_puffer",
    metadata,
    Column("von_wt_id", String(40), primary_key=True),
    Column("pps_order", String(20), nullable=False),
    Column("nach_wt_id", String(40)),
    Column("menge_bereit", Float, nullable=False, default=0),
    Column("menge_unterwegs", Float, nullable=False, default=0),
    Column("menge_an_b", Float, nullable=False, default=0),
    Column("menge_gesamt", Float, nullable=False, default=0),
    Column("liegt_seit", TS),
    Column("letzte_bewegung", TS),
    Column("zeit_geschaetzt", Boolean, nullable=False, default=False),
    Column("aktualisiert_am", TS, nullable=False),
    Index("ix_puffer_order", "pps_order"),
    Index("ix_puffer_nach", "nach_wt_id"),
)

bestand_tag = Table(
    "bestand_tag",
    metadata,
    Column("tag", Date, primary_key=True),          # Kalendertag Europe/Berlin, Stand Tagesende
    Column("work_cntr", String(20), primary_key=True),  # Arbeitsplatz B, vor dem die Ware wartet
    Column("anzahl_puffer", Integer, nullable=False),
    Column("menge", Float, nullable=False),
    Column("aktualisiert_am", TS, nullable=False),
)

bestand_status = Table(
    "bestand_status",
    metadata,
    Column("id", SmallInteger, primary_key=True, autoincrement=False),
    Column("letzter_lauf", TS),
    Column("letzter_erfolg", TS),
    Column("backfill_ab", TS),       # Beginn des nachgeladenen Zeitfensters (UTC)
    Column("rueck_wz", TS),          # bis hierhin wurden pps_rueckmeldung-Zeilen ausgewertet
    Column("event_wz", Integer),     # max. transport_event.id, die verbucht ist
    Column("meldung", Unicode(1000)),
)

poller_status = Table(
    "poller_status",
    metadata,
    Column("id", SmallInteger, primary_key=True, autoincrement=False),
    Column("letzter_lauf", TS),
    Column("letzter_erfolg", TS),
    Column("wasserzeichen", TS),  # max. RUECK_TS (UTC), bis zu dem gelesen wurde
    Column("anzahl_neu", Integer),
    Column("meldung", Unicode(1000)),
    Column("host", Unicode(100)),
)
