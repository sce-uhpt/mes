"""Extract aus Proxia (nur lesend, User 'report').

Quellen (siehe PROXIA_DB.md und README):
- dbo.TSF_RUECKMELDUNG  Ereignisse je Vorgang: RUECK_ID, RUECK_TS (UTC!), RUECK_TYPE_ID
                        (C_START, C_TFRTG = teilfertig, C_FRTG = fertig, C_RUECK),
                        WKPL_RES_ID = Ist-Arbeitsplatz/Maschine
- dbo.TSF_WT            Vorgaenge inkl. bereits fertiger (Status C_FRTG) — im Gegensatz
                        zu VSF_U_UHPT_PLANDATEN_2, das fertige Vorgaenge ausblendet
- dbo.TRS_RES           Ressourcenstamm. RES_TYPE_ID: C_WKPL, C_MACH, C_DELIVERER (Fremdfertiger),
                        C_PERS (Person!). Deshalb NIE TSF_WT_QTY.RES_ID als Standort verwenden.
"""

import pandas as pd
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

ORDER_CHUNK = 500

SQL_EVENTS = text("""
    SELECT RM.RUECK_ID, RM.WT_ID, RM.RUECK_TS, RM.RUECK_TYPE_ID, RM.WKPL_RES_ID,
           R.DISPLAYNAME AS IST_RES, R.RES_TYPE_ID AS IST_RES_TYP,
           WT.PPS_ORDER, WT.AFO_NR
    FROM dbo.TSF_RUECKMELDUNG AS RM
    JOIN dbo.TSF_WT AS WT ON WT.WT_ID = RM.WT_ID
    LEFT JOIN dbo.TRS_RES AS R ON R.RES_ID = RM.WKPL_RES_ID
    WHERE RM.RUECK_TS > :seit
""")

SQL_VORGAENGE = text("""
    SELECT WT.WT_ID, WT.PPS_ORDER, WT.AFO_NR, WT.DISPLAYNAME AS VORGANG_TEXT,
           WT.WT_STATUS_ID, WT.PPS_WORK_CNTR, WT.PLANNED_RES_ID,
           R.DISPLAYNAME AS PLAN_RES, R.RES_TYPE_ID AS PLAN_RES_TYP,
           WT.QTY_SOLL, WT.QTY_CONFIRMED_GUT, WT.PPS_ART_NR, WT.PPS_ART_DISPLAYNAME,
           WT.PPS_PLANT, PSP.PSP_ELEMENT_NR
    FROM dbo.TSF_WT AS WT
    LEFT JOIN dbo.TRS_RES AS R ON R.RES_ID = WT.PLANNED_RES_ID
    LEFT JOIN dbo.TSF_FA AS FA ON FA.FA_ID = WT.FA_ID
    LEFT JOIN dbo.TSF_PSP_ELEMENT AS PSP ON PSP.PSP_ELEMENT_ID = FA.PSP_ELEMENT_ID
    WHERE WT.WT_DELETED = 0 AND WT.PPS_ORDER IN :orders
""").bindparams(bindparam("orders", expanding=True))

EVENT_COLS = {
    "RUECK_ID": "rueck_id", "WT_ID": "wt_id", "RUECK_TS": "rueck_ts",
    "RUECK_TYPE_ID": "rueck_type_id", "WKPL_RES_ID": "wkpl_res_id",
    "IST_RES": "ist_res", "IST_RES_TYP": "ist_res_typ",
    "PPS_ORDER": "pps_order", "AFO_NR": "afo_nr",
}

VORGANG_COLS = {
    "WT_ID": "wt_id", "PPS_ORDER": "pps_order", "AFO_NR": "afo_nr",
    "VORGANG_TEXT": "vorgang_text", "WT_STATUS_ID": "wt_status_id",
    "PPS_WORK_CNTR": "work_cntr", "PLANNED_RES_ID": "plan_res_id",
    "PLAN_RES": "plan_res", "PLAN_RES_TYP": "plan_res_typ",
    "QTY_SOLL": "qty_soll", "QTY_CONFIRMED_GUT": "qty_gut",
    "PPS_ART_NR": "material_nr", "PPS_ART_DISPLAYNAME": "material_text",
    "PPS_PLANT": "werk", "PSP_ELEMENT_NR": "psp",
}

_STRIP = ["wt_id", "pps_order", "afo_nr", "rueck_id", "rueck_type_id", "work_cntr",
          "wkpl_res_id", "plan_res_id", "material_nr", "werk", "wt_status_id"]


def _clean(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    df = df.rename(columns={k: v for k, v in mapping.items() if k in df.columns})
    df = df[[c for c in mapping.values() if c in df.columns]].copy()
    for col in _STRIP:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip()
    return df


def fetch_events(engine: Engine, seit) -> pd.DataFrame:
    """Alle Rueckmeldungen seit `seit` (naive UTC-Zeit)."""
    with engine.connect() as con:
        df = pd.read_sql(SQL_EVENTS, con, params={"seit": seit})
    df = _clean(df, EVENT_COLS)
    if not df.empty:
        df["rueck_ts"] = pd.to_datetime(df["rueck_ts"])
    return df


def fetch_vorgaenge(engine: Engine, orders) -> pd.DataFrame:
    """Kompletter Arbeitsplan (alle nicht geloeschten Vorgaenge) der gegebenen Auftraege."""
    orders = sorted({str(o).strip() for o in orders if pd.notna(o) and str(o).strip()})
    frames = []
    with engine.connect() as con:
        for i in range(0, len(orders), ORDER_CHUNK):
            chunk = orders[i:i + ORDER_CHUNK]
            frames.append(pd.read_sql(SQL_VORGAENGE, con, params={"orders": chunk}))
    if not frames:
        return pd.DataFrame(columns=list(VORGANG_COLS.values()))
    df = _clean(pd.concat(frames, ignore_index=True), VORGANG_COLS)
    for col in ("qty_soll", "qty_gut"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.drop_duplicates("wt_id")
