"""Reine Logik des Bestandsmonitors (Ware in Arbeit) — ohne DB, mit DataFrames testbar.

Begriffe
--------
Puffer      Uebergang von Vorgang A (von_wt_id) zu seinem Nachfolger B (naechsthoehere AFO).
            Bestand = Gutmenge(A) - Verbrauch(B), Verbrauch = Gut + Ausschuss an B.
Ort         Transporte teilen den Puffer auf: bereit (liegt bei A) -> unterwegs -> an_b.
            Braucht A->B keinen Transport (gleicher Arbeitsplatz/Sektor), landet die Ware
            direkt in an_b.
Journal     Jede Mengenaenderung ist eine Zeile in sce_mes.wip_bewegung. Bestand je
            Puffer und Ort = Summe der Zeilen. Korrekturen sind Gegenbuchungen.

Zeitpunkte (zeit_quelle)
------------------------
rueckmeldung  Teil-/Fertigmeldung in Proxia (exakt)
tag           Gutmenge aus TSF_WT_QTY, Uhrzeit nur ueber den Tag bekannt
erkennung     Gutmenge hat sich in TSF_WT ohne Proxia-Rueckmeldung geaendert (vermutlich
              SAP); Zeitpunkt = wann der Poller es gesehen hat
fenster       rueckwirkend, keine Rueckmeldung: spaetestens beim Start des Nachfolgers
init          Anfangsbestand zu Beginn des nachgeladenen Zeitfensters
app           Transport-App (Uebernehmen / Erledigt)
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import pandas as pd

from .rules import transport_noetig
from .transport import build_kette

TZ = "Europe/Berlin"
ORTE = ("bereit", "unterwegs", "an_b")
MELDE_TYPEN = ("C_TFRTG", "C_FRTG")
NICHT_GESTARTET = ("C_FREI", "C_NEU", "")

EPS = 1e-9


# ── Plausibilitaet ───────────────────────────────────────────────────────────

def mengen_grenze(soll) -> float:
    """Ab hier gilt eine Menge als Fehlbuchung (z. B. 2^31 oder eine gescannte Nummer).

    Ueberlieferung ist erlaubt, aber nicht das Zehnfache und mehr als 1.000 darueber.
    """
    s = float(soll) if soll is not None and pd.notna(soll) and float(soll) > 0 else 1.0
    return max(10 * s, s + 1000)


def ist_fehlbuchung(menge, soll) -> bool:
    return menge is not None and pd.notna(menge) and float(menge) > mengen_grenze(soll)


# ── Arbeitsplatz-Art ─────────────────────────────────────────────────────────

def art_vorschlag(work_cntr, bezeichnung, res_typ, pseudo: set[str]) -> str:
    """Vorschlag fuer arbeitsplatz.art, solange dort nichts gepflegt ist."""
    wc = str(work_cntr or "").strip()
    name = str(bezeichnung or "").strip().upper()
    if name.startswith("DO_") or name.startswith("DO "):
        return "fremd_standort"
    if wc in pseudo:
        return "pseudo"
    if str(res_typ or "") == "C_DELIVERER":
        return "extern"
    return "intern"


# ── Kette ────────────────────────────────────────────────────────────────────

@dataclass
class Glied:
    wt_id: str
    pps_order: str
    work_cntr: str | None
    vor: str | None
    nach: str | None
    nach_work_cntr: str | None
    ort_zugang: str  # 'bereit' (Transport noetig) oder 'an_b'


def ist_ausgeschlossen(pps_order, praefixe) -> bool:
    """Auftraege, die nicht in den Bestand gehoeren (z. B. IH = Instandhaltung, Soll 0, ohne Arbeitsplatz)."""
    o = str(pps_order or "").strip().upper()
    return any(o.startswith(p) for p in praefixe)


def baue_kette(vorgaenge: pd.DataFrame, arbeitsplatz: pd.DataFrame | None = None,
               fallback_regel: str = "arbeitsplatz", transport_ort: bool = True) -> dict[str, Glied]:
    """Vorgangskette je Auftrag.

    transport_ort=False (Push ohne Transport-App): Zugaenge liegen sofort vor B ('an_b').
    transport_ort=True: bei Uebergaengen mit Transport erst 'bereit' bis zur Fahrer-Quittung.
    """
    if vorgaenge.empty:
        return {}
    k = build_kette(vorgaenge)
    k["vor_wt_id"] = k.groupby("pps_order", sort=False)["wt_id"].shift(1)
    sektor = {}
    if arbeitsplatz is not None and not arbeitsplatz.empty and "sektor" in arbeitsplatz:
        sektor = arbeitsplatz.set_index("work_cntr")["sektor"].dropna().to_dict()
    out = {}
    for r in k.itertuples(index=False):
        nach = r.nach_wt_id if pd.notna(r.nach_wt_id) else None
        noetig = transport_noetig(r.work_cntr, r.nach_work_cntr, sektor.get(r.work_cntr),
                                  sektor.get(r.nach_work_cntr), fallback_regel) if nach else True
        out[r.wt_id] = Glied(
            wt_id=r.wt_id, pps_order=r.pps_order,
            work_cntr=r.work_cntr if pd.notna(r.work_cntr) else None,
            vor=r.vor_wt_id if pd.notna(r.vor_wt_id) else None,
            nach=nach,
            nach_work_cntr=r.nach_work_cntr if pd.notna(r.nach_work_cntr) else None,
            ort_zugang="bereit" if (noetig and transport_ort) else "an_b",
        )
    return out


# ── Ereignisse und Journal ───────────────────────────────────────────────────

@dataclass
class Ereignis:
    ts: datetime
    art: str          # gut | aus | init | uebernommen | zurueckgegeben | erledigt | abgleich | differenz | abschluss
    wt_id: str        # gut/aus: Vorgang mit der Buchung; init/Transport/abschluss: Von-Vorgang A
    menge: float | None
    zeit_quelle: str
    key: str
    transport_id: int | None = None
    ort: str | None = None  # nur init: Zielort
    info: str | None = None

    def sortkey(self):
        # Transporte nach Mengenbuchungen gleichen Zeitpunkts
        rang = {"init": 0, "gut": 1, "aus": 2, "uebernommen": 4, "zurueckgegeben": 5,
                "erledigt": 6, "abgleich": 8, "differenz": 8, "abschluss": 9}.get(self.art, 7)
        return (pd.Timestamp(self.ts), rang, self.key)


@dataclass
class Saldo:
    """Laufender Bestand je Puffer und Ort, plus was je Transport gerade unterwegs ist."""
    orte: dict = field(default_factory=lambda: defaultdict(lambda: {o: 0.0 for o in ORTE}))
    unterwegs_je_transport: dict = field(default_factory=dict)

    def gesamt(self, von) -> float:
        return sum(self.orte[von].values())


def _zeile(e: Ereignis, g: Glied, kette, ort, menge, art, suffix) -> dict:
    nach = kette.get(g.nach) if g.nach else None
    return {
        "quelle_key": f"{e.key}|{suffix}"[:100],
        "ts": pd.Timestamp(e.ts).to_pydatetime(),
        "pps_order": g.pps_order,
        "von_wt_id": g.wt_id,
        "nach_wt_id": g.nach,
        "von_work_cntr": g.work_cntr,
        "nach_work_cntr": nach.work_cntr if nach else g.nach_work_cntr,
        "ort": ort,
        "menge": float(menge),
        "art": art,
        "zeit_quelle": e.zeit_quelle,
        "transport_id": e.transport_id,
        "info": e.info,
    }


def _verbrauch(e: Ereignis, b: Glied, kette, saldo: Saldo, menge: float, art: str) -> list[dict]:
    """Verbrauch an B aus dem Puffer seines Vorgaengers: erst an_b, dann unterwegs, dann bereit."""
    if not b.vor or b.vor not in kette or abs(menge) < EPS:
        return []
    a = kette[b.vor]
    s = saldo.orte[a.wt_id]
    rows = []
    if menge < 0:  # Storno einer Buchung an B -> Ware wieder vor B
        s["an_b"] += -menge
        return [_zeile(e, a, kette, "an_b", -menge, art, "v:an_b")]
    rest = menge
    for ort in ("an_b", "unterwegs", "bereit"):
        nimm = min(rest, max(s[ort], 0.0))
        if nimm > EPS:
            s[ort] -= nimm
            rows.append(_zeile(e, a, kette, ort, -nimm, art, f"v:{ort}"))
            rest -= nimm
        if rest <= EPS:
            break
    if rest > EPS:  # B verbraucht mehr als bekannt: Fehlmenge dort, wo der Zugang landen wird
        ort = a.ort_zugang
        s[ort] -= rest
        rows.append(_zeile(e, a, kette, ort, -rest, art, f"v:{ort}:f"))
    return rows


def _zugang(e: Ereignis, g: Glied, kette, saldo: Saldo, menge: float, art: str) -> list[dict]:
    """Menge in den Puffer von A legen (>0) oder herausnehmen (<0, z. B. Storno an A)."""
    s = saldo.orte[g.wt_id]
    rows, rest = [], menge
    if menge > 0:  # B hat schon mehr verbraucht als bekannt war: Fehlmenge zuerst auffuellen
        for ort in ORTE:
            if s[ort] < -EPS and rest > EPS:
                fuell = min(rest, -s[ort])
                s[ort] += fuell
                rows.append(_zeile(e, g, kette, ort, fuell, art, f"z:{ort}"))
                rest -= fuell
    else:          # Entnahme: erst was noch bei A liegt, dann unterwegs, dann an B — nie unter 0
        rest = -menge
        for ort in ("bereit", "unterwegs", "an_b"):
            nimm = min(rest, max(s[ort], 0.0))
            if nimm > EPS:
                s[ort] -= nimm
                rows.append(_zeile(e, g, kette, ort, -nimm, art, f"z:{ort}"))
                rest -= nimm
        rest = -rest
    if abs(rest) > EPS:
        s[g.ort_zugang] += rest
        rows.append(_zeile(e, g, kette, g.ort_zugang, rest, art, "z"))
    return rows


def wende_an(ereignisse: list[Ereignis], kette: dict[str, Glied], saldo: Saldo) -> list[dict]:
    """Ereignisse chronologisch auf den Saldo anwenden; liefert die Journalzeilen."""
    rows: list[dict] = []
    for e in sorted(ereignisse, key=Ereignis.sortkey):
        g = kette.get(e.wt_id)
        if g is None:
            continue
        m = float(e.menge) if e.menge is not None and pd.notna(e.menge) else None

        if e.art == "gut" and m:
            rows += _verbrauch(e, g, kette, saldo, m, "verbrauch")
            if g.nach:  # Ausgang des letzten Vorgangs verfolgen wir nicht (Versand/Lager)
                rows += _zugang(e, g, kette, saldo, m, "zugang")

        elif e.art in ("abgleich", "differenz") and m and g.nach:
            rows += _zugang(e, g, kette, saldo, m, e.art)

        elif e.art == "aus" and m:
            rows += _verbrauch(e, g, kette, saldo, m, "ausschuss")

        elif e.art == "init" and m and g.nach:
            ort = e.ort or g.ort_zugang
            saldo.orte[g.wt_id][ort] += m
            rows.append(_zeile(e, g, kette, ort, m, "init", "i"))

        elif e.art == "abschluss" and g.nach:
            for ort in ORTE:
                rest = saldo.orte[g.wt_id][ort]
                if abs(rest) > EPS:
                    saldo.orte[g.wt_id][ort] = 0.0
                    rows.append(_zeile(e, g, kette, ort, -rest, "abschluss", f"a:{ort}"))

        elif e.art == "uebernommen" and g.nach:
            s = saldo.orte[g.wt_id]
            menge = m if m is not None else max(s["bereit"], 0.0)
            saldo.unterwegs_je_transport[e.transport_id] = menge
            if menge > EPS:
                s["bereit"] -= menge
                s["unterwegs"] += menge
                rows.append(_zeile(e, g, kette, "bereit", -menge, "transport", "t:bereit"))
                rows.append(_zeile(e, g, kette, "unterwegs", menge, "transport", "t:unterwegs"))

        elif e.art in ("zurueckgegeben", "erledigt") and g.nach:
            s = saldo.orte[g.wt_id]
            if e.transport_id in saldo.unterwegs_je_transport:
                menge, quelle = saldo.unterwegs_je_transport.pop(e.transport_id), "unterwegs"
            else:  # Uebernahme lag vor dem Journal: direkt aus 'bereit'
                menge = m if m is not None else max(s["bereit"], 0.0)
                quelle = "bereit"
            # B kann die Ware schon verbraucht haben, bevor der Fahrer quittiert: nur bewegen,
            # was am Ort noch da ist (sonst Phantom-Bestand an B)
            menge = min(menge, max(s[quelle], 0.0))
            ziel = "bereit" if e.art == "zurueckgegeben" else "an_b"
            if menge > EPS and quelle != ziel:
                s[quelle] -= menge
                s[ziel] += menge
                rows.append(_zeile(e, g, kette, quelle, -menge, "transport", f"t:{quelle}"))
                rows.append(_zeile(e, g, kette, ziel, menge, "transport", f"t:{ziel}"))
    return rows


# ── Puffer aus dem Journal ───────────────────────────────────────────────────

PUFFER_COLS = ["von_wt_id", "pps_order", "nach_wt_id", "menge_bereit", "menge_unterwegs",
               "menge_an_b", "menge_gesamt", "liegt_seit", "letzte_bewegung", "zeit_geschaetzt"]


def berechne_puffer(journal: pd.DataFrame) -> pd.DataFrame:
    """Stand je Puffer aus seinen Journalzeilen.

    liegt_seit = Zugangszeitpunkt der aeltesten noch vorhandenen Einheit (FIFO),
    zeit_geschaetzt = dieser Zugang hat keinen exakten Zeitpunkt.
    Ein Durchlauf ueber alle Zeilen (schnell genug fuer den Erstlauf mit ~50k Puffern).
    """
    if journal.empty:
        return pd.DataFrame(columns=PUFFER_COLS)
    j = journal.sort_values(["von_wt_id", "ts", "id" if "id" in journal else "quelle_key"])
    summen = j.pivot_table(index="von_wt_id", columns="ort", values="menge", aggfunc="sum", fill_value=0.0)
    for o in ORTE:
        if o not in summen:
            summen[o] = 0.0
    kopf = j.groupby("von_wt_id", sort=False).agg(pps_order=("pps_order", "first"),
                                                  nach_wt_id=("nach_wt_id", "last"),
                                                  letzte_bewegung=("ts", "max"))
    fifo = {}
    aktuell, q, schuld = None, None, 0.0

    def ablegen():
        if aktuell is not None:
            fifo[aktuell] = (q[0][0], q[0][2]) if q else (None, False)

    for von, ts, menge, art, quelle in zip(j["von_wt_id"].to_numpy(), j["ts"].to_numpy(),
                                           j["menge"].to_numpy(dtype=float), j["art"].to_numpy(),
                                           j["zeit_quelle"].to_numpy()):
        if von != aktuell:
            ablegen()
            aktuell, q, schuld = von, deque(), 0.0
        if art == "transport":
            continue
        if menge > EPS:
            tilgen = min(schuld, menge)
            schuld -= tilgen
            if menge - tilgen > EPS:
                q.append([ts, menge - tilgen, quelle not in ("rueckmeldung", "app")])
        elif menge < -EPS:
            rest = -menge
            von_hinten = art in ("zugang", "init", "abgleich")  # Storno: juengste Menge zuerst
            while rest > EPS and q:
                idx = -1 if von_hinten else 0
                nimm = min(rest, q[idx][1])
                q[idx][1] -= nimm
                rest -= nimm
                if q[idx][1] <= EPS:
                    q.pop() if von_hinten else q.popleft()
            schuld += rest
    ablegen()

    out = kopf.join(summen[list(ORTE)]).reset_index()
    out = out.rename(columns={"bereit": "menge_bereit", "unterwegs": "menge_unterwegs", "an_b": "menge_an_b"})
    for c in ("menge_bereit", "menge_unterwegs", "menge_an_b"):
        out[c] = out[c].astype(float).round(6)
    out["menge_gesamt"] = (out["menge_bereit"] + out["menge_unterwegs"] + out["menge_an_b"]).round(6)
    voll = out["menge_gesamt"] > EPS
    out["liegt_seit"] = [pd.Timestamp(fifo[v][0]) if (ok and fifo[v][0] is not None) else None
                         for v, ok in zip(out["von_wt_id"], voll)]
    out["zeit_geschaetzt"] = [bool(fifo[v][1]) if ok else False for v, ok in zip(out["von_wt_id"], voll)]
    return out[PUFFER_COLS]


def saldo_aus_puffer(puffer: pd.DataFrame, unterwegs_je_transport: dict | None = None) -> Saldo:
    s = Saldo()
    for r in puffer.itertuples(index=False):
        s.orte[r.von_wt_id] = {"bereit": float(r.menge_bereit), "unterwegs": float(r.menge_unterwegs),
                               "an_b": float(r.menge_an_b)}
    s.unterwegs_je_transport = dict(unterwegs_je_transport or {})
    return s


# ── Tagesbestand (Verlauf) ───────────────────────────────────────────────────

def berechne_tagesbestand(journal: pd.DataFrame, tage: list) -> pd.DataFrame:
    """Bestand je Arbeitsplatz B am Ende jedes Kalendertags (Europe/Berlin).

    Liefert tag, work_cntr, anzahl_puffer (Puffer mit Bestand > 0), menge.
    """
    cols = ["tag", "work_cntr", "anzahl_puffer", "menge"]
    if journal.empty or not tage:
        return pd.DataFrame(columns=cols)
    j = journal[["von_wt_id", "nach_work_cntr", "ts", "menge"]].copy()
    j["tag"] = (pd.to_datetime(j["ts"]).dt.tz_localize("UTC").dt.tz_convert(TZ)
                .dt.tz_localize(None).dt.normalize())
    tage = sorted(pd.Timestamp(t).normalize() for t in tage)
    j.loc[j["tag"] < tage[0], "tag"] = tage[0]  # alles davor zaehlt in den ersten Tag
    j = j[j["tag"] <= tage[-1]]
    wc = j.groupby("von_wt_id")["nach_work_cntr"].last()
    d = j.groupby(["von_wt_id", "tag"])["menge"].sum().unstack("tag")
    d = d.reindex(columns=tage).fillna(0.0).cumsum(axis=1)
    d.columns = list(d.columns)
    pos = d.where(d > 1e-6)
    grp = pd.Index(wc.reindex(d.index).fillna("?").astype(str).to_numpy(), name="work_cntr")
    anz = pos.notna().groupby(grp).sum()
    men = pos.fillna(0).groupby(grp).sum()
    res = pd.DataFrame({
        "anzahl_puffer": anz.stack(),
        "menge": men.stack(),
    })
    res.index.names = ["work_cntr", "tag"]
    res = res.reset_index()
    res = res[res["anzahl_puffer"] > 0]
    res["tag"] = res["tag"].dt.date
    res["anzahl_puffer"] = res["anzahl_puffer"].astype(int)
    return res[cols]


# ── Nachladen (Backfill) ─────────────────────────────────────────────────────

def _lokal_mittag(tag) -> datetime:
    t = pd.Timestamp(datetime.combine(pd.Timestamp(tag).date(), time(12, 0))).tz_localize(TZ)
    return t.tz_convert("UTC").tz_localize(None).to_pydatetime()


def _lokaler_tag(ts: pd.Series) -> pd.Series:
    return (pd.to_datetime(ts).dt.tz_localize("UTC").dt.tz_convert(TZ)
            .dt.tz_localize(None).dt.normalize())


@dataclass
class Vorbereitung:
    ereignisse: list
    vorgaenge: pd.DataFrame  # mit qty_gut (wirksam), qty_gut_proxia, plausi, Meldezeiten


def wirksame_mengen(vorgaenge: pd.DataFrame, wt_qty: pd.DataFrame, rueck: pd.DataFrame) -> pd.DataFrame:
    """Gutmengen bereinigen und Meldezeitpunkte je Vorgang ergaenzen."""
    v = vorgaenge.copy()
    v["qty_gut_proxia"] = v["qty_gut"]
    v["plausi"] = None
    q = wt_qty.merge(v[["wt_id", "qty_soll"]], on="wt_id", how="inner")
    q["fehl"] = pd.Series([ist_fehlbuchung(m, s) for m, s in zip(q["qty"], q["qty_soll"])],
                          index=q.index, dtype=bool)
    gueltig = q[~q["fehl"]].groupby("wt_id")["qty"].sum()
    fehl = set(q.loc[q["fehl"], "wt_id"])
    for i, r in v.iterrows():
        if ist_fehlbuchung(r["qty_gut"], r["qty_soll"]) or r["wt_id"] in fehl:
            v.at[i, "plausi"] = "Fehlbuchung Gutmenge (Proxia {:,.0f})".format(
                r["qty_gut"] if pd.notna(r["qty_gut"]) else 0).replace(",", ".")
            if ist_fehlbuchung(r["qty_gut"], r["qty_soll"]):
                ersatz = float(gueltig.get(r["wt_id"], 0.0))
                if ersatz <= EPS and r.get("wt_status_id") == "C_FRTG":
                    ersatz = float(r["qty_soll"] or 0.0)  # fertig gemeldet: Soll ist die beste Schaetzung
                v.at[i, "qty_gut"] = ersatz
    for c in ("qty_gut", "qty_aus", "qty_nach"):
        v[c] = pd.to_numeric(v[c], errors="coerce").fillna(0.0)
    if not rueck.empty:
        r = rueck[rueck["wt_id"].isin(v["wt_id"])]
        erste = r.groupby("wt_id")["rueck_ts"].min()
        letzte = r[r["rueck_type_id"].isin(MELDE_TYPEN)].groupby("wt_id")["rueck_ts"].max()
        v["erste_meldung_ts"] = v["wt_id"].map(erste)
        v["letzte_meldung_ts"] = v["wt_id"].map(letzte)
    else:
        v["erste_meldung_ts"] = pd.NaT
        v["letzte_meldung_ts"] = pd.NaT
    return v


def backfill(vorgaenge: pd.DataFrame, wt_qty: pd.DataFrame, rueck: pd.DataFrame,
             kette: dict[str, Glied], fenster_start: datetime, jetzt: datetime) -> Vorbereitung:
    """Ereignisse fuer das Zeitfenster [fenster_start, jetzt] plus Anfangsbestand.

    Die Summe aller Ereignisse entspricht exakt dem heutigen Stand in TSF_WT;
    was vor dem Fenster passiert ist, steckt im Anfangsbestand (init).
    """
    v = wirksame_mengen(vorgaenge, wt_qty, rueck)
    info = v.set_index("wt_id")
    fs = pd.Timestamp(fenster_start)
    ereignisse: list[Ereignis] = []

    # Nachschlagetabellen (einmal gruppieren statt je Vorgang filtern)
    meld_tag: dict = {}
    frtg_max: dict = {}
    erstes: dict = {}
    if not rueck.empty:
        erstes = rueck.groupby("wt_id")["rueck_ts"].min().to_dict()
        meld = rueck[rueck["rueck_type_id"].isin(MELDE_TYPEN)].copy()
        if not meld.empty:
            meld["tag"] = _lokaler_tag(meld["rueck_ts"])
            g = meld.groupby(["wt_id", "tag"])["rueck_ts"].agg(["max", "count"])
            meld_tag = {k: (r["max"], int(r["count"])) for k, r in g.iterrows()}
            frtg_max = meld[meld["rueck_type_id"] == "C_FRTG"].groupby("wt_id")["rueck_ts"].max().to_dict()

    # 1) Gutmengen je Tag aus TSF_WT_QTY
    gebucht = defaultdict(float)   # je Vorgang: Summe gueltiger TSF_WT_QTY-Zeilen
    zeiten = defaultdict(list)     # je Vorgang: Zeitpunkte der Gutbuchungen
    q = wt_qty[wt_qty["wt_id"].isin(info.index)]
    if not q.empty:
        soll = info["qty_soll"].to_dict()
        q = q[pd.Series([not ist_fehlbuchung(m, soll.get(w)) for w, m in zip(q["wt_id"], q["qty"])],
                        index=q.index, dtype=bool)]
        tag_summe = q.groupby(["wt_id", "cal_day"])["qty"].sum()
        for (wt, tag), menge in tag_summe.items():
            if pd.isna(menge) or menge <= EPS:
                continue
            gebucht[wt] += float(menge)
            treffer = meld_tag.get((wt, pd.Timestamp(tag)))
            if treffer:
                ts, quelle = treffer[0], ("rueckmeldung" if treffer[1] == 1 else "tag")
            else:
                ts, quelle = pd.Timestamp(_lokal_mittag(tag)), "tag"
            zeiten[wt].append(pd.Timestamp(ts))
            ereignisse.append(Ereignis(ts=ts, art="gut", wt_id=wt, menge=float(menge), zeit_quelle=quelle,
                                       key=f"B|Q|{wt}|{pd.Timestamp(tag).date()}"))

    # 2) Rest: Gutmenge ohne TSF_WT_QTY (ausserhalb Proxia gemeldet) und Ausschuss
    def zeitpunkt(wt) -> tuple:
        if wt in frtg_max:
            return frtg_max[wt], "rueckmeldung"
        if zeiten[wt]:
            return max(zeiten[wt]), "tag"
        g = kette.get(wt)
        if g and g.nach:
            if g.nach in erstes:
                return erstes[g.nach], "fenster"
            if zeiten.get(g.nach):
                return min(zeiten[g.nach]), "fenster"
        return None, "init"

    for wt, r in info.iterrows():
        rest = float(r["qty_gut"]) - gebucht[wt]
        if abs(rest) > EPS:
            ts, quelle = zeitpunkt(wt)
            ereignisse.append(Ereignis(
                ts=ts if ts is not None else fs, art="gut", wt_id=wt, menge=rest,
                zeit_quelle=quelle, key=f"B|R|{wt}",
                info=None if rest > 0 else "Korrektur: TSF_WT_QTY groesser als Gutmenge"))
        aus = float(r["qty_aus"])
        if aus > EPS:
            ts, quelle = zeitpunkt(wt)
            ereignisse.append(Ereignis(ts=ts if ts is not None else fs, art="aus", wt_id=wt, menge=aus,
                                       zeit_quelle=quelle, key=f"B|A|{wt}"))

    # 3) Alles vor dem Fenster zu einem Anfangsbestand je Puffer und Ort verdichten.
    #    Zeitpunkt = letzte Gutbuchung an A vor dem Fenster (fuer "liegt seit"), sonst Fensterbeginn.
    vorher = [e for e in ereignisse if pd.Timestamp(e.ts) < fs]
    im_fenster = [e for e in ereignisse if pd.Timestamp(e.ts) >= fs]
    letzte_gut = {}
    for e in vorher:
        if e.art == "gut" and e.menge and e.menge > 0:
            letzte_gut[e.wt_id] = max(letzte_gut.get(e.wt_id, pd.Timestamp(e.ts)), pd.Timestamp(e.ts))
    init = []
    rows = wende_an(vorher, kette, Saldo())
    if rows:
        j = pd.DataFrame(rows).groupby(["von_wt_id", "ort"])["menge"].sum()
        for (von, ort), m in j.items():
            if abs(m) > EPS:
                init.append(Ereignis(ts=letzte_gut.get(von, fs), art="init", wt_id=von, menge=float(m),
                                     zeit_quelle="init", key=f"B|I|{von}|{ort}", ort=ort))
    return Vorbereitung(init + im_fenster, v)


# ── Laufender Betrieb ────────────────────────────────────────────────────────

def live_ereignisse(alt: pd.DataFrame, neu: pd.DataFrame, neue_meldungen: pd.DataFrame,
                    jetzt: datetime, lauf_id: str) -> tuple[list[Ereignis], pd.DataFrame]:
    """Mengenaenderungen gegenueber dem letzten Lauf als Ereignisse.

    alt             bestand_vorgang (qty_gut wirksam, qty_gut_proxia, qty_aus, plausi)
    neu             frisch aus Proxia (fetch_bestand_vorgaenge)
    neue_meldungen  Rueckmeldungen seit dem letzten Lauf (wt_id, rueck_ts, rueck_type_id)
    Rueckgabe: Ereignisse + neu mit wirksamer Gutmenge und plausi.
    Neue Vorgaenge (nicht in alt) erzeugen hier keine Ereignisse — das erledigt der Abgleich.
    """
    n = neu.copy()
    n["qty_gut_proxia"] = pd.to_numeric(n["qty_gut"], errors="coerce")
    for c in ("qty_gut", "qty_aus", "qty_nach", "qty_soll"):
        n[c] = pd.to_numeric(n[c], errors="coerce")
    for c in ("qty_gut", "qty_aus", "qty_nach"):
        n[c] = n[c].fillna(0.0)
    a_cols = ["wt_id", "qty_gut", "qty_gut_proxia", "qty_aus", "plausi"]
    a = (alt[[c for c in a_cols if c in alt.columns]].rename(columns=lambda c: c if c == "wt_id" else f"alt_{c}")
         if not alt.empty else pd.DataFrame(columns=["wt_id"] + [f"alt_{c}" for c in a_cols[1:]]))
    for c in a_cols[1:]:
        if f"alt_{c}" not in a:
            a[f"alt_{c}"] = None
    m = n.merge(a, on="wt_id", how="left")
    bekannt = m["wt_id"].isin(set(alt["wt_id"])) if not alt.empty else pd.Series(False, index=m.index)

    grenze = m["qty_soll"].map(mengen_grenze)
    fehl = m["qty_gut"] > grenze
    alt_gut = pd.to_numeric(m["alt_qty_gut"], errors="coerce").fillna(0.0)
    alt_roh = pd.to_numeric(m["alt_qty_gut_proxia"], errors="coerce")
    # wirksame Gutmenge: bei Fehlbuchung den bisherigen Wert behalten (neue Vorgaenge: Soll, falls fertig)
    neu_fertig = (m["wt_status_id"] == "C_FRTG")
    ersatz = alt_gut.where(bekannt, m["qty_soll"].fillna(0.0).where(neu_fertig, 0.0))
    m["qty_gut"] = m["qty_gut"].where(~fehl, ersatz)
    roh_unveraendert = (alt_roh == m["qty_gut_proxia"]) | (alt_roh.isna() & m["qty_gut_proxia"].isna())
    m["plausi"] = None
    m.loc[fehl, "plausi"] = [f"Fehlbuchung Gutmenge (Proxia {x:,.0f})".replace(",", ".")
                             for x in m.loc[fehl, "qty_gut_proxia"]]
    behalten = ~fehl & bekannt & roh_unveraendert & m["alt_plausi"].notna()
    m.loc[behalten, "plausi"] = m.loc[behalten, "alt_plausi"]

    meld = neue_meldungen[neue_meldungen["rueck_type_id"].isin(MELDE_TYPEN)] \
        if not neue_meldungen.empty else neue_meldungen
    letzte = meld.groupby("wt_id")["rueck_ts"].max() if not meld.empty else pd.Series(dtype=object)

    d_gut = m["qty_gut"] - alt_gut
    d_aus = m["qty_aus"] - pd.to_numeric(m["alt_qty_aus"], errors="coerce").fillna(0.0)
    aendern = bekannt & ((d_gut.abs() > EPS) | (d_aus.abs() > EPS))
    ereignisse = []
    for i in m.index[aendern]:
        wt = m.at[i, "wt_id"]
        if wt in letzte.index:
            ts, quelle = letzte[wt], "rueckmeldung"
        else:
            ts, quelle = jetzt, "erkennung"
        if abs(d_gut[i]) > EPS:
            ereignisse.append(Ereignis(ts=ts, art="gut", wt_id=wt, menge=float(d_gut[i]), zeit_quelle=quelle,
                                       key=f"L|G|{wt}|{lauf_id}",
                                       info=None if d_gut[i] > 0 else "Gutmenge in Proxia reduziert"))
        if abs(d_aus[i]) > EPS:
            ereignisse.append(Ereignis(ts=ts, art="aus", wt_id=wt, menge=float(d_aus[i]), zeit_quelle=quelle,
                                       key=f"L|A|{wt}|{lauf_id}"))
    out = m.drop(columns=[c for c in m.columns if c.startswith("alt_")])
    return ereignisse, out


DIFFERENZ_REST = "Differenz bei Fertigmeldung Nachfolger: Rest ausgebucht"
DIFFERENZ_FEHL = "Differenz bei Fertigmeldung Nachfolger: Fehlmenge ausgeglichen"


def puffer_soll(v: pd.DataFrame, g: Glied) -> float:
    """Sollbestand eines Puffers: Gut(A) - Gut(B) - Ausschuss(B).

    Regel 1: Ist B fertig gemeldet, ist der Puffer leer. Ein Rest (A hat mehr geliefert als B
    verbraucht, z. B. Ausschuss nicht gebucht) wird als Differenz ausgebucht. Wird B wieder
    geoeffnet, holt der Abgleich die Menge automatisch zurueck.
    """
    if v.at[g.nach, "wt_status_id"] == "C_FRTG":
        return 0.0
    return float(v.at[g.wt_id, "qty_gut"] or 0) - float(v.at[g.nach, "qty_gut"] or 0) \
        - float(v.at[g.nach, "qty_aus"] or 0)


def abgleich_ereignisse(vorgaenge: pd.DataFrame, kette: dict[str, Glied], saldo: Saldo,
                        orders: set, jetzt: datetime, lauf_id: str,
                        letzte_ts: dict | None = None) -> list[Ereignis]:
    """Soll-Ist-Abgleich je Puffer (puffer_soll) gegen den Journalstand.

    Faengt alles ab, was die Einzelereignisse nicht abbilden (neuer oder geaenderter
    Arbeitsplan, wieder geoeffnete Auftraege, Korrekturen in Proxia) und bucht Reste
    nach Fertigmeldung des Nachfolgers aus (art 'differenz').
    letzte_ts: je Puffer letzte Journalzeit (nur Erstlauf) -> Differenz zum Zeitpunkt der
    Fertigmeldung von B statt 'jetzt', damit der Tagesverlauf stimmt.
    """
    v = vorgaenge.set_index("wt_id")
    out = []
    for g in kette.values():
        if not g.nach or g.pps_order not in orders or g.wt_id not in v.index or g.nach not in v.index:
            continue
        soll = puffer_soll(v, g)
        ist = saldo.gesamt(g.wt_id)
        if abs(soll - ist) <= 1e-6:
            continue
        if v.at[g.nach, "wt_status_id"] == "C_FRTG":
            ts = jetzt
            if letzte_ts is not None:
                kand = [x for x in (v.at[g.nach, "letzte_meldung_ts"] if "letzte_meldung_ts" in v else None,
                                    letzte_ts.get(g.wt_id)) if x is not None and not pd.isna(x)]
                ts = max(pd.Timestamp(x) for x in kand) if kand else jetzt
            out.append(Ereignis(ts=ts, art="differenz", wt_id=g.wt_id, menge=soll - ist,
                                zeit_quelle="erkennung", key=f"D|{g.wt_id}|{lauf_id}",
                                info=DIFFERENZ_REST if ist > soll else DIFFERENZ_FEHL))
        else:
            out.append(Ereignis(ts=jetzt, art="abgleich", wt_id=g.wt_id, menge=soll - ist,
                                zeit_quelle="erkennung", key=f"K|{g.wt_id}|{lauf_id}",
                                info="Abgleich mit Proxia (Arbeitsplan oder Menge geaendert)"))
    return out


def ausbuchen_zeilen(von_wt_id: str, pps_order: str, orte: dict, ts, key: str, info: str) -> list[dict]:
    """Puffer eines Vorgangs, der nicht mehr im Arbeitsplan steht, auf 0 setzen."""
    rows = []
    for ort, m in orte.items():
        if abs(m) > EPS:
            rows.append({"quelle_key": f"{key}|{ort}"[:100], "ts": pd.Timestamp(ts).to_pydatetime(),
                         "pps_order": pps_order, "von_wt_id": von_wt_id, "nach_wt_id": None,
                         "von_work_cntr": None, "nach_work_cntr": None, "ort": ort, "menge": -float(m),
                         "art": "abgleich", "zeit_quelle": "erkennung", "transport_id": None, "info": info})
            orte[ort] = 0.0
    return rows


def transport_ereignisse(events: pd.DataFrame) -> list[Ereignis]:
    """transport_event (+ von_wt_id, menge aus transport_auftrag) -> Ereignisse."""
    out = []
    for r in events.itertuples(index=False):
        if r.event not in ("uebernommen", "zurueckgegeben", "erledigt", "auto_erledigt"):
            continue
        art = "erledigt" if r.event == "auto_erledigt" else r.event
        out.append(Ereignis(ts=r.ts, art=art, wt_id=r.von_wt_id,
                            menge=r.menge if pd.notna(r.menge) else None, zeit_quelle="app",
                            key=f"E|{int(r.id)}", transport_id=int(r.transport_id),
                            info=(r.fahrer if pd.notna(r.fahrer) else None)))
    return out


def auftrag_abgeschlossen(vorgaenge: pd.DataFrame) -> set:
    """Auftraege, deren letzter Vorgang fertig ist — Restbestaende werden ausgebucht.

    Bewusst nicht "alle Vorgaenge fertig": vergessene Fertigmeldungen in der Mitte
    wuerden einen Auftrag sonst ewig offen halten.
    """
    if vorgaenge.empty:
        return set()
    v = vorgaenge.assign(_afo=pd.to_numeric(vorgaenge["afo_nr"].astype("string").str.strip(), errors="coerce"))
    letzter = v.sort_values(["pps_order", "_afo"]).groupby("pps_order").tail(1)
    return set(letzter.loc[letzter["wt_status_id"] == "C_FRTG", "pps_order"])
