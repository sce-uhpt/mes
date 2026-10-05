"""Tests der reinen Transportlogik (ohne Datenbank)."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from mes_sync.rules import transport_noetig
from mes_sync.transport import build_kette, build_transporte, finde_auto_erledigt

T0 = datetime(2026, 10, 5, 6, 0, 0)


def vorgang(wt, order, afo, wc, res, status="C_FREI", soll=10, gut=0, typ="C_MACH"):
    return dict(wt_id=wt, pps_order=order, afo_nr=afo, vorgang_text=f"Vorgang {afo}",
                wt_status_id=status, work_cntr=wc, plan_res_id=f"R{wc}", plan_res=res,
                plan_res_typ=typ, qty_soll=soll, qty_gut=gut, material_nr="4711",
                material_text="Ventilkörper", werk="80", psp="P-1")


def event(rid, wt, typ, minuten=0, order="A1", ist="Maschine X"):
    return dict(rueck_id=rid, wt_id=wt, rueck_ts=T0 + timedelta(minutes=minuten),
                rueck_type_id=typ, wkpl_res_id="W", ist_res=ist, ist_res_typ="C_MACH",
                pps_order=order, afo_nr=None)


@pytest.fixture
def plan():
    return pd.DataFrame([
        vorgang("w10", "A1", "0010", "100", "100 Sägen", status="C_FRTG", gut=10),
        vorgang("w20", "A1", "0020", "321", "321-22 Weiler E40", status="C_TFRTG", gut=4),
        vorgang("w30", "A1", "0030", "321", "321-22 Weiler E40"),           # gleicher Arbeitsplatz
        vorgang("w40", "A1", "0040", "235", "235 Maßkontrolle"),
        vorgang("w50", "A1", "0050", "168", "VERSAND", typ="C_WKPL"),
    ])


LEER_B = pd.DataFrame(columns=["von_wt_id", "quelle_key", "menge", "status"])
LEER_AP = pd.DataFrame(columns=["work_cntr", "sektor", "transport_modus"])


def run(ev, plan, bisher=LEER_B, ap=LEER_AP, modus="teil", regel="arbeitsplatz"):
    return build_transporte(pd.DataFrame(ev), plan, bisher, ap, modus, regel)


def test_fertigmeldung_erzeugt_transport_zum_naechsten_arbeitsplatz(plan):
    r = run([event("r1", "w10", "C_FRTG")], plan)
    t = r.transporte
    assert len(t) == 1
    row = t.iloc[0]
    assert (row.von_res, row.nach_res) == ("100 Sägen", "321-22 Weiler E40")
    assert (row.von_afo, row.nach_afo) == ("0010", "0020")
    assert row.menge == 10 and not row.ist_teilmenge
    assert row.quelle_key == "T|r1"


def test_teilmeldung_im_teilmodus_mit_menge(plan):
    t = run([event("r2", "w20", "C_TFRTG")], plan).transporte
    # w20 -> w30 ist derselbe Arbeitsplatz -> kein Transport
    assert t.empty


def test_gleicher_arbeitsplatz_kein_transport_aber_bei_regel_alle(plan):
    assert run([event("r2", "w20", "C_TFRTG")], plan).statistik["kein_transport_noetig"] == 1
    t = run([event("r2", "w20", "C_TFRTG")], plan, regel="alle").transporte
    assert len(t) == 1 and t.iloc[0].menge == 4 and t.iloc[0].ist_teilmenge


def test_voll_modus_ignoriert_teilmeldung(plan):
    assert run([event("r1", "w10", "C_TFRTG")], plan, modus="voll").transporte.empty
    t = run([event("r1", "w10", "C_FRTG")], plan, modus="voll").transporte
    assert t.iloc[0].quelle_key == "V|w10"


def test_modus_je_arbeitsplatz_ueberschreibt_standard(plan):
    ap = pd.DataFrame([{"work_cntr": "100", "sektor": None, "transport_modus": "voll"}])
    assert run([event("r1", "w10", "C_TFRTG")], plan, ap=ap, modus="teil").transporte.empty


def test_letzter_vorgang_kein_transport(plan):
    p = plan.copy()
    r = run([event("r9", "w50", "C_FRTG")], p)
    assert r.transporte.empty and r.statistik["letzter_vorgang"] == 1


def test_menge_ist_differenz_zu_bereits_verteilten_transporten(plan):
    p = plan.copy()
    p.loc[p.wt_id == "w10", "qty_gut"] = 10
    bisher = pd.DataFrame([{"von_wt_id": "w10", "quelle_key": "T|r0", "menge": 6.0, "status": "erledigt"}])
    t = run([event("r1", "w10", "C_TFRTG")], p, bisher=bisher).transporte
    assert t.iloc[0].menge == 4


def test_teilmeldung_ohne_neue_menge_wird_uebersprungen(plan):
    bisher = pd.DataFrame([{"von_wt_id": "w10", "quelle_key": "T|r0", "menge": 10.0, "status": "offen"}])
    r = run([event("r1", "w10", "C_TFRTG")], plan, bisher=bisher)
    assert r.transporte.empty and r.statistik["keine_neue_menge"] == 1


def test_fertigmeldung_ohne_restmenge_wird_trotzdem_transportiert(plan):
    bisher = pd.DataFrame([{"von_wt_id": "w10", "quelle_key": "T|r0", "menge": 10.0, "status": "offen"}])
    t = run([event("r1", "w10", "C_FRTG")], plan, bisher=bisher).transporte
    assert len(t) == 1 and pd.isna(t.iloc[0].menge)


def test_stornierte_transporte_zaehlen_nicht_als_verteilt(plan):
    bisher = pd.DataFrame([{"von_wt_id": "w10", "quelle_key": "T|r0", "menge": 10.0, "status": "storniert"}])
    t = run([event("r1", "w10", "C_FRTG")], plan, bisher=bisher).transporte
    assert t.iloc[0].menge == 10


def test_mehrere_meldungen_im_selben_lauf_ergeben_einen_transport(plan):
    ev = [event("r1", "w10", "C_TFRTG", 0), event("r2", "w10", "C_FRTG", 5)]
    t = run(ev, plan).transporte
    assert len(t) == 1 and t.iloc[0].rueck_id == "r2" and not t.iloc[0].ist_teilmenge


def test_voll_transport_wird_nicht_doppelt_angelegt(plan):
    bisher = pd.DataFrame([{"von_wt_id": "w10", "quelle_key": "V|w10", "menge": 10.0, "status": "offen"}])
    r = run([event("r1", "w10", "C_FRTG")], plan, bisher=bisher, modus="voll")
    assert r.transporte.empty and r.statistik["schon_vorhanden"] == 1


def test_nachfolger_schon_fertig_kein_transport(plan):
    p = plan.copy()
    p.loc[p.wt_id == "w20", "wt_status_id"] = "C_FRTG"
    r = run([event("r1", "w10", "C_FRTG")], p)
    assert r.transporte.empty and r.statistik["nachfolger_schon_fertig"] == 1


def test_start_und_rueck_events_loesen_nichts_aus(plan):
    ev = [event("r1", "w10", "C_START"), event("r2", "w10", "C_RUECK")]
    assert run(ev, plan).transporte.empty


def test_unbekannter_vorgang_wird_gezaehlt(plan):
    r = run([event("rx", "w999", "C_FRTG")], plan)
    assert r.transporte.empty and r.statistik["ohne_vorgang"] == 1


def test_afo_luecken_und_doppelte_afo():
    p = pd.DataFrame([
        vorgang("a", "B", "0010", "1", "R1", gut=1),
        vorgang("b2", "B", "0050", "5", "R5"),
        vorgang("b1", "B", "0050", "5", "R5"),  # Split-Duplikat
        vorgang("c", "B", "0180", "9", "R9"),
    ])
    k = build_kette(p).set_index("wt_id")
    assert k.loc["a", "nach_wt_id"] == "b1"      # kleinste WT_ID gewinnt, Luecke 10->50 egal
    assert k.loc["b1", "nach_wt_id"] == "c"


def test_sektorregel_hat_vorrang_vor_fallback():
    assert transport_noetig("1", "2", "S1", "S1") is False      # gleicher Sektor
    assert transport_noetig("1", "1", "S1", "S2") is True       # Sektorwechsel
    assert transport_noetig("1", "1", "S1", None) is False      # Fallback: gleicher AP
    assert transport_noetig("1", "2", None, None) is True


def test_sektoren_aus_stammdaten(plan):
    ap = pd.DataFrame([
        {"work_cntr": "100", "sektor": "Säge", "transport_modus": None},
        {"work_cntr": "321", "sektor": "Säge", "transport_modus": None},
    ])
    assert run([event("r1", "w10", "C_FRTG")], plan, ap=ap).transporte.empty


# ── Auto-Erledigung ──────────────────────────────────────────────────────────

def test_auto_erledigt_bei_start_am_folgevorgang_nach_dem_ausloeser():
    offene = pd.DataFrame([{"id": 1, "nach_wt_id": "w20", "rueck_ts": T0},
                           {"id": 2, "nach_wt_id": "w40", "rueck_ts": T0}])
    ev = pd.DataFrame([
        {"wt_id": "w20", "rueck_ts": T0 + timedelta(minutes=20), "rueck_type_id": "C_START"},
        {"wt_id": "w40", "rueck_ts": T0 - timedelta(minutes=5), "rueck_type_id": "C_START"},  # davor
        {"wt_id": "w40", "rueck_ts": T0 + timedelta(minutes=9), "rueck_type_id": "C_TFRTG"},  # zaehlt nicht
    ])
    r = finde_auto_erledigt(offene, ev)
    assert list(r["id"]) == [1]
    assert r.iloc[0].erledigt_ts == T0 + timedelta(minutes=20)


def test_auto_erledigt_wenn_folgevorgang_fertig():
    offene = pd.DataFrame([{"id": 7, "nach_wt_id": "w30", "rueck_ts": T0}])
    status = pd.DataFrame([{"wt_id": "w30", "wt_status_id": "C_FRTG"}])
    r = finde_auto_erledigt(offene, pd.DataFrame(columns=["wt_id", "rueck_ts", "rueck_type_id"]), status)
    assert list(r["id"]) == [7]
