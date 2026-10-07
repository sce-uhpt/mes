"""Simuliertes Proxia als SQLite — fuer Tests und Offline-Demo ohne MES-Zugang.

Bildet nur die Tabellen/Spalten nach, die mes_sync liest (TSF_WT, TSF_RUECKMELDUNG,
TSF_WT_QTY, TRS_RES, TSF_FA, TSF_PSP_ELEMENT). Zeitstempel in UTC wie im Original.

Wie in echt (Stand 06.10.2026, PROXIA_DB.md Abschn. 10):
- Mengen stehen nur in TSF_WT (Summe) und TSF_WT_QTY (je Tag/Schicht/Person, nur C_GUT)
- einige Arbeitsplaetze melden NICHT in Proxia zurueck (Fremdfertigung, Versand, teils
  Mass-/QS-Kontrolle): dann aendern sich nur Status und Gutmenge in TSF_WT
- gelegentlich Ausschuss (QTY_CONFIRMED_AUS) und eine Fehlbuchung (2^31)

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
    PPS_ART_NR TEXT, PPS_ART_DISPLAYNAME TEXT, PPS_PLANT TEXT, FA_ID TEXT, WT_DELETED INTEGER,
    QTY_CONFIRMED_AUS REAL DEFAULT 0, QTY_CONFIRMED_NACH REAL DEFAULT 0, UNIT_QTY TEXT,
    CONF_NR TEXT, BEGIN_SCHEDULED TEXT,
    U_FREIGABE TEXT);  -- nur Simulator: ab wann der Auftrag bearbeitet wird
CREATE TABLE IF NOT EXISTS dbo.TSF_WT_QTY (
    WT_QTY_ID TEXT PRIMARY KEY, RES_ID TEXT, WT_ID TEXT, SHIFT_ID TEXT, TIMEFRAME_ID TEXT,
    CAL_DAY TEXT, QTY_CLASS_ID TEXT, QTY REAL);
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


# Arbeitsplaetze, die (teilweise) ausserhalb von Proxia zurueckmelden: Anteil ohne Rueckmeldung
OHNE_RUECKMELDUNG = {"10000145": 1.0, "10000168": 1.0, "10000223": 0.35, "10000221": 0.2}


def init(path: str, auftraege: int = 40, stunden: float = 6, seed: int = 42) -> None:
    rnd = random.Random(seed)
    con = _connect(path)
    con.executescript(DDL)
    for tbl in ("TRS_RES", "TSF_PSP_ELEMENT", "TSF_FA", "TSF_WT", "TSF_RUECKMELDUNG", "TSF_WT_QTY"):
        con.execute(f"DELETE FROM dbo.{tbl}")
    for name, typ, _ in RESSOURCEN + [VERSAND]:
        con.execute("INSERT INTO dbo.TRS_RES VALUES (?,?,?)", (_res_id(name), name, typ))
    for i in range(6):  # ein paar Werker (C_PERS) — duerfen nie als Standort auftauchen
        con.execute("INSERT INTO dbo.TRS_RES VALUES (?,?,?)", (f"pers{i}", f"Werker {i}", "C_PERS"))

    jetzt = _utcnow()
    start = jetzt - timedelta(hours=stunden)
    conf = 77670000
    for n in range(auftraege):
        order = str(70263000 + n * 7)
        # Freigabe verteilt ueber die ersten 85 % des Zeitraums, damit immer Auftraege laufen
        freigabe = start + timedelta(hours=stunden * 0.85 * n / max(auftraege, 1))
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
        einheit = rnd.choice(["C_STK", "C_STK", "C_UNK"])
        for j, (name, typ, wc) in enumerate(kette):
            afo = f"{(j + 1) * 10:04d}"
            conf += 1
            termin = freigabe + timedelta(days=2 * j + 1)
            con.execute(
                "INSERT INTO dbo.TSF_WT VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,?,?,?)",
                (uuid.uuid4().hex, order, afo, rnd.choice(VORGANG_TEXT.get(typ, ["Versand"])), "C_FREI", wc,
                 _res_id(name), soll, 0, mat_nr, mat_txt, "80", fa, einheit, f"00{conf}",
                 _ts(termin), _ts(freigabe)))
    con.commit()

    # Historie: Zeit in Schritten vorspulen und Ereignisse erzeugen
    # Schrittweite so, dass ein Auftrag ~16 Ereignisse ueber den Zeitraum bekommt
    faktor = max(1.0, stunden * 60 / (max(auftraege, 1) * 16 * 5.5))
    t = start
    while t < jetzt - timedelta(minutes=20):
        _schritt(con, rnd, t)
        t += timedelta(minutes=rnd.randint(2, 9) * faktor)
    _fehlbuchung(con, rnd)
    con.commit()
    con.close()


def _fehlbuchung(con, rnd):
    """Eine Gutmenge 2^31 wie in Proxia beobachtet (Ueberlauf) an einem fertigen Vorgang."""
    row = con.execute("SELECT r.WT_ID FROM dbo.TSF_RUECKMELDUNG r JOIN dbo.TSF_WT w ON w.WT_ID = r.WT_ID "
                      "WHERE r.RUECK_TYPE_ID='C_FRTG' AND w.PPS_WORK_CNTR NOT IN ('10000145','10000168') "
                      "ORDER BY r.RUECK_TS DESC LIMIT 1").fetchone()
    if row:
        con.execute("UPDATE dbo.TSF_WT SET QTY_CONFIRMED_GUT = QTY_CONFIRMED_GUT + 2147483648 WHERE WT_ID=?",
                    row)
        con.execute("UPDATE dbo.TSF_WT_QTY SET QTY = QTY + 2147483648 WHERE WT_QTY_ID = "
                    "(SELECT WT_QTY_ID FROM dbo.TSF_WT_QTY WHERE WT_ID=? LIMIT 1)", row)


def _offene_vorgaenge(con, t=None):
    """Vorgaenge, die als naechstes dran sind. Der Nachfolger darf schon starten, sobald
    der Vorgaenger eine Teilmenge geliefert hat (Teillose)."""
    return con.execute("""
        SELECT w.WT_ID, w.PPS_ORDER, w.AFO_NR, w.WT_STATUS_ID, w.QTY_SOLL, w.QTY_CONFIRMED_GUT,
               w.PLANNED_RES_ID, r.RES_TYPE_ID, w.PPS_WORK_CNTR, w.QTY_CONFIRMED_AUS,
               COALESCE((SELECT v.QTY_CONFIRMED_GUT FROM dbo.TSF_WT v WHERE v.PPS_ORDER = w.PPS_ORDER
                         AND v.AFO_NR < w.AFO_NR ORDER BY v.AFO_NR DESC LIMIT 1), w.QTY_SOLL) AS VOR_GUT
        FROM dbo.TSF_WT w JOIN dbo.TRS_RES r ON r.RES_ID = w.PLANNED_RES_ID
        WHERE w.WT_STATUS_ID <> 'C_FRTG'
          AND (? IS NULL OR w.U_FREIGABE IS NULL OR w.U_FREIGABE <= ?)
          AND NOT EXISTS (SELECT 1 FROM dbo.TSF_WT v WHERE v.PPS_ORDER = w.PPS_ORDER
                          AND v.AFO_NR < w.AFO_NR AND v.WT_STATUS_ID <> 'C_FRTG'
                          AND v.QTY_CONFIRMED_GUT <= 0)
          AND NOT EXISTS (SELECT 1 FROM dbo.TSF_WT v WHERE v.PPS_ORDER = w.PPS_ORDER
                          AND v.AFO_NR < w.AFO_NR AND v.WT_STATUS_ID <> 'C_FRTG'
                          AND EXISTS (SELECT 1 FROM dbo.TSF_WT u WHERE u.PPS_ORDER = v.PPS_ORDER
                                      AND u.AFO_NR < v.AFO_NR AND u.WT_STATUS_ID <> 'C_FRTG'))
    """, (_ts(t) if t else None, _ts(t) if t else None)).fetchall()


def _event(con, wt_id, res_id, typ, t):
    con.execute("INSERT INTO dbo.TSF_RUECKMELDUNG VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), wt_id, f"pers{random.randint(0, 5)}", _ts(t), typ, res_id,
                 t.strftime("%Y-%m-%d 00:00:00.000")))


def _schicht(t: datetime) -> str:
    h = (t + timedelta(hours=2)).hour  # grob lokale Zeit
    return "C_FRUEH" if 6 <= h < 14 else "C_SPAET" if 14 <= h < 22 else "C_NONE"


def _menge_buchen(con, wt_id, menge, t):
    """TSF_WT_QTY: eine Zeile je Vorgang/Tag/Schicht/Person, Menge wird aufaddiert."""
    tag = (t + timedelta(hours=2)).strftime("%Y-%m-%d 00:00:00.000")
    schicht, person = _schicht(t), f"pers{random.randint(0, 5)}"
    row = con.execute("SELECT WT_QTY_ID FROM dbo.TSF_WT_QTY WHERE WT_ID=? AND CAL_DAY=? AND SHIFT_ID=? "
                      "AND RES_ID=?", (wt_id, tag, schicht, person)).fetchone()
    if row:
        con.execute("UPDATE dbo.TSF_WT_QTY SET QTY = QTY + ? WHERE WT_QTY_ID=?", (menge, row[0]))
    else:
        con.execute("INSERT INTO dbo.TSF_WT_QTY VALUES (?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, person, wt_id, schicht, "C_CALDAY", tag, "C_GUT", menge))


def _schritt(con, rnd, t):
    """Ein Ereignis an einem zufaelligen 'aktuellen' Vorgang eines Auftrags."""
    kandidaten = _offene_vorgaenge(con, t)
    if not kandidaten:
        return
    wt_id, order, afo, status, soll, gut, res_id, _typ, wc, aus, vor_gut = rnd.choice(kandidaten)
    ohne = rnd.random() < OHNE_RUECKMELDUNG.get(wc, 0.0)
    if status in ("C_FREI", "C_NEU"):
        con.execute("UPDATE dbo.TSF_WT SET WT_STATUS_ID='C_ANGM' WHERE WT_ID=?", (wt_id,))
        if not ohne:
            _event(con, wt_id, res_id, "C_START", t)
        return
    verfuegbar = max(0.0, (vor_gut or 0) - gut - (aus or 0))
    rest = soll - gut - (aus or 0)
    if verfuegbar <= 0:
        return  # wartet auf die naechste Teilmenge des Vorgaengers
    if soll >= 4 and gut == 0 and rnd.random() < 0.45 and not ohne:
        teil = max(1, int(min(soll * rnd.choice([0.3, 0.5]), verfuegbar)))
        con.execute("UPDATE dbo.TSF_WT SET WT_STATUS_ID='C_TFRTG', QTY_CONFIRMED_GUT=? WHERE WT_ID=?",
                    (gut + teil, wt_id))
        _menge_buchen(con, wt_id, teil, t)
        _event(con, wt_id, res_id, "C_TFRTG", t)
        return
    if verfuegbar < rest:  # Vorgaenger hat noch nicht alles geliefert
        menge = verfuegbar
        con.execute("UPDATE dbo.TSF_WT SET WT_STATUS_ID='C_TFRTG', QTY_CONFIRMED_GUT=? WHERE WT_ID=?",
                    (gut + menge, wt_id))
        if not ohne:
            _menge_buchen(con, wt_id, menge, t)
            _event(con, wt_id, res_id, "C_TFRTG", t)
        return
    ausschuss = 1 if (rest >= 5 and rnd.random() < 0.15) else 0
    con.execute("UPDATE dbo.TSF_WT SET WT_STATUS_ID='C_FRTG', QTY_CONFIRMED_GUT=?, QTY_CONFIRMED_AUS=? "
                "WHERE WT_ID=?", (gut + rest - ausschuss, (aus or 0) + ausschuss, wt_id))
    if not ohne:
        _menge_buchen(con, wt_id, rest - ausschuss, t)
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
    p.add_argument("--tage", type=float, default=None, help="Historie in Tagen (statt --stunden)")
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args(argv)
    if a.befehl == "init":
        init(a.pfad, a.auftraege, a.tage * 24 if a.tage else a.stunden, a.seed)
    else:
        tick(a.pfad, a.anzahl)
    print("ok")


if __name__ == "__main__":
    main()
