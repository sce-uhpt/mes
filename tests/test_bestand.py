"""Bestandsmonitor: reine Logik + End-to-End gegen das simulierte Proxia."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import text

from mes_sync import bestand as B
from mes_sync import bestand_lauf, state
from mes_sync.config import Settings
from mes_sync.db import _sqlite_engine
from mes_sync.runner import run_bestand, run_once
from mes_sync.tools import fake_proxia

T0 = datetime(2026, 10, 5, 6, 0, 0)


def vorgang(wt, afo, wc, soll=10, gut=0, aus=0, status="C_FREI", order="A1", typ="C_MACH"):
    return dict(wt_id=wt, pps_order=order, afo_nr=afo, vorgang_text=f"V{afo}", wt_status_id=status,
                work_cntr=wc, plan_res=f"AP {wc}", plan_res_typ=typ, qty_soll=soll, qty_gut=gut,
                qty_aus=aus, qty_nach=0, material_nr="4711", material_text="Ventilkörper", psp="P-1",
                einheit="C_STK", conf_nr="0077", begin_scheduled=None)


@pytest.fixture
def plan():
    return pd.DataFrame([
        vorgang("w10", "0010", "100"),
        vorgang("w20", "0020", "321"),
        vorgang("w30", "0030", "321"),   # gleicher Arbeitsplatz wie w20 -> kein Transport
        vorgang("w40", "0040", "235"),
    ])


def gut(wt, menge, minuten, quelle="rueckmeldung"):
    return B.Ereignis(ts=T0 + timedelta(minutes=minuten), art="gut", wt_id=wt, menge=menge,
                      zeit_quelle=quelle, key=f"g|{wt}|{minuten}")


def tr(art, von, tid, minuten, menge=None, eid=0):
    return B.Ereignis(ts=T0 + timedelta(minutes=minuten), art=art, wt_id=von, menge=menge,
                      zeit_quelle="app", key=f"E|{eid}", transport_id=tid)


def summe(rows, von):
    out = {o: 0.0 for o in B.ORTE}
    for r in rows:
        if r["von_wt_id"] == von:
            out[r["ort"]] += r["menge"]
    return out


# ── reine Logik ──────────────────────────────────────────────────────────────

def test_kette_kennt_transportbedarf(plan):
    k = B.baue_kette(plan)
    assert k["w10"].nach == "w20" and k["w10"].ort_zugang == "bereit"
    assert k["w20"].ort_zugang == "an_b"          # 321 -> 321: kein Transport
    assert k["w40"].nach is None and k["w20"].vor == "w10"


def test_teilmengen_transport_und_verbrauch(plan):
    k = B.baue_kette(plan)
    ev = [gut("w10", 5, 0), tr("uebernommen", "w10", 1, 5, eid=1), tr("erledigt", "w10", 1, 9, eid=2),
          gut("w10", 5, 30),                       # zweite Teilmenge liegt noch bei A
          gut("w20", 3, 40)]                       # B verbraucht 3 aus dem, was an B liegt
    rows = B.wende_an(ev, k, B.Saldo())
    assert summe(rows, "w10") == {"bereit": 5.0, "unterwegs": 0.0, "an_b": 2.0}
    # w20 -> w30 ohne Transport: Gutmenge landet direkt an B
    assert summe(rows, "w20") == {"bereit": 0.0, "unterwegs": 0.0, "an_b": 3.0}


def test_transport_ohne_menge_nimmt_was_bereit_liegt(plan):
    k = B.baue_kette(plan)
    rows = B.wende_an([gut("w10", 7, 0), tr("uebernommen", "w10", 9, 1, menge=None, eid=1)], k, B.Saldo())
    assert summe(rows, "w10") == {"bereit": 0.0, "unterwegs": 7.0, "an_b": 0.0}


def test_zurueckgeben_bringt_ware_zurueck(plan):
    k = B.baue_kette(plan)
    s = B.Saldo()
    rows = B.wende_an([gut("w10", 4, 0), tr("uebernommen", "w10", 2, 1, 4, 1),
                       tr("zurueckgegeben", "w10", 2, 2, 4, 2)], k, s)
    assert summe(rows, "w10") == {"bereit": 4.0, "unterwegs": 0.0, "an_b": 0.0}
    assert 2 not in s.unterwegs_je_transport


def test_verbrauch_vor_zugang_wird_ausgeglichen(plan):
    """SAP-Meldung an A kommt spaeter als die Proxia-Meldung an B: keine Phantom-Minusbestaende."""
    k = B.baue_kette(plan)
    rows = B.wende_an([gut("w20", 4, 0), gut("w10", 4, 10, "erkennung")], k, B.Saldo())
    assert summe(rows, "w10") == {"bereit": 0.0, "unterwegs": 0.0, "an_b": 0.0}


def test_erledigt_nach_verbrauch_erzeugt_keinen_phantombestand(plan):
    k = B.baue_kette(plan)
    rows = B.wende_an([gut("w10", 5, 0), tr("uebernommen", "w10", 1, 1, 5, 1), gut("w20", 5, 2),
                       tr("erledigt", "w10", 1, 3, eid=2)], k, B.Saldo())
    assert summe(rows, "w10") == {"bereit": 0.0, "unterwegs": 0.0, "an_b": 0.0}


def test_storno_an_a_nimmt_zuerst_was_noch_da_ist(plan):
    k = B.baue_kette(plan)
    rows = B.wende_an([gut("w10", 5, 0), tr("uebernommen", "w10", 1, 1, 5, 1), tr("erledigt", "w10", 1, 2, eid=2),
                       gut("w10", -2, 3)], k, B.Saldo())
    assert summe(rows, "w10") == {"bereit": 0.0, "unterwegs": 0.0, "an_b": 3.0}


def test_fifo_mit_spaeter_sap_meldung():
    """B verbraucht 4, die SAP-Meldung an A kommt spaeter, dann 5 neue: liegt seit = neue Menge."""
    base = dict(von_wt_id="a", pps_order="o", nach_wt_id="b", ort="an_b")
    j = pd.DataFrame([
        dict(base, id=1, ts=T0, menge=-4, art="verbrauch", zeit_quelle="rueckmeldung"),
        dict(base, id=2, ts=T0 + timedelta(minutes=10), menge=4, art="zugang", zeit_quelle="erkennung"),
        dict(base, id=3, ts=T0 + timedelta(minutes=600), menge=5, art="zugang", zeit_quelle="rueckmeldung"),
    ])
    p = B.berechne_puffer(j).iloc[0]
    assert p.menge_gesamt == 5 and p.liegt_seit == T0 + timedelta(minutes=600) and not p.zeit_geschaetzt


def test_puffer_fifo_liegezeit():
    j = pd.DataFrame([
        dict(id=1, von_wt_id="a", pps_order="o", nach_wt_id="b", ts=T0, ort="bereit", menge=5,
             art="zugang", zeit_quelle="rueckmeldung"),
        dict(id=2, von_wt_id="a", pps_order="o", nach_wt_id="b", ts=T0 + timedelta(hours=2), ort="bereit",
             menge=5, art="zugang", zeit_quelle="erkennung"),
        dict(id=3, von_wt_id="a", pps_order="o", nach_wt_id="b", ts=T0 + timedelta(hours=3), ort="bereit",
             menge=-5, art="verbrauch", zeit_quelle="rueckmeldung"),
    ])
    p = B.berechne_puffer(j).iloc[0]
    assert p.menge_gesamt == 5 and p.liegt_seit == T0 + timedelta(hours=2) and p.zeit_geschaetzt


def test_fehlbuchung_und_art():
    assert B.ist_fehlbuchung(2147483648, 4) and not B.ist_fehlbuchung(12, 10)
    assert B.art_vorschlag("10000145", "FF Anstrich", "C_DELIVERER", set()) == "extern"
    assert B.art_vorschlag("10000168", "VERSAND", "C_WKPL", {"10000168"}) == "pseudo"
    assert B.art_vorschlag("10000475", "DO_100-30 Sägen", "C_WKPL", set()) == "fremd_standort"
    assert B.art_vorschlag("10000223", "235 Maßkontrolle", "C_WKPL", set()) == "intern"


def test_live_erkennt_aenderung_ohne_rueckmeldung(plan):
    alt = plan.assign(plausi=None)
    neu = plan.copy()
    neu.loc[neu.wt_id == "w10", "qty_gut"] = 10
    neu.loc[neu.wt_id == "w20", "qty_gut"] = 2147483648  # Fehlbuchung wird ignoriert
    meld = pd.DataFrame(columns=["wt_id", "rueck_ts", "rueck_type_id"])
    ev, v = B.live_ereignisse(alt, neu, meld, T0, "x")
    assert [(e.wt_id, e.menge, e.zeit_quelle) for e in ev] == [("w10", 10.0, "erkennung")]
    assert v.set_index("wt_id").at["w20", "plausi"].startswith("Fehlbuchung")


# ── End-to-End ───────────────────────────────────────────────────────────────

SETTINGS = Settings(poll_sekunden=30, standard_modus="teil", fallback_regel="arbeitsplatz",
                    backfill_stunden=12, ueberlappung_minuten=10, bestand_tage=10)


def q(engine, sql):
    with engine.connect() as con:
        return pd.read_sql(text(sql), con)


def erwartet_vs_ist(proxia, sce):
    """Bestand je Puffer muss exakt Gut(A) - Gut(B) - Ausschuss(B) aus TSF_WT sein."""
    wt = q(proxia, "SELECT WT_ID wt_id, PPS_ORDER o, AFO_NR afo, QTY_SOLL soll, QTY_CONFIRMED_GUT gut, "
                   "QTY_CONFIRMED_AUS aus FROM dbo.TSF_WT WHERE WT_DELETED = 0").sort_values(["o", "afo"])
    wt["nach"] = wt.groupby("o")["wt_id"].shift(-1)
    bv = q(sce, "SELECT wt_id, qty_gut, abgeschlossen FROM sce_mes.bestand_vorgang").set_index("wt_id")
    pu = q(sce, "SELECT von_wt_id, menge_gesamt FROM sce_mes.bestand_puffer").set_index("von_wt_id")
    info = wt.set_index("wt_id")
    abw, n = [], 0
    for w, r in info.iterrows():
        if pd.isna(r.nach) or w not in bv.index or bv.at[w, "abgeschlossen"]:
            continue
        soll = bv.at[w, "qty_gut"] - bv.at[r.nach, "qty_gut"] - (info.at[r.nach, "aus"] or 0)
        ist = pu.at[w, "menge_gesamt"] if w in pu.index else 0.0
        n += 1
        if abs(soll - ist) > 1e-6:
            abw.append((r.o, r.afo, soll, ist))
    return n, abw


def app(sce, aktion, tid, fahrer="Max"):
    ts = state.utcnow()
    upd = {"uebernehmen": ("uebernommen", "status='offen'"), "erledigt": ("erledigt", "status='uebernommen'"),
           "zurueckgeben": ("zurueckgegeben", "status='uebernommen'")}[aktion]
    neu = {"uebernehmen": "uebernommen", "erledigt": "erledigt", "zurueckgeben": "offen"}[aktion]
    with sce.begin() as con:
        n = con.execute(text(f"UPDATE sce_mes.transport_auftrag SET status=:s, geaendert_am=:t "
                             f"WHERE id=:i AND {upd[1]}"), {"s": neu, "t": ts, "i": tid}).rowcount
        if n:
            con.execute(text("INSERT INTO sce_mes.transport_event (transport_id, event, fahrer, ts) "
                             "VALUES (:i, :e, :f, :t)"), {"i": tid, "e": upd[0], "f": fahrer, "t": ts})
    return n


@pytest.fixture
def umgebung(tmp_path):
    proxia_file = tmp_path / "proxia.sqlite"
    fake_proxia.init(str(proxia_file), auftraege=50, stunden=24 * 16, seed=11)
    proxia = _sqlite_engine(f"sqlite:///{proxia_file}", attach_schema="dbo")
    sce = _sqlite_engine(f"sqlite:///{tmp_path / 'sce.sqlite'}", attach_schema="sce_mes")
    state.init_db(sce)
    return proxia_file, proxia, sce


def test_erstlauf_stimmt_mit_proxia_ueberein(umgebung):
    _, proxia, sce = umgebung
    run_once(SETTINGS, proxia, sce)
    s = run_bestand(SETTINGS, proxia, sce)
    assert s["modus"] == "erstlauf" and s["bewegungen"] > 0
    n, abw = erwartet_vs_ist(proxia, sce)
    assert n > 20 and abw == []
    # Fehlbuchung erkannt, Arbeitsplatz-Art vorgeschlagen, Verlauf vorhanden
    assert q(sce, "SELECT COUNT(*) n FROM sce_mes.bestand_vorgang WHERE plausi LIKE 'Fehlbuchung%'").n[0] == 1
    art = q(sce, "SELECT work_cntr, art FROM sce_mes.arbeitsplatz").set_index("work_cntr")["art"]
    assert art.get("10000145") == "extern" and art.get("10000168") == "pseudo"
    assert len(q(sce, "SELECT * FROM sce_mes.bestand_tag")) > 0
    # Vorgaenge ohne Proxia-Rueckmeldung wurden mit geschaetztem Zeitpunkt verbucht
    zq = q(sce, "SELECT DISTINCT zeit_quelle FROM sce_mes.wip_bewegung").zeit_quelle
    assert {"rueckmeldung", "fenster"} <= set(zq) or {"rueckmeldung", "tag"} <= set(zq)


def test_laufender_betrieb_mit_app_und_sap_meldungen(umgebung):
    proxia_file, proxia, sce = umgebung
    run_once(SETTINGS, proxia, sce)
    run_bestand(SETTINGS, proxia, sce)
    leer = run_bestand(SETTINGS, proxia, sce)
    assert leer["bewegungen"] == 0  # nichts passiert -> nichts gebucht

    offen = q(sce, "SELECT id, von_wt_id FROM sce_mes.transport_auftrag WHERE status='offen' ORDER BY id")
    assert len(offen) >= 2
    t1, t2 = int(offen.id[0]), int(offen.id[1])
    assert app(sce, "uebernehmen", t1) and app(sce, "uebernehmen", t2) and app(sce, "erledigt", t2)
    for i in range(12):
        fake_proxia.tick(str(proxia_file), anzahl=4, seed=200 + i)
    run_once(SETTINGS, proxia, sce)
    s = run_bestand(SETTINGS, proxia, sce)
    assert s["transport_events"] >= 3 and s["mengenaenderungen"] > 0  # inkl. auto_erledigt
    assert s["abgleich_korrekturen"] == 0  # Einzelbuchungen erklaeren alles
    n, abw = erwartet_vs_ist(proxia, sce)
    assert abw == []
    orte = q(sce, f"SELECT von_wt_id, menge_unterwegs, menge_an_b FROM sce_mes.bestand_puffer "
                  f"WHERE von_wt_id IN ('{offen.von_wt_id[0]}', '{offen.von_wt_id[1]}')").set_index("von_wt_id")
    # Transport 1 ist unterwegs (sofern B noch nicht alles verbraucht hat), Transport 2 an B oder verbraucht
    assert (orte["menge_unterwegs"] >= 0).all() and (orte["menge_an_b"] >= 0).all()


def test_geaenderter_arbeitsplan_wird_abgeglichen(umgebung):
    proxia_file, proxia, sce = umgebung
    run_once(SETTINGS, proxia, sce)
    run_bestand(SETTINGS, proxia, sce)
    # Vorgang in einen laufenden Auftrag einfuegen (mit Gutmenge) und einen anderen loeschen
    with proxia.begin() as con:
        o = con.execute(text("SELECT w.PPS_ORDER FROM dbo.TSF_WT w WHERE w.WT_STATUS_ID='C_FRTG' AND EXISTS "
                             "(SELECT 1 FROM dbo.TSF_WT v WHERE v.PPS_ORDER=w.PPS_ORDER AND v.WT_STATUS_ID<>'C_FRTG') "
                             "ORDER BY w.PPS_ORDER LIMIT 1")).scalar()
        con.execute(text("INSERT INTO dbo.TSF_WT (WT_ID, PPS_ORDER, AFO_NR, DISPLAYNAME, WT_STATUS_ID, PPS_WORK_CNTR, "
                         "QTY_SOLL, QTY_CONFIRMED_GUT, WT_DELETED, QTY_CONFIRMED_AUS) "
                         "VALUES ('neu1', :o, '0015', 'Zwischenpruefung', 'C_TFRTG', '10000223', 50, 1, 0, 0)"),
                    {"o": o})
        letzter = con.execute(text("SELECT WT_ID FROM dbo.TSF_WT WHERE PPS_ORDER=:o AND WT_STATUS_ID<>'C_FRTG' "
                                   "ORDER BY AFO_NR DESC LIMIT 1"), {"o": o}).scalar()
        con.execute(text("UPDATE dbo.TSF_WT SET WT_DELETED=1 WHERE WT_ID=:w"), {"w": letzter})
    s = run_bestand(SETTINGS, proxia, sce)
    assert s["abgleich_korrekturen"] > 0
    assert erwartet_vs_ist(proxia, sce)[1] == []
    assert run_bestand(SETTINGS, proxia, sce)["abgleich_korrekturen"] == 0  # danach stabil


def test_neuer_auftrag_und_reset(umgebung):
    proxia_file, proxia, sce = umgebung
    kurz = replace(SETTINGS, bestand_tage=1)
    run_once(kurz, proxia, sce)
    s1 = run_bestand(kurz, proxia, sce)
    for i in range(25):
        fake_proxia.tick(str(proxia_file), anzahl=4, seed=300 + i)
    run_once(kurz, proxia, sce)
    s2 = run_bestand(kurz, proxia, sce)
    assert s2["neue_auftraege"] >= 0
    assert erwartet_vs_ist(proxia, sce)[1] == []
    bestand_lauf.zuruecksetzen(sce)
    s3 = run_bestand(kurz, proxia, sce)
    assert s3["modus"] == "erstlauf" and erwartet_vs_ist(proxia, sce)[1] == []
    assert s1["auftraege"] <= s3["auftraege"] + s2["neue_auftraege"] + 50


def test_backfill_ohne_mengenbuchungen(plan):
    """Auftrag nur mit SAP-Meldungen: keine TSF_WT_QTY-Zeilen, keine Rueckmeldungen."""
    v = plan.copy()
    v.loc[v.wt_id == "w10", ["qty_gut", "wt_status_id"]] = [10, "C_FRTG"]
    leer_q = pd.DataFrame(columns=["wt_id", "cal_day", "shift_id", "qty"])
    leer_r = pd.DataFrame(columns=["rueck_id", "wt_id", "rueck_ts", "rueck_type_id"])
    k = B.baue_kette(v)
    vb = B.backfill(v, leer_q, leer_r, k, T0 - timedelta(days=5), T0)
    rows = B.wende_an(vb.ereignisse, k, B.Saldo())
    assert summe(rows, "w10")["bereit"] == 10.0
