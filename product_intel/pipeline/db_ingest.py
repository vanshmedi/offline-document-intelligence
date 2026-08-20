"""
DuckDB analytics store.

Three normalized tables instead of the predecessor's single wide one, with
indexes and a delta-merge upsert rather than a full delete-and-reinsert.

The attributes table is where catalog-scale questions get answered: coverage by
category, conflict rates, confidence distributions, which attributes are
systematically missing across a manufacturer's line.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import duckdb

from product_intel.config import Settings, settings as global_settings
from product_intel.models import Product

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    product_id        VARCHAR PRIMARY KEY,
    manufacturer      VARCHAR,
    mpn               VARCHAR,
    normalized_mpn    VARCHAR,
    gtin              VARCHAR,
    series            VARCHAR,
    base_product_id   VARCHAR,
    category_id       VARCHAR,
    category_confidence DOUBLE,
    status            VARCHAR,
    product_name      VARCHAR,
    completeness_core       DOUBLE,
    completeness_ecommerce  DOUBLE,
    completeness_enhanced   DOUBLE,
    accuracy          DOUBLE,
    consistency       DOUBLE,
    distinctiveness   DOUBLE,
    quality_overall   DOUBLE,
    channel_ready     BOOLEAN,
    conflict_count    INTEGER,
    source_count      INTEGER,
    updated_at        VARCHAR
);

CREATE TABLE IF NOT EXISTS attributes (
    product_id      VARCHAR,
    code            VARCHAR,
    value_text      VARCHAR,
    value_number    DOUBLE,
    unit            VARCHAR,
    raw_value       VARCHAR,
    confidence      DOUBLE,
    source_id       VARCHAR,
    source_kind     VARCHAR,
    method          VARCHAR,
    page            INTEGER,
    locator         VARCHAR,
    quote           VARCHAR,
    quote_verified  BOOLEAN,
    is_inferred     BOOLEAN,
    inference_strategy VARCHAR,
    validation_errors  VARCHAR
);

CREATE TABLE IF NOT EXISTS conflicts (
    product_id      VARCHAR,
    code            VARCHAR,
    winning_value   VARCHAR,
    winning_source  VARCHAR,
    losing_count    INTEGER,
    resolution_rule VARCHAR,
    severity        VARCHAR,
    losing_values   VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_attr_product ON attributes(product_id);
CREATE INDEX IF NOT EXISTS idx_attr_code    ON attributes(code);
CREATE INDEX IF NOT EXISTS idx_prod_cat     ON products(category_id);
CREATE INDEX IF NOT EXISTS idx_prod_mfr     ON products(manufacturer);
CREATE INDEX IF NOT EXISTS idx_conf_product ON conflicts(product_id);
"""

#: Statement prefixes that may run against the analytics database.
_READ_ONLY_PREFIXES = ("SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN", "PRAGMA TABLE_INFO", "SUMMARIZE")

#: Explicitly refused even in read-only mode: DuckDB can write files and reach
#: the filesystem through these, so a keyword blocklist alone is not enough.
_FORBIDDEN = (
    "ATTACH", "COPY", "INSTALL", "LOAD", "EXPORT", "IMPORT",
    "CREATE", "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CALL", "SET ", "RESET",
)


class CatalogDB:
    def __init__(self, cfg: Optional[Settings] = None) -> None:
        self.cfg = cfg or global_settings
        self.path = self.cfg.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self, read_only: bool = False):
        if read_only and not self.path.exists():
            raise FileNotFoundError(f"No catalog database at {self.path}. Run ingest first.")
        return duckdb.connect(str(self.path), read_only=read_only)

    def initialize(self) -> None:
        with self.connect() as conn:
            for stmt in SCHEMA_SQL.strip().split(";"):
                if stmt.strip():
                    conn.execute(stmt)

    def rebuild(self, products: Sequence[Product]) -> Dict[str, int]:
        """Drop and rebuild every table from the product JSON files on disk."""
        with self.connect() as conn:
            for table in ("attributes", "conflicts", "products"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            for stmt in SCHEMA_SQL.strip().split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            counts = self._insert(conn, products)
        return counts

    def upsert(self, products: Sequence[Product]) -> Dict[str, int]:
        """Replace rows for the given products only. Leaves the rest untouched."""
        self.initialize()
        ids = [p.identity.product_id for p in products]
        if not ids:
            return {"products": 0, "attributes": 0, "conflicts": 0}
        with self.connect() as conn:
            placeholders = ",".join("?" for _ in ids)
            for table in ("attributes", "conflicts", "products"):
                conn.execute(f"DELETE FROM {table} WHERE product_id IN ({placeholders})", ids)
            counts = self._insert(conn, products)
        return counts

    def _insert(self, conn, products: Sequence[Product]) -> Dict[str, int]:
        prod_rows: List[tuple] = []
        attr_rows: List[tuple] = []
        conflict_rows: List[tuple] = []

        for p in products:
            ident = p.identity
            q = p.quality
            prod_rows.append(
                (
                    ident.product_id, ident.manufacturer, ident.mpn, ident.normalized_mpn,
                    ident.gtin, ident.series, ident.base_product_id, p.category_id,
                    p.category_confidence, p.status.value if hasattr(p.status, "value") else str(p.status),
                    str(p.get("product_name") or ""),
                    q.completeness_core, q.completeness_ecommerce, q.completeness_enhanced,
                    q.accuracy, q.consistency, q.distinctiveness, q.overall, q.channel_ready,
                    len(p.conflicts), len(p.source_ids), p.updated_at,
                )
            )

            for code, av in p.attributes.items():
                is_num = isinstance(av.value, (int, float)) and not isinstance(av.value, bool)
                if isinstance(av.value, list):
                    text = json.dumps(av.value, ensure_ascii=False)
                else:
                    text = None if av.value is None else str(av.value)
                ev = av.evidence
                attr_rows.append(
                    (
                        ident.product_id, code, text,
                        float(av.value) if is_num else None,
                        av.unit, av.raw_value, av.confidence,
                        ev.source_id if ev else None,
                        ev.source_kind.value if ev else None,
                        ev.method.value if ev else None,
                        ev.page if ev else None,
                        ev.locator if ev else None,
                        ev.quote if ev else None,
                        bool(ev.quote_verified) if ev else False,
                        av.inference is not None,
                        av.inference.strategy if av.inference else None,
                        "; ".join(av.validation_errors) if av.validation_errors else None,
                    )
                )

            for c in p.conflicts:
                conflict_rows.append(
                    (
                        ident.product_id, c.code, str(c.winning_value), c.winning_source,
                        len(c.losing_values), c.resolution_rule, c.severity,
                        json.dumps(c.losing_values, ensure_ascii=False, default=str),
                    )
                )

        if prod_rows:
            conn.executemany(
                f"INSERT INTO products VALUES ({','.join('?' * 22)})", prod_rows
            )
        if attr_rows:
            conn.executemany(
                f"INSERT INTO attributes VALUES ({','.join('?' * 17)})", attr_rows
            )
        if conflict_rows:
            conn.executemany(
                f"INSERT INTO conflicts VALUES ({','.join('?' * 8)})", conflict_rows
            )

        return {
            "products": len(prod_rows),
            "attributes": len(attr_rows),
            "conflicts": len(conflict_rows),
        }

    # -- querying -----------------------------------------------------------

    def is_read_only_sql(self, sql: str) -> tuple[bool, str]:
        """
        Decide whether a statement is safe to run.

        Allowlist on the leading keyword plus a denylist of DuckDB verbs that
        reach the filesystem. The predecessor checked only for INSERT/UPDATE/
        DELETE/DROP/ALTER as substrings, which let COPY ... TO and ATTACH
        through -- both of which can write arbitrary files.
        """
        stripped = re.sub(r"--[^\n]*", " ", sql)
        stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.DOTALL).strip()
        if not stripped:
            return False, "empty statement"
        if ";" in stripped.rstrip(";"):
            return False, "multiple statements are not permitted"

        upper = stripped.upper()
        if not any(upper.startswith(p) for p in _READ_ONLY_PREFIXES):
            return False, f"only read statements are permitted ({', '.join(_READ_ONLY_PREFIXES[:4])}...)"
        for verb in _FORBIDDEN:
            if re.search(rf"(?<![A-Z_]){re.escape(verb.strip())}(?![A-Z_])", upper):
                return False, f"'{verb.strip()}' is not permitted"
        return True, ""

    def query(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        """Run a read-only query. Enforced both by validation and a read-only connection."""
        ok, reason = self.is_read_only_sql(sql)
        if not ok:
            raise ValueError(f"Refused to execute: {reason}")
        with self.connect(read_only=True) as conn:
            cur = conn.execute(sql, list(params) if params else [])
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def table_summary(self) -> Dict[str, int]:
        if not self.path.exists():
            return {}
        with self.connect(read_only=True) as conn:
            out = {}
            for table in ("products", "attributes", "conflicts"):
                try:
                    out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except duckdb.Error:
                    out[table] = 0
            return out
