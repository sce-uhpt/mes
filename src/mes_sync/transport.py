"""Reine Logik: aus Rueckmeldungen + Arbeitsplan werden Transportauftraege.

Keine DB-Zugriffe — alles hier ist mit DataFrames testbar (tests/test_transport.py).

Ablauf je Lauf:
1. Ausloeser sind neue Rueckmeldungen vom Typ C_TFRTG (teilfertig) oder C_FRTG (fertig).
2. Modus je Von-Arbeitsplatz (sce_mes.arbeitsplatz.transport_modus, sonst Standard):
   - teil: jede Teil-/Fertigmeldung mit neuer Gutmenge erzeugt einen Transport
   - voll: nur die Fertigmeldung erzeugt genau einen Transport je Vorgang
3. Nachfolger = naechsthoehere AFO_NR im selben Auftrag (Proxia-Verkettung CHAINED_WT_ID
   ist praktisch leer, parallele/gesplittete Vorgaenge kommen nicht vor — Stand 02.10.2026).
4. Transportregel (rules.py): Sektorwechsel, solange Sektoren fehlen Arbeitsplatzwechsel.
5. Menge = aktuelle Gutmenge des Vorgangs minus Menge, die schon auf fruehere Transporte
   dieses Vorgangs verteilt wurde.
"""

from dataclasses import dataclass, field

import pandas as pd

from .rules import transport_noetig

AUSLOESER = ("C_TFRTG", "C_FRTG")
# Rueckmeldungen am Nach-Vorgang, die belegen, dass die Ware angekommen ist.
# C_TFRTG bewusst NICHT: bei Teillosen kann sie zu einem frueheren Los gehoeren.
AUTO_ERLEDIGT_TYPEN = ("C_START", "C_FRTG")

TRANSPORT_COLS = [
    "quelle_key", "modus", "pps_order", "material_nr", "material_text", "psp",
    "von_wt_id", "von_afo", "von_vorgang_text", "von_work_cntr", "von_res", "von_res_typ", "ist_res",
    "nach_wt_id", "nach_afo", "nach_vorgang_text", "nach_work_cntr", "nach_res", "nach_res_typ",
    "menge", "menge_soll", "ist_teilmenge", "rueck_id", "rueck_ts",
]


@dataclass
class Ergebnis:
    transporte: pd.DataFrame
    statistik: dict = field(default_factory=dict)


def _afo_int(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype("string").str.strip(), errors="coerce")


def build_kette(vorgaenge: pd.DataFrame) -> pd.DataFrame:
    """Je (Auftrag, AFO) genau ein Vorgang plus dessen Nachfolger.

    Doppelte AFO_NR (Splits) werden deterministisch auf die kleinste WT_ID reduziert.
    """
    v = vorgaenge.copy()
    v["afo_int"] = _afo_int(v["afo_nr"])
    v = v.dropna(subset=["afo_int"])
    v = v.sort_values(["pps_order", "afo_int", "wt_id"]).drop_duplicates(["pps_order", "afo_int"])
    g = v.groupby("pps_order", sort=False)
    nach_cols = ["wt_id", "afo_nr", "vorgang_text", "work_cntr", "plan_res", "plan_res_typ", "wt_status_id"]
    for col in nach_cols:
        v[f"nach_{col}"] = g[col].shift(-1)
    return v


def build_transporte(
    events: pd.DataFrame,
    vorgaenge: pd.DataFrame,
    bisherige: pd.DataFrame,
    arbeitsplatz: pd.DataFrame,
    standard_modus: str = "teil",
    fallback_regel: str = "arbeitsplatz",
) -> Ergebnis:
    """
    events      neue Rueckmeldungen (rueck_id, wt_id, rueck_ts, rueck_type_id, ist_res, ...)
    vorgaenge   Arbeitsplan der betroffenen Auftraege (Spalten wie sce_mes.pps_vorgang)
    bisherige   bereits existierende Transporte dieser Vorgaenge (von_wt_id, quelle_key, menge, status)
    arbeitsplatz Stammdaten (work_cntr, sektor, transport_modus)
    """
    stat = {"rueckmeldungen": len(events)}
    leer = Ergebnis(pd.DataFrame(columns=TRANSPORT_COLS), stat)

    if events.empty or vorgaenge.empty:
        return leer

    trig = events[events["rueck_type_id"].isin(AUSLOESER)].copy()
    stat["ausloeser"] = len(trig)
    if trig.empty:
        return leer

    kette = build_kette(vorgaenge)
    vorgang_info = vorgaenge.drop_duplicates("wt_id").set_index("wt_id")

    # Ausloeser -> Von-Vorgang
    trig = trig.merge(
        vorgang_info[["work_cntr"]].rename(columns={"work_cntr": "von_work_cntr"}),
        left_on="wt_id", right_index=True, how="left",
    )
    ohne = trig["von_work_cntr"].isna() & ~trig["wt_id"].isin(vorgang_info.index)
    stat["ohne_vorgang"] = int(ohne.sum())
    trig = trig[~ohne]

    # Modus je Von-Arbeitsplatz
    ap = arbeitsplatz.set_index("work_cntr") if not arbeitsplatz.empty else pd.DataFrame(
        columns=["sektor", "transport_modus"])
    modus_map = ap["transport_modus"].dropna().to_dict() if "transport_modus" in ap else {}
    trig["modus"] = trig["von_work_cntr"].map(modus_map).fillna(standard_modus)
    trig = trig[(trig["modus"] == "teil") | (trig["rueck_type_id"] == "C_FRTG")]
    stat["nach_modusfilter"] = len(trig)
    if trig.empty:
        return leer

    # Mehrere Meldungen desselben Vorgangs im selben Lauf -> ein Transport
    trig = trig.sort_values(["wt_id", "rueck_ts", "rueck_id"])
    agg = trig.groupby("wt_id", as_index=False).agg(
        rueck_id=("rueck_id", "last"),
        rueck_ts=("rueck_ts", "last"),
        ist_res=("ist_res", "last"),
        modus=("modus", "last"),
        hat_fertig=("rueck_type_id", lambda s: bool((s == "C_FRTG").any())),
    )

    # Von-Vorgang + Nachfolger dazuholen
    von = vorgang_info.loc[agg["wt_id"]].reset_index()
    von["afo_int"] = _afo_int(von["afo_nr"])
    df = agg.merge(von, on="wt_id", how="left")
    df = df.merge(
        kette[["pps_order", "afo_int"] + [c for c in kette.columns if c.startswith("nach_")]],
        on=["pps_order", "afo_int"], how="left",
    )

    letzter = df["nach_wt_id"].isna()
    stat["letzter_vorgang"] = int(letzter.sum())
    df = df[~letzter]

    nach_fertig = df["nach_wt_status_id"] == "C_FRTG"
    stat["nachfolger_schon_fertig"] = int(nach_fertig.sum())
    df = df[~nach_fertig]

    # Transportregel
    sektor_map = ap["sektor"].dropna().to_dict() if "sektor" in ap else {}
    noetig = df.apply(
        lambda r: transport_noetig(
            r["work_cntr"], r["nach_work_cntr"],
            sektor_map.get(r["work_cntr"]), sektor_map.get(r["nach_work_cntr"]),
            fallback_regel,
        ),
        axis=1,
    ) if not df.empty else pd.Series(dtype=bool)
    stat["kein_transport_noetig"] = int((~noetig).sum()) if len(noetig) else 0
    df = df[noetig] if len(noetig) else df

    if df.empty:
        return Ergebnis(pd.DataFrame(columns=TRANSPORT_COLS), stat)

    # Menge: Gutmenge minus bereits verteilte Menge (stornierte zaehlen nicht)
    b = bisherige.copy() if bisherige is not None else pd.DataFrame(
        columns=["von_wt_id", "quelle_key", "menge", "status"])
    if not b.empty:
        b = b[b["status"] != "storniert"]
    verteilt = b.groupby("von_wt_id")["menge"].sum(min_count=1) if not b.empty else pd.Series(dtype=float)
    vorhandene_keys = set(b["quelle_key"]) if not b.empty else set()

    rows, uebersprungen_menge, schon_da = [], 0, 0
    for r in df.itertuples(index=False):
        gut = r.qty_gut if pd.notna(r.qty_gut) else None
        bereits = verteilt.get(r.wt_id, 0.0)
        bereits = 0.0 if pd.isna(bereits) else float(bereits)
        delta = (gut - bereits) if gut is not None else None

        if r.modus == "voll":
            key = f"V|{r.wt_id}"
            menge = delta if (delta is not None and delta > 0) else gut
        else:
            key = f"T|{r.rueck_id}"
            if delta is not None and delta > 0:
                menge = delta
            elif r.hat_fertig:
                menge = None  # Fertigmeldung ohne erkennbare Restmenge: trotzdem transportieren
            else:
                uebersprungen_menge += 1
                continue

        if key in vorhandene_keys:
            schon_da += 1
            continue

        rows.append({
            "quelle_key": key,
            "modus": r.modus,
            "pps_order": r.pps_order,
            "material_nr": r.material_nr,
            "material_text": r.material_text,
            "psp": r.psp,
            "von_wt_id": r.wt_id,
            "von_afo": r.afo_nr,
            "von_vorgang_text": r.vorgang_text,
            "von_work_cntr": r.work_cntr,
            "von_res": r.plan_res,
            "von_res_typ": r.plan_res_typ,
            "ist_res": r.ist_res,
            "nach_wt_id": r.nach_wt_id,
            "nach_afo": r.nach_afo_nr,
            "nach_vorgang_text": r.nach_vorgang_text,
            "nach_work_cntr": r.nach_work_cntr,
            "nach_res": r.nach_plan_res,
            "nach_res_typ": r.nach_plan_res_typ,
            "menge": float(menge) if menge is not None else None,
            "menge_soll": float(r.qty_soll) if pd.notna(r.qty_soll) else None,
            "ist_teilmenge": not bool(r.hat_fertig),
            "rueck_id": r.rueck_id,
            "rueck_ts": r.rueck_ts,
        })

    stat["keine_neue_menge"] = uebersprungen_menge
    stat["schon_vorhanden"] = schon_da
    out = pd.DataFrame(rows, columns=TRANSPORT_COLS)
    stat["neue_transporte"] = len(out)
    return Ergebnis(out, stat)


def finde_auto_erledigt(offene: pd.DataFrame, events_nach: pd.DataFrame,
                        status_nach: pd.DataFrame | None = None) -> pd.DataFrame:
    """Offene/uebernommene Transporte, deren Ware nachweislich angekommen ist.

    offene       id, nach_wt_id, rueck_ts
    events_nach  wt_id, rueck_ts, rueck_type_id   (Rueckmeldungen an den Nach-Vorgaengen)
    status_nach  wt_id, wt_status_id              (optional, aktueller Vorgangsstatus)

    Regel: Nach dem Ausloeser gab es am Nach-Vorgang C_START oder C_FRTG,
    oder der Nach-Vorgang ist inzwischen komplett fertig (C_FRTG).
    Rueckgabe: id, erledigt_ts (None = Zeitpunkt unbekannt -> jetzt)
    """
    if offene.empty:
        return pd.DataFrame(columns=["id", "erledigt_ts", "grund"])

    treffer = []
    if not events_nach.empty:
        e = events_nach[events_nach["rueck_type_id"].isin(AUTO_ERLEDIGT_TYPEN)]
        m = offene.merge(e, left_on="nach_wt_id", right_on="wt_id", suffixes=("", "_e"))
        m = m[m["rueck_ts_e"] > m["rueck_ts"]]
        if not m.empty:
            first = m.groupby("id", as_index=False).agg(
                erledigt_ts=("rueck_ts_e", "min"), typ=("rueck_type_id", "first"))
            first["grund"] = "Rückmeldung " + first["typ"] + " am Folgevorgang"
            treffer.append(first[["id", "erledigt_ts", "grund"]])

    if status_nach is not None and not status_nach.empty:
        fertig = set(status_nach.loc[status_nach["wt_status_id"] == "C_FRTG", "wt_id"])
        f = offene[offene["nach_wt_id"].isin(fertig)][["id"]].copy()
        if not f.empty:
            f["erledigt_ts"] = pd.NaT
            f["grund"] = "Folgevorgang fertig gemeldet"
            treffer.append(f)

    if not treffer:
        return pd.DataFrame(columns=["id", "erledigt_ts", "grund"])
    return pd.concat(treffer, ignore_index=True).drop_duplicates("id", keep="first")
