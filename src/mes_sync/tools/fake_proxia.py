"""Simuliertes Proxia als SQLite — fuer Tests und Offline-Demo ohne MES-Zugang.

Bildet nur die Tabellen/Spalten nach, die mes_sync liest (TSF_WT, TSF_RUECKMELDUNG,
TRS_RES, TSF_FA, TSF_PSP_ELEMENT). Zeitstempel in UTC wie im Original.

    python -m mes_sync.tools.fake_proxia init output/proxia_fake.sqlite --auftraege 40 --stunden 6
    python -m mes_sync.tools.fake_proxia tick output/proxia_fake.sqlite --anzahl 3
"""

import argparse
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

RESSOURCEN = [
    ("100 Sägen", "C_WKPL", "10000009"),
    ("207 Wareneingangskontrolle", "C_WKPL", "10000233"),
    ("220 QS (FE- Prüfung)", "C_WKPL", "10000221"),
    ("235 Maßkontrolle", "C_WKPL", "10000223"),
    ("309-8 Mazak Quick Turn Nexus", "C_MACH", "10000259"),
    ("311-5 Bohren klein [HKS 40]", "C_MACH", "10000062"),
    ("313-9 Rundschleifen Kellenberger", "C_MACH", "10000279"),
    ("321-22 Weiler E40", "C_MACH", "10000324"),
    ("323-7 Weiler E50", "C_MACH", "10000274"),
    ("340-2 Vorrichten (Vorbereiten)", "C_WKPL", "10000432"),
    ("352 Autofrettage", "C_WKPL", "10000140"),
    ("363 Druckprobe", "C_WKPL", "10000150"),
    ("Fremdfertigung Anstrich FF Anstrich", "C_DELIVERER", "10000145"),
]
VERSAND = ("VERSAND An Versand bereitstellen", "C_WKPL", "10000168")
MATERIAL = [
    ("14049410", "Ventilkörper"), ("17042281", "HD-Bogen, 90° reduz. DN-60x50 PN-3300"),
    ("80131999", "Sieblinse DN-65 PN-2000"), ("16020455", "Spindel M36x3"),
    ("17050012", "Flansch DN-80 PN-2500"), ("14077120", "Ventilsitz Ø42"),
    ("19001377", "Rohrstück 1.4571 L=850"), ("16033902", "Gehäusedeckel"),
]

DDL = """
CREATE TABLE IF NOT EXISTS dbo.TRS_RES (RES_ID TEXT PRIMARY KEY, DISPLAYNAME TEXT, RES_TYPE_ID TEXT);
CREATE TABLE IF NOT EXISTS dbo.TSF_PSP_ELEMENT (PSP_ELEMENT_ID TEXT PRIMARY KEY, PSP_ELEMENT_NR TEXT);
CREATE TABLE IF NOT EXISTS dbo.TSF_FA (FA_ID TEXT PRIMARY KEY, PSP_ELEMENT_ID TEXT, MAT_ID TEXT);
CREATE TABLE IF NOT EXISTS dbo.TSF_WT (
    WT_ID TEXT PRIMARY KEY, PPS_ORDER TEXT, AFO_NR TEXT, DISPLAYNAME TEXT, WT_STATUS_ID TEXT,
    PPS_WORK_CNTR TEXT, PLANNED_RES_ID TEXT, QTY_SOLL REAL, QTY_CONFIRMED_GUT REAL,
    PPS_ART_NR TEXT, PPS_ART_DISPLAYNAME TEXT, PPS_PLANT TEXT, FA_ID TEXT, WT_DELETED INTEGER);
CREATE TABLE IF NOT EXISTS dbo.TSF_RUECKMELDUNG (
    RUECK_ID TEXT PRIMARY KEY, WT_ID TEXT, RUECK_RES_ID TEXT, RUECK_TS TEXT, RUECK_TYPE_ID TEXT,
    WKPL_RES_ID TEXT, CAL_DAY TEXT);
"""

VORGANG_TEXT = {
    "C_WKPL": ["Prüfen", "Kontrolle Maße", "Vorrichten", "Druckprobe nach Plan"],
    "C_MACH": ["Drehen", "Bohren", "Schleifen", "Fräsen Fertigkontur"],
    "C_DELIVERER": ["Oberflächenbehandlung Strahlen + Anstrich"],
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S.") + f"{d.microsecond // 1000:03d}"


def _connect(path: str) -> sqlite3.Connection:
    main = Path(path)
    main.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(main)
    con.execute(f"ATTACH DATABASE '{str(main).replace('.sqlite', '.dbo.sqlite')}' AS dbo")
    return con


def _res_id(name: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, name).hex


def init(path: str, auftraege: int = 40, stunden: float = 6, seed: int = 42) -> None:
    rnd = random.Random(seed)
    con = _connect(path)
    con.executescript(DDL)
    for tbl in ("TRS_RES", "TSF_PSP_ELEMENT", "TSF_FA", "TSF_WT", "TSF_RUECKMELDUNG"):
        con.execute(f"DELETE FROM dbo.{tbl}")
    for name, typ, _ in RESSOURCEN + [VERSAND]:
        con.execute("INSERT INTO dbo.TRS_RES VALUES (?,?,?)", (_res_id(name), name, typ))
    for i in range(6):  # ein paar Werker (C_PERS) — duerfen nie als Standort auftauchen
        con.execute("INSERT INTO dbo.TRS_RES VALUES (?,?,?)", (f"pers{i}", f"Werker {i}", "C_PERS"))

    for n in range(auftraege):
        order = str(70263000 + n * 7)
        fa = f"fa{n}"
        psp = f"psp{n % 6}"
        con.execute("INSERT OR IGNORE INTO dbo.TSF_PSP_ELEMENT VALUES (?,?)", (psp, f"P-26{n % 6:03d}-01"))
        mat_nr, mat_txt = rnd.choice(MATERIAL)
        con.execute("INSERT INTO dbo.TSF_FA VALUES (?,?,?)", (fa, psp, mat_nr))
        soll = rnd.choice([1, 1, 2, 4, 5, 10, 20, 50])
        k = rnd.randint(3, 7)
        kette = rnd.sample(RESSOURCEN, k)
        if rnd.random() < 0.25:  # gelegentlich zwei Vorgaenge am selben Platz
            kette.insert(1, kette[0])
        kette.append(VERSAND)
        for j, (name, typ, wc) in enumerate(kette):
            afo = f"{(j + 1) * 10:04d}"
            con.execute(
                "INSERT INTO dbo.TSF_WT VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (uuid.uuid4().hex, order, afo, rnd.choice(VORGANG_TEXT[typ]), "C_FREI", wc,
                 _res_id(name), soll, 0, mat_nr, mat_txt, "80", fa))
    con.commit()

    # Historie: Zeit in Schritten vorspulen und Ereignisse erzeugen
    jetzt = _utcnow()
    t = jetzt - timedelta(hours=stunden)
    while t < jetzt - timedelta(minutes=20):
        _schritt(con, rnd, t)
        t += timedelta(minutes=rnd.randint(2, 9))
    con.commit()
    con.close()


def _offene_vorgaenge(con):
    return con.execute("""
        SELECT w.WT_ID, w.PPS_ORDER, w.AFO_NR, w.WT_STATUS_ID, w.QTY_SOLL, w.QTY_CONFIRMED_GUT,
               w.PLANNED_RES_ID, r.RES_TYPE_ID
        FROM dbo.TSF_WT w JOIN dbo.TRS_RES r ON r.RES_ID = w.PLANNED_RES_ID
        WHERE w.WT_STATUS_ID <> 'C_FRTG'
          AND NOT EXISTS (SELECT 1 FROM dbo.TSF_WT v WHERE v.PPS_ORDER = w.PPS_ORDER
                          AND v.AFO_NR < w.AFO_NR AND v.WT_STATUS_ID <> 'C_FRTG')
    """).fetchall()


def _event(con, wt_id, res_id, typ, t):
    con.execute("INSERT INTO dbo.TSF_RUECKMELDUNG VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), wt_id, f"pers{random.randint(0, 5)}", _ts(t), typ, res_id,
                 t.strftime("%Y-%m-%d 00:00:00.000")))


def _schritt(con, rnd, t):
    """Ein Ereignis an einem zufaelligen 'aktuellen' Vorgang eines Auftrags."""
    kandidaten = _offene_vorgaenge(con)
    if not kandidaten:
        return
    wt_id, order, afo, status, soll, gut, res_id, _typ = rnd.choice(kandidaten)
    if status in ("C_FREI", "C_NEU"):
        con.execute("UPDATE dbo.TSF_WT SET WT_STATUS_ID='C_ANGM' WHERE WT_ID=?", (wt_id,))
        _event(con, wt_id, res_id, "C_START", t)
        return
    rest = soll - gut
    if soll >= 4 and gut == 0 and rnd.random() < 0.45:
        teil = max(1, int(soll * rnd.choice([0.3, 0.5])))
        con.execute("UPDATE dbo.TSF_WT SET WT_STATUS_ID='C_TFRTG', QTY_CONFIRMED_GUT=? WHERE WT_ID=?",
                    (gut + teil, wt_id))
        _event(con, wt_id, res_id, "C_TFRTG", t)
    else:
        con.execute("UPDATE dbo.TSF_WT SET WT_STATUS_ID='C_FRTG', QTY_CONFIRMED_GUT=? WHERE WT_ID=?",
                    (gut + rest, wt_id))
        _event(con, wt_id, res_id, "C_FRTG", t)


def tick(path: str, anzahl: int = 3, seed: int | None = None) -> None:
    rnd = random.Random(seed)
    con = _connect(path)
    jetzt = _utcnow()
    for i in range(anzahl):
        _schritt(con, rnd, jetzt - timedelta(seconds=(anzahl - i) * 7))
    con.commit()
    con.close()


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("befehl", choices=["init", "tick"])
    p.add_argument("pfad")
    p.add_argument("--auftraege", type=int, default=40)
    p.add_argument("--stunden", type=float, default=6)
    p.add_argument("--anzahl", type=int, default=3)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args(argv)
    if a.befehl == "init":
        init(a.pfad, a.auftraege, a.stunden, a.seed)
    else:
        tick(a.pfad, a.anzahl)
    print("ok")


if __name__ == "__main__":
    main()
