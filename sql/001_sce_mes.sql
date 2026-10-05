-- sce_mes — Tabellen fuer das Stapler-Leitsystem (Milk Run)
-- GENERIERT aus src/mes_sync/schema.py via `uv run python -m mes_sync ddl` — nicht von Hand aendern.
-- Anlegen bitte ueber `uv run python -m mes_sync init-db` (idempotent).

IF SCHEMA_ID('sce_mes') IS NULL EXEC('CREATE SCHEMA sce_mes');
GO

CREATE TABLE sce_mes.arbeitsplatz (
	work_cntr VARCHAR(20) NOT NULL, 
	bezeichnung NVARCHAR(200) NULL, 
	res_typ VARCHAR(20) NULL, 
	sektor NVARCHAR(50) NULL, 
	transport_modus VARCHAR(10) NULL, 
	lagerort_code NVARCHAR(50) NULL, 
	aktualisiert_am DATETIME2(3) NOT NULL, 
	PRIMARY KEY (work_cntr), 
	CONSTRAINT ck_arbeitsplatz_modus CHECK (transport_modus IS NULL OR transport_modus IN ('teil','voll'))
);
GO

CREATE TABLE sce_mes.fahrer (
	name NVARCHAR(100) NOT NULL, 
	aktiv BIT NOT NULL, 
	PRIMARY KEY (name)
);
GO

CREATE TABLE sce_mes.poller_status (
	id SMALLINT NOT NULL, 
	letzter_lauf DATETIME2(3) NULL, 
	letzter_erfolg DATETIME2(3) NULL, 
	wasserzeichen DATETIME2(3) NULL, 
	anzahl_neu INTEGER NULL, 
	meldung NVARCHAR(1000) NULL, 
	host NVARCHAR(100) NULL, 
	PRIMARY KEY (id)
);
GO

CREATE TABLE sce_mes.pps_rueckmeldung (
	rueck_id VARCHAR(40) NOT NULL, 
	wt_id VARCHAR(40) NOT NULL, 
	pps_order VARCHAR(20) NULL, 
	afo_nr VARCHAR(10) NULL, 
	rueck_ts DATETIME2(3) NOT NULL, 
	rueck_type_id VARCHAR(20) NOT NULL, 
	wkpl_res_id VARCHAR(40) NULL, 
	ist_res NVARCHAR(200) NULL, 
	ist_res_typ VARCHAR(20) NULL, 
	geladen_am DATETIME2(3) NOT NULL, 
	PRIMARY KEY (rueck_id)
);
GO

CREATE INDEX ix_rueck_ts ON sce_mes.pps_rueckmeldung (rueck_ts);
GO

CREATE INDEX ix_rueck_wt_ts ON sce_mes.pps_rueckmeldung (wt_id, rueck_ts);
GO

CREATE TABLE sce_mes.pps_vorgang (
	wt_id VARCHAR(40) NOT NULL, 
	pps_order VARCHAR(20) NOT NULL, 
	afo_nr VARCHAR(10) NOT NULL, 
	vorgang_text NVARCHAR(400) NULL, 
	wt_status_id VARCHAR(20) NULL, 
	work_cntr VARCHAR(20) NULL, 
	plan_res_id VARCHAR(40) NULL, 
	plan_res NVARCHAR(200) NULL, 
	plan_res_typ VARCHAR(20) NULL, 
	qty_soll FLOAT NULL, 
	qty_gut FLOAT NULL, 
	material_nr NVARCHAR(40) NULL, 
	material_text NVARCHAR(400) NULL, 
	werk VARCHAR(10) NULL, 
	psp NVARCHAR(40) NULL, 
	aktualisiert_am DATETIME2(3) NOT NULL, 
	PRIMARY KEY (wt_id)
);
GO

CREATE INDEX ix_vorgang_order ON sce_mes.pps_vorgang (pps_order);
GO

CREATE TABLE sce_mes.transport_auftrag (
	id INTEGER NOT NULL IDENTITY, 
	quelle_key VARCHAR(60) NOT NULL, 
	modus VARCHAR(10) NOT NULL, 
	pps_order VARCHAR(20) NOT NULL, 
	material_nr NVARCHAR(40) NULL, 
	material_text NVARCHAR(400) NULL, 
	psp NVARCHAR(40) NULL, 
	von_wt_id VARCHAR(40) NOT NULL, 
	von_afo VARCHAR(10) NULL, 
	von_vorgang_text NVARCHAR(400) NULL, 
	von_work_cntr VARCHAR(20) NULL, 
	von_res NVARCHAR(200) NULL, 
	von_res_typ VARCHAR(20) NULL, 
	ist_res NVARCHAR(200) NULL, 
	nach_wt_id VARCHAR(40) NOT NULL, 
	nach_afo VARCHAR(10) NULL, 
	nach_vorgang_text NVARCHAR(400) NULL, 
	nach_work_cntr VARCHAR(20) NULL, 
	nach_res NVARCHAR(200) NULL, 
	nach_res_typ VARCHAR(20) NULL, 
	menge FLOAT NULL, 
	menge_soll FLOAT NULL, 
	ist_teilmenge BIT NOT NULL, 
	rueck_id VARCHAR(40) NULL, 
	rueck_ts DATETIME2(3) NULL, 
	status VARCHAR(20) NOT NULL, 
	erstellt_am DATETIME2(3) NOT NULL, 
	uebernommen_von NVARCHAR(100) NULL, 
	uebernommen_am DATETIME2(3) NULL, 
	erledigt_von NVARCHAR(100) NULL, 
	erledigt_am DATETIME2(3) NULL, 
	geaendert_am DATETIME2(3) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_transport_status CHECK (status IN ('offen','uebernommen','erledigt','auto_erledigt','storniert')), 
	CONSTRAINT ck_transport_modus CHECK (modus IN ('teil','voll')), 
	UNIQUE (quelle_key)
);
GO

CREATE INDEX ix_transport_erstellt ON sce_mes.transport_auftrag (erstellt_am);
GO

CREATE INDEX ix_transport_status ON sce_mes.transport_auftrag (status);
GO

CREATE INDEX ix_transport_von_wt ON sce_mes.transport_auftrag (von_wt_id);
GO

CREATE TABLE sce_mes.transport_event (
	id INTEGER NOT NULL IDENTITY, 
	transport_id INTEGER NOT NULL, 
	event VARCHAR(20) NOT NULL, 
	fahrer NVARCHAR(100) NULL, 
	ts DATETIME2(3) NOT NULL, 
	info NVARCHAR(400) NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(transport_id) REFERENCES sce_mes.transport_auftrag (id)
);
GO

CREATE INDEX ix_event_transport ON sce_mes.transport_event (transport_id);
GO

CREATE INDEX ix_event_ts ON sce_mes.transport_event (ts);
GO

INSERT INTO sce_mes.poller_status (id) VALUES (1);
GO

INSERT INTO sce_mes.fahrer (name, aktiv) VALUES (N'Fahrer 1', 1), (N'Fahrer 2', 1);
GO
