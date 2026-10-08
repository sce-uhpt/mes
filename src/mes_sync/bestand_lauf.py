"""Bestandsmonitor: Lese-/Schreibzugriffe und der Ablauf eines Laufs.

Erster Lauf (bestand_status.backfill_ab ist leer): Auftraege mit Proxia-Aktivitaet in den
letzten MES_BESTAND_TAGE Tagen nachladen und das Journal komplett aufbauen.

Danach je Lauf (alle MES_BESTAND_SEKUNDEN):
1. Bekannte, nicht abgeschlossene Auftraege in TSF_WT neu lesen -> Mengenaenderungen.
   Hat der Vorgang seit dem letzten Lauf eine Proxia-Rueckmeldung, gilt deren Zeitpunkt,
   sonst der Zeitpunkt der Erkennung (Meldung kam von ausserhalb, vermutlich SAP).
2. Neue Auftraege (neue Rueckmeldungen in pps_rueckmeldung) wie beim ersten Lauf nachladen.
3. Transport-Events der App seit dem letzten Lauf verbuchen.
4. Auftraege, deren Vorgaenge alle fertig sind, ausbuchen und aus dem Lauf nehmen.
Alles in einer Transaktion.
"""

import logging
import time
import uuid
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.engine import Connection

from . import bestand as B
from . import proxia, state
from .schema import (
    arbeitsplatz,
    bestand_puffer,
    bestand_status,
    bestand_tag,
    bestand_vorgang,
    pps_rueckmeldung,
    transport_auftrag,
    transport_event,
    wip_bewegung,
)
from .state import _chunks, _none, _records

log = logging.getLogger("mes_sync.bestand")

VORGANG_SPALTEN = [c.name for c in bestand_vorgang.columns]
JOURNAL_SPALTEN = [c.name for c in wip_bewegung.columns if c.name != "id"]


# ── Lesen ────────────────────────────────────────────────────────────────────

def lese_status(con: Connection) -> dict:
    r = con.execute(select(bestand_status).where(bestand_status.c.id == 1)).mappings().first()
    d = dict(r) if r else {}
    for k in ("backfill_ab", "rueck_wz", "letzter_lauf", "letzter_erfolg"):
        if d.get(k) is not None:
            d[k] = pd.Timestamp(d[k]).to_pydatetime()
    return d


def lade_arbeitsplatz(con: Connection) -> pd.DataFrame:
    a = arbeitsplatz.c
    return pd.read_sql(select(a.work_cntr, a.bezeichnung, a.res_typ, a.sektor, a.art), con)


def _ts(df: pd.DataFrame, *cols) -> pd.DataFrame:
    """SQLite liefert Zeitstempel als Text, SQL Server als datetime — vereinheitlichen."""
    for c in cols:
        if c in df:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def lade_vorgaenge(con: Connection, auch_orders=()) -> pd.DataFrame:
    """Nicht abgeschlossene Auftraege plus `auch_orders` (z. B. Nacharbeit an fertigen Auftraegen)."""
    v = bestand_vorgang
    frames = [pd.read_sql(select(v).where(v.c.abgeschlossen == False), con)]  # noqa: E712
    for o in _chunks(sorted(set(auch_orders))):
        frames.append(pd.read_sql(select(v).where(and_(v.c.pps_order.in_(o), v.c.abgeschlossen == True)), con))  # noqa: E712
    df = pd.concat(frames, ignore_index=True)
    return _ts(df, "begin_scheduled", "erste_meldung_ts", "letzte_meldung_ts", "aktualisiert_am")


def lade_journal(con: Connection, von_ids) -> pd.DataFrame:
    w = wip_bewegung
    frames = [pd.read_sql(select(w).where(w.c.von_wt_id.in_(ids)), con) for ids in _chunks(von_ids)]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=[c.name for c in w.columns])
    return _ts(df, "ts")


def lade_puffer(con: Connection, von_ids) -> pd.DataFrame:
    p = bestand_puffer
    frames = [pd.read_sql(select(p).where(p.c.von_wt_id.in_(ids)), con) for ids in _chunks(von_ids)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=[c.name for c in p.columns])


def lade_unterwegs(con: Connection, transport_ids) -> dict:
    """Je Transport die Menge, die laut Journal gerade unterwegs ist."""
    w = wip_bewegung.c
    out = {}
    for ids in _chunks([int(i) for i in transport_ids]):
        rows = con.execute(
            select(w.transport_id, func.sum(w.menge))
            .where(and_(w.transport_id.in_(ids), w.ort == "unterwegs"))
            .group_by(w.transport_id)).all()
        out.update({int(t): float(m) for t, m in rows if m and m > B.EPS})
    return out


def lade_transport_events(con: Connection, nach_id: int | None, bis_id: int, seit=None) -> pd.DataFrame:
    """Transport-Events (nach_id, bis_id] — bis_id vorher lesen, sonst gehen Events verloren."""
    e, t = transport_event.c, transport_auftrag.c
    q = (select(e.id, e.transport_id, e.event, e.fahrer, e.ts, t.von_wt_id, t.menge, t.pps_order)
         .join(transport_auftrag, t.id == e.transport_id)
         .where(and_(e.event.in_(("uebernommen", "zurueckgegeben", "erledigt", "auto_erledigt")),
                     e.id <= bis_id)))
    if nach_id is not None:
        q = q.where(e.id > nach_id)
    if seit is not None:
        q = q.where(e.ts >= seit)
    return _ts(pd.read_sql(q.order_by(e.id), con), "ts")


def lade_puffer_orders(con: Connection, orders) -> pd.DataFrame:
    p = bestand_puffer
    frames = [pd.read_sql(select(p).where(p.c.pps_order.in_(o)), con) for o in _chunks(sorted(orders))]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=[c.name for c in p.columns])


def max_event_id(con: Connection) -> int:
    return int(con.execute(select(func.max(transport_event.c.id))).scalar() or 0)


def neue_meldungen(con: Connection, seit) -> pd.DataFrame:
    r = pps_rueckmeldung.c
    q = select(r.wt_id, r.pps_order, r.rueck_ts, r.rueck_type_id)
    if seit is not None:
        q = q.where(r.geladen_am > seit)
    return _ts(pd.read_sql(q, con), "rueck_ts")


# ── Schreiben ────────────────────────────────────────────────────────────────

def _vorgang_records(v: pd.DataFrame, kette: dict, abgeschlossen: set, jetzt) -> list[dict]:
    df = v.copy()
    df["vor_wt_id"] = df["wt_id"].map(lambda w: kette[w].vor if w in kette else None)
    df["nach_wt_id"] = df["wt_id"].map(lambda w: kette[w].nach if w in kette else None)
    df["abgeschlossen"] = df["pps_order"].isin(abgeschlossen)
    df["aktualisiert_am"] = jetzt
    for c in VORGANG_SPALTEN:
        if c not in df:
            df[c] = None
    return _records(df, VORGANG_SPALTEN)


VERGLEICH = ["pps_order", "afo_nr", "wt_status_id", "work_cntr", "vor_wt_id", "nach_wt_id", "qty_soll",
             "qty_gut", "qty_gut_proxia", "qty_aus", "qty_nach", "plausi", "abgeschlossen",
             "erste_meldung_ts", "letzte_meldung_ts", "begin_scheduled", "plan_res"]


def _gleich(a, b) -> bool:
    if a is None or (isinstance(a, float) and a != a):
        return b is None or (isinstance(b, float) and b != b)
    if b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    try:
        return pd.Timestamp(a) == pd.Timestamp(b) if hasattr(a, "year") or hasattr(b, "year") else str(a) == str(b)
    except (ValueError, TypeError):
        return str(a) == str(b)


def speichere_vorgaenge(con: Connection, v: pd.DataFrame, kette, abgeschlossen, jetzt,
                        alt: pd.DataFrame | None = None) -> int:
    """Vorgaenge schreiben; mit `alt` nur die geaenderten (haelt den Lauf kurz)."""
    if v.empty:
        return 0
    recs = _vorgang_records(v, kette, abgeschlossen, jetzt)
    if alt is not None and not alt.empty:
        bisher = {r["wt_id"]: r for r in _records(alt, [c for c in VORGANG_SPALTEN if c in alt.columns])}
        recs = [r for r in recs if r["wt_id"] not in bisher
                or not all(_gleich(r.get(c), bisher[r["wt_id"]].get(c)) for c in VERGLEICH)]
    if not recs:
        return 0
    for ids in _chunks([r["wt_id"] for r in recs]):
        con.execute(delete(bestand_vorgang).where(bestand_vorgang.c.wt_id.in_(ids)))
    for part in _chunks(recs):
        con.execute(insert(bestand_vorgang), part)
    return len(recs)


def speichere_journal(con: Connection, rows: list[dict], jetzt, pruefen: bool = True) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df["erfasst_am"] = jetzt
    vorhanden = set()
    for keys in (_chunks(df["quelle_key"].unique()) if pruefen else []):
        vorhanden |= set(con.execute(
            select(wip_bewegung.c.quelle_key).where(wip_bewegung.c.quelle_key.in_(keys))).scalars())
    df = df[~df["quelle_key"].isin(vorhanden)].drop_duplicates("quelle_key")
    if df.empty:
        return 0
    recs = _records(df, JOURNAL_SPALTEN)
    for part in _chunks(recs, 1000):
        con.execute(insert(wip_bewegung), part)
    return len(df)


def aktualisiere_puffer(con: Connection, von_ids, jetzt) -> pd.DataFrame:
    von_ids = list(dict.fromkeys(von_ids))
    if not von_ids:
        return pd.DataFrame(columns=B.PUFFER_COLS)
    p = B.berechne_puffer(lade_journal(con, von_ids))
    for ids in _chunks(von_ids):
        con.execute(delete(bestand_puffer).where(bestand_puffer.c.von_wt_id.in_(ids)))
    if not p.empty:
        p["aktualisiert_am"] = jetzt
        cols = B.PUFFER_COLS + ["aktualisiert_am"]
        for part in _chunks(_records(p, cols), 1000):
            con.execute(insert(bestand_puffer), part)
    return p


def schreibe_tagesbestand(con: Connection, tb: pd.DataFrame, tage: list, jetzt) -> None:
    for t in _chunks(tage):
        con.execute(delete(bestand_tag).where(bestand_tag.c.tag.in_(t)))
    if tb.empty:
        return
    df = tb.copy()
    df["aktualisiert_am"] = jetzt
    for part in _chunks(_records(df, ["tag", "work_cntr", "anzahl_puffer", "menge", "aktualisiert_am"]), 1000):
        con.execute(insert(bestand_tag), part)


def tagesbestand_heute(con: Connection, jetzt) -> pd.DataFrame:
    """Heutiger Stand je Arbeitsplatz B direkt aus bestand_puffer."""
    p, v = bestand_puffer.c, bestand_vorgang.c
    df = pd.read_sql(
        select(p.menge_gesamt, v.work_cntr.label("work_cntr"))
        .select_from(bestand_puffer.join(bestand_vorgang, v.wt_id == p.nach_wt_id))
        .where(p.menge_gesamt > B.EPS), con)
    heute = pd.Timestamp(jetzt).tz_localize("UTC").tz_convert(B.TZ).date()
    if df.empty:
        return pd.DataFrame(columns=["tag", "work_cntr", "anzahl_puffer", "menge"])
    g = df.groupby(df["work_cntr"].fillna("?")).agg(anzahl_puffer=("menge_gesamt", "size"),
                                                    menge=("menge_gesamt", "sum")).reset_index()
    g["tag"] = heute
    return g[["tag", "work_cntr", "anzahl_puffer", "menge"]]


def ergaenze_art(con: Connection, pseudo: set, jetzt) -> int:
    """Leere arbeitsplatz.art mit einem Vorschlag fuellen; Gepflegtes bleibt unberuehrt."""
    a = arbeitsplatz.c
    leer = pd.read_sql(select(a.work_cntr, a.bezeichnung, a.res_typ).where(a.art.is_(None)), con)
    for r in leer.itertuples(index=False):
        con.execute(update(arbeitsplatz).where(and_(a.work_cntr == r.work_cntr, a.art.is_(None)))
                    .values(art=B.art_vorschlag(r.work_cntr, r.bezeichnung, r.res_typ, pseudo),
                            aktualisiert_am=jetzt))
    return len(leer)


def setze_status(con: Connection, jetzt, erfolg: bool, meldung: str, **werte) -> None:
    v = {"letzter_lauf": jetzt, "meldung": (meldung or "")[:1000], **werte}
    if erfolg:
        v["letzter_erfolg"] = jetzt
    con.execute(update(bestand_status).where(bestand_status.c.id == 1).values(**v))


def zuruecksetzen(engine) -> None:
    """Journal und Bestand leeren; der naechste Lauf baut alles neu auf (Backfill)."""
    with engine.begin() as con:
        for t in (wip_bewegung, bestand_puffer, bestand_tag, bestand_vorgang):
            con.execute(delete(t))
        con.execute(update(bestand_status).where(bestand_status.c.id == 1).values(
            backfill_ab=None, rueck_wz=None, event_wz=None, meldung="zurueckgesetzt"))


# ── Ablauf ───────────────────────────────────────────────────────────────────

def _nachladen(proxia_engine, orders, fenster_start, jetzt, ap, settings):
    vorgaenge = proxia.fetch_bestand_vorgaenge(proxia_engine, orders)
    if vorgaenge.empty:
        return None
    wt_qty = proxia.fetch_wt_qty(proxia_engine, orders)
    rueck = proxia.fetch_rueck_orders(proxia_engine, orders)
    kette = B.baue_kette(vorgaenge, ap, settings.fallback_regel, settings.bestand_transport_ort)
    vb = B.backfill(vorgaenge, wt_qty, rueck, kette, fenster_start, jetzt)
    return vb, kette


def _abschluss(vorgaenge: pd.DataFrame, kette, ereignisse, mindestens=None) -> tuple[set, list]:
    """Abschluss-Ereignisse fuer fertige Auftraege, zeitlich nach dem letzten Ereignis des Auftrags."""
    fertig = B.auftrag_abgeschlossen(vorgaenge)
    if not fertig:
        return fertig, []
    letzte = {}
    for e in ereignisse:
        g = kette.get(e.wt_id)
        if g and g.pps_order in fertig:
            letzte[g.pps_order] = max(letzte.get(g.pps_order, pd.Timestamp(e.ts)), pd.Timestamp(e.ts))
    meld = vorgaenge.groupby("pps_order")["letzte_meldung_ts"].max() if "letzte_meldung_ts" in vorgaenge else {}
    ev = []
    for g in kette.values():
        if g.pps_order not in fertig or not g.nach:
            continue
        kand = [x for x in (letzte.get(g.pps_order), meld.get(g.pps_order) if len(meld) else None, mindestens)
                if x is not None and not pd.isna(x)]
        ts = max(pd.Timestamp(x) for x in kand) if kand else pd.Timestamp(mindestens)
        ev.append(B.Ereignis(ts=ts, art="abschluss", wt_id=g.wt_id, menge=None, zeit_quelle="erkennung",
                             key=f"X|{g.wt_id}|{ts:%Y%m%d%H%M%S}", info="Auftrag fertig gemeldet"))
    return fertig, ev


def lauf(settings, proxia_engine, sce_engine) -> dict:
    jetzt = state.utcnow()
    t0 = time.monotonic()
    with sce_engine.connect() as con:
        st = lese_status(con)
    if not st.get("backfill_ab"):
        stat = erstlauf(settings, proxia_engine, sce_engine, jetzt)
    else:
        stat = folgelauf(settings, proxia_engine, sce_engine, jetzt, st)
    stat["dauer_ms"] = int((time.monotonic() - t0) * 1000)
    return stat


def _scope(settings, proxia_engine, jetzt) -> tuple[datetime, set]:
    fs = jetzt - timedelta(days=settings.bestand_tage)
    orders = proxia.fetch_scope_orders(proxia_engine, fs)
    # Ware, die laenger liegt als das Fenster: offene Auftraege mit Aktivitaet im letzten Jahr
    if settings.bestand_scope_tage > settings.bestand_tage:
        orders |= proxia.fetch_offene_orders(proxia_engine, jetzt - timedelta(days=settings.bestand_scope_tage))
    return fs, {o for o in orders if not B.ist_ausgeschlossen(o, settings.bestand_ausschluss)}


def _ohne_transport(tr: pd.DataFrame, settings) -> pd.DataFrame:
    """Push ohne Transport-App (MES_BESTAND_TRANSPORT_ORT=0): Transport-Events nicht verbuchen."""
    return tr if settings.bestand_transport_ort else tr.iloc[0:0]


def erstlauf(settings, proxia_engine, sce_engine, jetzt) -> dict:
    fs, orders = _scope(settings, proxia_engine, jetzt)
    log.info("Bestand: Erstbefuellung fuer %s Auftraege ab %s", len(orders), fs)
    with sce_engine.connect() as con:
        ap = lade_arbeitsplatz(con)
        ev_wz = max_event_id(con)
        tr = _ohne_transport(lade_transport_events(con, None, ev_wz, seit=fs), settings)
    erg = _nachladen(proxia_engine, orders, fs, jetzt, ap, settings) if orders else None
    rows, v, kette, fertig, korr = [], pd.DataFrame(), {}, set(), []
    if erg:
        vb, kette = erg
        v = vb.vorgaenge
        ev = vb.ereignisse + B.transport_ereignisse(tr)
        fertig, ab = _abschluss(v, kette, ev)
        saldo = B.Saldo()
        rows = B.wende_an(ev, kette, saldo)
        # Regel 1 (Nachfolger fertig -> Rest ausbuchen) zum Zeitpunkt der Fertigmeldung
        letzte_ts = pd.DataFrame(rows).groupby("von_wt_id")["ts"].max().to_dict() if rows else {}
        korr = B.abgleich_ereignisse(v, kette, saldo, orders - fertig, jetzt, "init", letzte_ts)
        rows += B.wende_an(korr + ab, kette, saldo)

    tage = [d.date() for d in pd.date_range(
        pd.Timestamp(fs).tz_localize("UTC").tz_convert(B.TZ).date(),
        pd.Timestamp(jetzt).tz_localize("UTC").tz_convert(B.TZ).date(), freq="D")]
    with sce_engine.begin() as con:
        for t in (wip_bewegung, bestand_puffer, bestand_tag, bestand_vorgang):
            con.execute(delete(t))
        if not v.empty:
            state.ergaenze_arbeitsplaetze(con, v, jetzt)
        n_art = ergaenze_art(con, settings.pseudo_arbeitsplaetze, jetzt)
        speichere_vorgaenge(con, v, kette, fertig, jetzt)
        n = speichere_journal(con, rows, jetzt, pruefen=False)
        journal = pd.DataFrame(rows)
        if rows:
            journal["id"] = range(len(journal))  # Buchungsreihenfolge wie in der Datenbank
        p = B.berechne_puffer(journal) if rows else pd.DataFrame(columns=B.PUFFER_COLS)
        if not p.empty:
            p["aktualisiert_am"] = jetzt
            for part in _chunks(_records(p, B.PUFFER_COLS + ["aktualisiert_am"]), 1000):
                con.execute(insert(bestand_puffer), part)
        tb = B.berechne_tagesbestand(journal, tage) if rows else pd.DataFrame()
        schreibe_tagesbestand(con, tb, tage, jetzt)
        stat = {"modus": "erstlauf", "auftraege": len(orders), "vorgaenge": len(v), "bewegungen": n,
                "puffer_mit_bestand": int((p["menge_gesamt"] > B.EPS).sum()) if len(p) else 0,
                "abgeschlossen": len(fertig), "differenzen": sum(e.art == "differenz" for e in korr),
                "abgleich_korrekturen": sum(e.art == "abgleich" for e in korr),
                "arbeitsplatz_art_gesetzt": n_art}
        setze_status(con, jetzt, True, ", ".join(f"{k}={v_}" for k, v_ in stat.items()),
                     backfill_ab=fs, rueck_wz=jetzt, event_wz=ev_wz)
    return stat


def folgelauf(settings, proxia_engine, sce_engine, jetzt, st) -> dict:
    lauf_id = uuid.uuid4().hex[:10]
    with sce_engine.connect() as con:
        meld = neue_meldungen(con, st.get("rueck_wz"))
        alt = lade_vorgaenge(con, set(meld["pps_order"].dropna()) if not meld.empty else ())
        ev_bis = max_event_id(con)
        tr = _ohne_transport(lade_transport_events(con, st.get("event_wz"), ev_bis), settings)
        ap = lade_arbeitsplatz(con)
        alle_bekannten = set(pd.read_sql(select(bestand_vorgang.c.pps_order).distinct(), con)["pps_order"])

    bekannte_orders = set(alt["pps_order"]) if not alt.empty else set()
    a = alt.set_index("wt_id") if not alt.empty else pd.DataFrame(columns=["pps_order"])
    neue_orders = set(meld["pps_order"].dropna()) - alle_bekannten if not meld.empty else set()
    if not tr.empty:  # Transporte zu Auftraegen, die der Bestand noch nicht kennt
        neue_orders |= set(tr["pps_order"].dropna()) - alle_bekannten
    neue_orders = {o for o in neue_orders if not B.ist_ausgeschlossen(o, settings.bestand_ausschluss)}

    t_proxia = time.monotonic()
    neu = proxia.fetch_bestand_vorgaenge(proxia_engine, bekannte_orders) if bekannte_orders else pd.DataFrame()
    nachgeladen = _nachladen(proxia_engine, neue_orders, st["backfill_ab"], jetzt, ap, settings) \
        if neue_orders else None
    proxia_ms = int((time.monotonic() - t_proxia) * 1000)

    ereignisse, v_neu, kette = [], pd.DataFrame(), {}
    if not neu.empty:
        kette = B.baue_kette(neu, ap, settings.fallback_regel, settings.bestand_transport_ort)
        ev, v_neu = B.live_ereignisse(alt, neu, meld, jetzt, lauf_id)
        v_neu["erste_meldung_ts"] = v_neu["wt_id"].map(a["erste_meldung_ts"])
        v_neu["letzte_meldung_ts"] = v_neu["wt_id"].map(a["letzte_meldung_ts"])
        if not meld.empty:
            m = meld[meld["wt_id"].isin(v_neu["wt_id"])]
            erste = m.groupby("wt_id")["rueck_ts"].min()
            letzte = m[m["rueck_type_id"].isin(B.MELDE_TYPEN)].groupby("wt_id")["rueck_ts"].max()
            v_neu["erste_meldung_ts"] = v_neu["erste_meldung_ts"].fillna(v_neu["wt_id"].map(erste))
            v_neu["letzte_meldung_ts"] = pd.concat(
                [v_neu["letzte_meldung_ts"], v_neu["wt_id"].map(letzte)], axis=1).max(axis=1)
        ereignisse += ev
    if nachgeladen:
        vb, k2 = nachgeladen
        kette.update(k2)
        ereignisse += vb.ereignisse
        v_neu = pd.concat([v_neu, vb.vorgaenge], ignore_index=True) if not v_neu.empty else vb.vorgaenge
    tr_ev = B.transport_ereignisse(tr) if not tr.empty else []
    ereignisse += tr_ev

    orders = bekannte_orders | neue_orders
    with sce_engine.begin() as con:
        puffer_alt = lade_puffer_orders(con, orders)
        saldo = B.saldo_aus_puffer(puffer_alt, lade_unterwegs(con, [e.transport_id for e in tr_ev]))
        rows = B.wende_an(ereignisse, kette, saldo)
        # Vorgaenge, die nicht mehr im Arbeitsplan stehen: Puffer ausbuchen
        weg = puffer_alt[~puffer_alt["von_wt_id"].isin(set(kette))] if not puffer_alt.empty else puffer_alt
        for r in weg.itertuples(index=False):
            rows += B.ausbuchen_zeilen(r.von_wt_id, r.pps_order, saldo.orte[r.von_wt_id], jetzt,
                                       f"W|{r.von_wt_id}|{lauf_id}", "Vorgang nicht mehr im Arbeitsplan")
        fertig, ab = _abschluss(v_neu, kette, ereignisse, mindestens=jetzt) if not v_neu.empty else (set(), [])
        # Soll-Ist-Abgleich je Puffer gegen Proxia (normalerweise leer), danach Abschluss
        korr = B.abgleich_ereignisse(v_neu, kette, saldo, orders - fertig, jetzt, lauf_id) \
            if not v_neu.empty else []
        rows += B.wende_an(korr + ab, kette, saldo)

        if not v_neu.empty:
            state.ergaenze_arbeitsplaetze(con, v_neu, jetzt)
            ergaenze_art(con, settings.pseudo_arbeitsplaetze, jetzt)
            n_v = speichere_vorgaenge(con, v_neu, kette, fertig, jetzt, alt)
            geloescht = set(alt["wt_id"]) - set(v_neu["wt_id"]) if not alt.empty else set()
            geloescht = {w for w in geloescht if a.at[w, "pps_order"] in bekannte_orders}
            for ids in _chunks(sorted(geloescht)):
                con.execute(delete(bestand_vorgang).where(bestand_vorgang.c.wt_id.in_(ids)))
        else:
            n_v, geloescht = 0, set()
        n = speichere_journal(con, rows, jetzt)
        aktualisiere_puffer(con, [r["von_wt_id"] for r in rows], jetzt)
        heute = tagesbestand_heute(con, jetzt)
        schreibe_tagesbestand(con, heute, [pd.Timestamp(jetzt).tz_localize("UTC").tz_convert(B.TZ).date()],
                              jetzt)
        quellen = pd.Series([e.zeit_quelle for e in ereignisse if e.art in ("gut", "aus")], dtype=object)
        stat = {"modus": "laufend", "auftraege": len(bekannte_orders), "neue_auftraege": len(neue_orders),
                "mengenaenderungen": int(len(quellen)),
                "davon_ohne_rueckmeldung": int((quellen == "erkennung").sum()),
                "transport_events": len(tr_ev),
                "abgleich_korrekturen": sum(e.art == "abgleich" for e in korr) + len(weg),
                "differenzen": sum(e.art == "differenz" for e in korr),
                "abgeschlossen": len(fertig), "vorgaenge_geschrieben": n_v,
                "vorgaenge_entfernt": len(geloescht), "bewegungen": n, "proxia_ms": proxia_ms}
        setze_status(con, jetzt, True, ", ".join(f"{k}={v_}" for k, v_ in stat.items()),
                     rueck_wz=jetzt, event_wz=ev_bis)
    return stat
