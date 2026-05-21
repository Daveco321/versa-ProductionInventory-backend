"""
Versa Inventory Ledger — Backend API
=====================================

A small Flask service that backs the Versa Inventory Ledger frontend.

Why this exists
---------------
The frontend previously stored everything in the browser's localStorage, which
is per-browser and per-device. That doesn't work for a company-wide platform.
This backend gives every user a single shared source of truth, and survives
machine swaps / browser-data clears.

Data model
----------
Three tables (full schema in models.py-style docstring below):

  ledger          — one row per base style. quantity = current ledger qty.
                    Updated atomically by upload commits.
  uploads         — one row per packing-list / invoice-report upload.
                    Stores the parsed item list as JSON.
  upload_items    — denormalized per-style line items so we can revert an
                    upload without re-parsing the original file.

Authentication
--------------
For now the API is open (same origin as the frontend, behind your existing
auth gate if any). All write endpoints accept an optional `X-Reset-Password`
header for the destructive reset operation. Wire this up to your real auth
when you have it — there's a single `require_auth()` shim near the top.

Endpoints
---------
  GET  /health                  — liveness probe
  GET  /ledger                  — full ledger (all styles)
  POST /ledger/seed             — one-time seed from /inventory of existing platform
  POST /ledger/reset            — reset everything (password-gated)
  GET  /uploads                 — upload history
  POST /uploads                 — commit a new upload (applies to ledger)
  PATCH /uploads/<id>           — edit an upload's items (re-applies to ledger)
  DELETE /uploads/<id>          — soft-delete + reverse the upload's effect

Deploy notes
------------
- Set DATABASE_URL env var. On Render Postgres free tier this is auto-injected.
- Set EXISTING_PLATFORM_URL to the existing API base (where /inventory lives).
- Set RESET_PASSWORD env var to the password used for the reset endpoint.
- Set ALLOWED_ORIGINS to a comma-separated list of frontend domains.
- See render.yaml for one-click Render deploy.
"""
import os
import json
import logging
import datetime as dt
from functools import wraps

from flask import Flask, jsonify, request, abort, make_response
from flask_cors import CORS
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, Text, DateTime,
    JSON, ForeignKey, Index, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
import requests

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///versa_ledger.db')
# Render Postgres uses postgres:// but SQLAlchemy 1.4+ requires postgresql://
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

EXISTING_PLATFORM_URL = os.environ.get(
    'EXISTING_PLATFORM_URL',
    'https://versa-inventory-api.onrender.com'
).rstrip('/')
# Separate service that exposes Ross's forward-looking open orders.
# Note: this service has a hostname allowlist — your backend's Render domain
# may need to be added on their end for the sync to succeed.
OPEN_ORDERS_API_URL = os.environ.get(
    'OPEN_ORDERS_API_URL',
    'https://open-orders-api.onrender.com'
).rstrip('/')
RESET_PASSWORD = os.environ.get('RESET_PASSWORD', 'Versa1211')

# CORS: comma-separated origins, or '*' for any (NOT recommended in prod).
_origins_raw = os.environ.get('ALLOWED_ORIGINS', '*')
ALLOWED_ORIGINS = (
    '*' if _origins_raw.strip() == '*'
    else [o.strip() for o in _origins_raw.split(',') if o.strip()]
)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger('versa-ledger')

# ──────────────────────────────────────────────────────────────────────────────
# Database setup
# ──────────────────────────────────────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,        # heal stale connections (esp. on Render free tier)
    pool_recycle=3600,
    pool_size=5,
    max_overflow=10,
)
SessionFactory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
db_session = scoped_session(SessionFactory)
Base = declarative_base()
Base.query = db_session.query_property()


class Ledger(Base):
    """
    Current authoritative quantity per base style.
    `quantity` is mutated by packing list (+) and invoice report (−) uploads ONLY —
    NEVER touched by the hourly sync.
    `committed` and `allocated` are OVERWRITTEN by the hourly sync from the
    existing platform — they're snapshots of what's spoken for, not local edits.
    Total ATS (the available-to-sell number shown in the UI) = quantity − committed − allocated.
    """
    __tablename__ = 'ledger'
    style = Column(String(64), primary_key=True)             # base style (no size suffix)
    quantity = Column(Integer, nullable=False, default=0)
    brand_id = Column(String(32), nullable=True, index=True)  # e.g. 'NAUTICA'
    brand_data = Column(JSON, nullable=True)                  # full brand obj snapshot
    color = Column(String(128), nullable=True)
    last_updated = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    seeded = Column(Boolean, default=False)                   # came from seed action
    # Synced from existing platform hourly. Stored as POSITIVE integers
    # (we abs() the values from the upstream because they can be signed there).
    committed = Column(Integer, nullable=False, default=0)
    allocated = Column(Integer, nullable=False, default=0)
    committed_synced_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self):
        ats = (self.quantity or 0) - (self.committed or 0) - (self.allocated or 0)
        return {
            'style': self.style,
            'quantity': self.quantity,                  # "Total units" — packing list − invoice
            'committed': self.committed or 0,
            'allocated': self.allocated or 0,
            'totalAts': ats,                            # quantity − committed − allocated
            'brand': self.brand_data,
            'color': self.color or '',
            'lastUpdated': self.last_updated.isoformat() if self.last_updated else None,
            'committedSyncedAt': self.committed_synced_at.isoformat() if self.committed_synced_at else None,
            'seeded': bool(self.seeded),
        }


class Upload(Base):
    """
    One record per packing-list or invoice-report upload.
    `items` is denormalized into `upload_items` for fast reverts.
    """
    __tablename__ = 'uploads'
    id = Column(String(64), primary_key=True)            # client-generated UUID-ish
    type = Column(String(32), nullable=False)            # 'packing_list' | 'invoice_report' | 'open_orders'
    uploaded_at = Column(DateTime(timezone=True), default=func.now())
    last_edited_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    file_names = Column(JSON, nullable=True)             # list[str]
    item_count = Column(Integer, default=0)
    grand_total = Column(Integer, default=0)             # signed: invoices store negative
    parser_meta = Column(JSON, nullable=True)            # warnings, raw row counts, etc.

    def to_dict(self, items=None):
        return {
            'id': self.id,
            'type': self.type,
            'uploadedAt': self.uploaded_at.isoformat() if self.uploaded_at else None,
            'lastEditedAt': self.last_edited_at.isoformat() if self.last_edited_at else None,
            'deletedAt': self.deleted_at.isoformat() if self.deleted_at else None,
            'fileNames': self.file_names or [],
            'itemCount': self.item_count,
            'grandTotal': self.grand_total,
            'parserMeta': self.parser_meta or {},
            'items': items if items is not None else [],
        }


class UploadItem(Base):
    """
    Per-style line item for a given upload. Kept separate from Upload so a
    later edit (or revert) can recompute the net effect on the Ledger.
    Signed quantity: packing list = positive, invoice = negative.
    """
    __tablename__ = 'upload_items'
    id = Column(Integer, primary_key=True, autoincrement=True)
    upload_id = Column(String(64), ForeignKey('uploads.id', ondelete='CASCADE'),
                       nullable=False, index=True)
    style = Column(String(64), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)                # signed
    brand_id = Column(String(32), nullable=True)
    brand_data = Column(JSON, nullable=True)
    color = Column(String(128), nullable=True)

    def to_dict(self):
        return {
            'style': self.style,
            'quantity': self.quantity,
            'brand': self.brand_data,
            'color': self.color or '',
        }


# Composite index for fast (style, upload) lookups during reverts
Index('ix_upload_items_style_upload', UploadItem.style, UploadItem.upload_id)


class SeedInfo(Base):
    """
    Singleton row tracking the most-recent seed/reset action.
    """
    __tablename__ = 'seed_info'
    id = Column(Integer, primary_key=True, default=1)
    seeded_at = Column(DateTime(timezone=True), default=func.now())
    source = Column(String(64), nullable=True)           # 'api' | 'api-reset' | 'manual'
    file_name = Column(String(256), nullable=True)
    row_count = Column(Integer, default=0)
    total_qty = Column(Integer, default=0)
    api_item_count = Column(Integer, default=0)

    def to_dict(self):
        return {
            'seededAt': self.seeded_at.isoformat() if self.seeded_at else None,
            'source': self.source,
            'fileName': self.file_name,
            'rowCount': self.row_count,
            'totalQty': self.total_qty,
            'apiItemCount': self.api_item_count,
        }


# ──────────────────────────────────────────────────────────────────────────────
# READ-ONLY MIRROR TABLES — hourly-synced from existing platform.
# These are NEVER manually edited. The sync REPLACES all rows on each run.
# ──────────────────────────────────────────────────────────────────────────────
class Allocation(Base):
    """
    One row per (sku, customer, po, source) — the per-customer breakdown
    of what's committed/allocated. Source matches existing platform's:
      's3'     — from the canonical Allocation CSV in S3
      'manual' — manual allocations added on top by admin UI
    """
    __tablename__ = 'allocations'
    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String(64), nullable=False, index=True)
    customer = Column(String(64), nullable=True, index=True)
    po = Column(String(128), nullable=True)
    qty = Column(Integer, nullable=False, default=0)
    source = Column(String(16), nullable=False, default='s3')   # 's3' | 'manual'
    synced_at = Column(DateTime(timezone=True), default=func.now())

    def to_dict(self):
        return {
            'sku': self.sku,
            'customer': self.customer or '',
            'po': self.po or '',
            'qty': self.qty,
            'source': self.source,
        }


Index('ix_allocations_style_base', Allocation.sku)


class Production(Base):
    """
    View-only mirror of the existing platform's /production endpoint.
    One row per production-order line. NEVER affects ledger quantity.
    """
    __tablename__ = 'productions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    production = Column(String(64), nullable=True, index=True)  # production # or label
    po_name = Column(String(128), nullable=True)
    style = Column(String(64), nullable=False, index=True)
    units = Column(Integer, nullable=False, default=0)
    brand = Column(String(64), nullable=True)
    etd = Column(String(32), nullable=True)                     # ex-factory date — keep as string (format varies)
    synced_at = Column(DateTime(timezone=True), default=func.now())

    def to_dict(self):
        return {
            'production': self.production or '',
            'poName': self.po_name or '',
            'style': self.style,
            'units': self.units,
            'brand': self.brand or '',
            'etd': self.etd,
        }


class OpenOrder(Base):
    """
    View-only mirror of the open-orders-api service. One row per order line.

    Why a separate table from Allocation: open orders are forward-looking
    demand (the retailer has placed an order, may be on pick, may still be
    open) and have date semantics. Allocations are about deductions against
    the current ATS. The existing platform shows both side-by-side in its
    deductions popup ("A2000 Breakdown" — image 3 from the user).

    Source URL: https://open-orders-api.onrender.com/api/orders
    Field-name fallbacks for `po` and `style` mirror the existing platform's
    multi-key handling (see app__19_.py:6633 — `po | poNumber | customerPO | orderNo | ctrlNo`).
    """
    __tablename__ = 'open_orders'
    id = Column(Integer, primary_key=True, autoincrement=True)
    style = Column(String(64), nullable=False, index=True)      # base style (no size suffix)
    customer = Column(String(64), nullable=True, index=True)
    po = Column(String(128), nullable=True)
    open_qty = Column(Integer, nullable=False, default=0)        # not yet picked
    pick_qty = Column(Integer, nullable=False, default=0)        # already on pick (shows "ON PICK" badge)
    start_date = Column(String(32), nullable=True)               # earliest ship date (string — format varies upstream)
    cancel_date = Column(String(32), nullable=True)              # latest acceptable ship date
    synced_at = Column(DateTime(timezone=True), default=func.now())

    def to_dict(self):
        return {
            'style': self.style,
            'customer': self.customer or '',
            'po': self.po or '',
            'openQty': self.open_qty,
            'pickQty': self.pick_qty,
            'totalQty': (self.open_qty or 0) + (self.pick_qty or 0),
            'startDate': self.start_date,
            'cancelDate': self.cancel_date,
            'onPick': (self.pick_qty or 0) > 0,
        }


class SyncLog(Base):
    """
    Tracks the most recent run of each sync source. Singleton-per-source: we
    upsert by `source` name. Used to:
      1. Compute "X minutes ago" labels in the UI
      2. Decide whether to fire a lazy sync on the next GET (>60min stale)
    """
    __tablename__ = 'sync_log'
    source = Column(String(32), primary_key=True)               # 'inventory_committed' | 'allocations' | 'productions'
    last_synced_at = Column(DateTime(timezone=True), default=func.now())
    last_status = Column(String(16), default='ok')              # 'ok' | 'error'
    last_error = Column(Text, nullable=True)
    rows_synced = Column(Integer, default=0)

    def to_dict(self):
        return {
            'source': self.source,
            'lastSyncedAt': self.last_synced_at.isoformat() if self.last_synced_at else None,
            'lastStatus': self.last_status,
            'lastError': self.last_error,
            'rowsSynced': self.rows_synced,
        }


def init_db():
    """Create tables if they don't exist. Safe to call repeatedly.

    Note: SQLAlchemy's create_all() will ADD new tables but won't ADD columns
    to existing tables. If you deployed an earlier version and need the new
    columns (committed / allocated / committed_synced_at on ledger), either:
      1. Drop the DB and let create_all() rebuild (loses data — only OK on fresh setups)
      2. Or run ALTER TABLE manually — see _ensure_new_columns() below.
    """
    Base.metadata.create_all(bind=engine)
    _ensure_new_columns()
    log.info(f'Database initialized: {DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL}')


def _ensure_new_columns():
    """
    Migration shim: add committed / allocated / committed_synced_at to ledger
    if they don't exist. Idempotent. Avoids needing a full migration tool yet.
    """
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    if 'ledger' not in inspector.get_table_names():
        return  # fresh DB — create_all() already made all columns
    existing_cols = {c['name'] for c in inspector.get_columns('ledger')}
    needed = {
        'committed': 'INTEGER NOT NULL DEFAULT 0',
        'allocated': 'INTEGER NOT NULL DEFAULT 0',
        'committed_synced_at': 'TIMESTAMP WITH TIME ZONE',
    }
    # Postgres uses 'TIMESTAMP WITH TIME ZONE'; SQLite is lenient and accepts 'TIMESTAMP'
    if engine.dialect.name == 'sqlite':
        needed['committed_synced_at'] = 'TIMESTAMP'
    added = []
    with engine.begin() as conn:
        for col, ddl in needed.items():
            if col not in existing_cols:
                try:
                    conn.execute(text(f'ALTER TABLE ledger ADD COLUMN {col} {ddl}'))
                    added.append(col)
                except Exception as e:
                    log.error(f'Failed to add ledger.{col}: {e}')
    if added:
        log.info(f'Migration: added ledger columns {added}')


# ──────────────────────────────────────────────────────────────────────────────
# SYNC FROM EXISTING PLATFORM — pulls committed/allocated/productions hourly
# ──────────────────────────────────────────────────────────────────────────────
SYNC_INTERVAL_SECONDS = 3600        # 1 hour
_sync_in_flight = False             # crude lock to prevent concurrent sync runs


# ──────────────────────────────────────────────────────────────────────────────
# Base-style extraction — MUST match the frontend's extractBaseStyle() exactly.
# Frontend lives in index.html (search TRAILING_SIZE_RE). If you change one you
# MUST change the other or the sync will store data against the wrong key and
# committed/allocated will appear to be 0 in the UI (see Versa Ledger bug: ATS
# = Units because deductions were stored against TJNASU201SLSXXL not TJNASU201SLS).
# ──────────────────────────────────────────────────────────────────────────────
import re as _re
# Require a separator (space or dash) BEFORE the size. Without this, the regex
# would also strip trailing letters that are part of the base style — e.g. it
# would eat the "S" off KHNADS525SLS and turn it into KHNAD.
_TRAILING_SIZE_RE = _re.compile(
    r'[\s-](?:'
    r'\d{1,3}(?:\.5)?(?:[-./]\d{1,3}(?:\.5)?)?'   # numeric sizes: 32, 32-33, 16.5/34, etc
    r'|XXXL|XXL|XL|XS|2XL|3XL|4XL|S|M|L'           # alpha sizes
    r'|SHORT|REGULAR|REG|LONG|TALL|BIG'            # length variants
    r')$',
    _re.IGNORECASE,
)


def _extract_base_style(raw):
    """Strip the trailing size suffix from a SKU to get the base style.
    Iterative because some SKUs have multiple suffixes (e.g. 'FOO-XL-LONG').
    Mirrors the frontend's extractBaseStyle() in index.html — keep these in sync."""
    if not raw:
        return ''
    s = str(raw).strip().upper()
    if not s:
        return ''
    prev = None
    guard = 0
    while s != prev and guard < 8 and s:
        prev = s
        s = _TRAILING_SIZE_RE.sub('', s).strip()
        guard += 1
    # Strip trailing punctuation that might be left behind
    return _re.sub(r'[,;\-\s]+$', '', s).strip()


def _stamp_sync(source, status='ok', error=None, rows=0):
    """Upsert a row in sync_log."""
    row = SyncLog.query.get(source)
    if row is None:
        row = SyncLog(source=source)
        db_session.add(row)
    row.last_synced_at = dt.datetime.utcnow()
    row.last_status = status
    row.last_error = error
    row.rows_synced = rows
    db_session.commit()


def _maybe_lazy_sync():
    """
    Called before serving GET /ledger. If any sync source is older than
    SYNC_INTERVAL_SECONDS, fire a non-blocking sync in a thread so the
    current request returns immediately with whatever's currently in DB.
    Next page load will see fresh data.
    """
    global _sync_in_flight
    if _sync_in_flight:
        return
    needed_sources = ['inventory_committed', 'allocations', 'productions', 'open_orders']
    now = dt.datetime.utcnow()
    needs = False
    for src in needed_sources:
        row = SyncLog.query.get(src)
        if row is None:
            needs = True
            break
        last = row.last_synced_at
        # Strip tz for comparison if needed
        if hasattr(last, 'tzinfo') and last.tzinfo is not None:
            last = last.replace(tzinfo=None)
        if (now - last).total_seconds() > SYNC_INTERVAL_SECONDS:
            needs = True
            break
    if not needs:
        return
    import threading
    _sync_in_flight = True
    threading.Thread(target=_run_full_sync_threadsafe, daemon=True).start()


def _run_full_sync_threadsafe():
    """Thread entry point — needs its own DB session because scoped_session is per-request."""
    global _sync_in_flight
    try:
        # Each background thread needs its own session lifecycle
        run_full_sync()
    except Exception as e:
        log.error(f'Background sync failed: {e}', exc_info=True)
    finally:
        _sync_in_flight = False
        db_session.remove()


def run_full_sync():
    """
    Pull all three data sources from the existing platform and overwrite our
    mirror tables. Each source is independent — if one fails, the others
    still get updated.
    """
    log.info('Starting full sync from existing platform')

    # 1. /inventory — pulls per-SKU committed and allocated, rolls them up to base styles
    try:
        r = requests.get(f'{EXISTING_PLATFORM_URL}/inventory', timeout=60)
        r.raise_for_status()
        data = r.json()
        items = data.get('inventory') or data.get('items') or []
        # Key committed/allocated by FULL SKU (no rollup). The frontend's seed
        # also stores SKUs at the full upstream level (no extractBaseStyle), so
        # both sides agree on what a "ledger row" is. If we rolled up here but
        # the seed didn't, the sync would write to non-existent base-style keys
        # and leave variant rows showing zero deductions forever.
        per_sku = {}  # sku -> {committed, allocated}
        sample_skus_logged = 0
        for it in items:
            sku = str(it.get('sku', '')).strip().upper()
            if not sku:
                continue
            # Defensive parsing: some sources return committed/allocated as strings
            try:
                committed_raw = it.get('committed', 0)
                committed = abs(int(float(committed_raw or 0)))
            except (ValueError, TypeError):
                committed = 0
            try:
                allocated_raw = it.get('allocated', 0)
                allocated = abs(int(float(allocated_raw or 0)))
            except (ValueError, TypeError):
                allocated = 0
            cur = per_sku.setdefault(sku, {'committed': 0, 'allocated': 0})
            cur['committed'] += committed
            cur['allocated'] += allocated
            # Log a few samples on each sync for debugging — visible in Render logs
            if sample_skus_logged < 3 and (committed > 0 or allocated > 0):
                log.info(f'    sample: sku={sku!r} committed={committed} allocated={allocated}')
                sample_skus_logged += 1
        # Apply to ledger. Use db_session.get() (SQLAlchemy 2.0 API) instead of the
        # deprecated Query.get() — the legacy method can silently return None when
        # the session has uncommitted writes to other tables.
        now = dt.datetime.utcnow()
        updated = 0
        missing_in_ledger = 0
        for style, vals in per_sku.items():
            row = db_session.get(Ledger, style)
            if row is None:
                missing_in_ledger += 1
                continue
            row.committed = vals['committed']
            row.allocated = vals['allocated']
            row.committed_synced_at = now
            updated += 1
        # SKUs with NO committed/allocated in this response should reset to 0
        skus_with_data = set(per_sku.keys())
        for r2 in Ledger.query.filter((Ledger.committed > 0) | (Ledger.allocated > 0)).all():
            if r2.style not in skus_with_data:
                r2.committed = 0
                r2.allocated = 0
                r2.committed_synced_at = now
                updated += 1
        db_session.commit()
        _stamp_sync('inventory_committed', 'ok', rows=updated)
        log.info(f'  ✓ inventory_committed: {updated} SKUs updated, '
                 f'{missing_in_ledger} SKUs in feed had no matching ledger row')
    except Exception as e:
        db_session.rollback()
        _stamp_sync('inventory_committed', 'error', error=str(e))
        log.error(f'  ✗ inventory_committed sync failed: {e}', exc_info=True)

    # 2. /allocations — per-customer breakdown
    try:
        r = requests.get(f'{EXISTING_PLATFORM_URL}/allocations', timeout=60)
        r.raise_for_status()
        data = r.json()
        rows = data.get('allocations') or []
        Allocation.query.delete()  # wipe and replace — single source of truth
        now = dt.datetime.utcnow()
        added = 0
        for r2 in rows:
            sku = str(r2.get('sku', '')).strip().upper()
            if not sku:
                continue
            try:
                qty = int(r2.get('qty') or 0)
            except (ValueError, TypeError):
                qty = 0
            db_session.add(Allocation(
                sku=sku,
                customer=str(r2.get('customer', ''))[:64],
                po=str(r2.get('po', ''))[:128],
                qty=qty,
                source=str(r2.get('source', 's3'))[:16],
                synced_at=now,
            ))
            added += 1
        db_session.commit()
        _stamp_sync('allocations', 'ok', rows=added)
        log.info(f'  ✓ allocations: {added} rows replaced')
    except Exception as e:
        db_session.rollback()
        _stamp_sync('allocations', 'error', error=str(e))
        log.error(f'  ✗ allocations sync failed: {e}')

    # 3. /production — production schedule (view-only)
    try:
        r = requests.get(f'{EXISTING_PLATFORM_URL}/production', timeout=60)
        r.raise_for_status()
        data = r.json()
        rows = data.get('production') or []
        Production.query.delete()
        now = dt.datetime.utcnow()
        added = 0
        for r2 in rows:
            style = str(r2.get('style', '')).strip().upper()
            if not style:
                continue
            try:
                units = int(r2.get('units') or 0)
            except (ValueError, TypeError):
                units = 0
            db_session.add(Production(
                production=str(r2.get('production', ''))[:64],
                po_name=str(r2.get('poName', ''))[:128],
                style=style,
                units=units,
                brand=str(r2.get('brand', ''))[:64],
                etd=str(r2.get('etd', ''))[:32] if r2.get('etd') else None,
                synced_at=now,
            ))
            added += 1
        db_session.commit()
        _stamp_sync('productions', 'ok', rows=added)
        log.info(f'  ✓ productions: {added} rows replaced')
    except Exception as e:
        db_session.rollback()
        _stamp_sync('productions', 'error', error=str(e))
        log.error(f'  ✗ productions sync failed: {e}')

    # 4. /api/orders from open-orders-api — Ross's forward-looking demand.
    # Different host than the existing platform — its own Render service.
    # If that service blocks us with "Host not in allowlist", it's a hostname
    # allowlist issue on their end (add this backend's domain to their allowlist).
    try:
        oo_url = OPEN_ORDERS_API_URL.rstrip('/') + '/api/orders'
        r = requests.get(oo_url, timeout=60)
        r.raise_for_status()
        data = r.json()
        rows = data.get('orders') or data if isinstance(data, list) else []
        OpenOrder.query.delete()
        now = dt.datetime.utcnow()
        added = 0
        skipped = 0
        for o in rows:
            # The open-orders API may return `style` or `baseStyle`. Both can be
            # full SKUs (with size suffix) so we strip to the base style.
            raw_style = str(o.get('style', '') or o.get('baseStyle', '') or '').upper().strip()
            if not raw_style:
                skipped += 1
                continue
            base_style = _extract_base_style(raw_style)
            if not base_style:
                skipped += 1
                continue
            try:
                open_qty = int(o.get('openQty') or 0)
            except (ValueError, TypeError):
                open_qty = 0
            try:
                pick_qty = int(o.get('pickQty') or 0)
            except (ValueError, TypeError):
                pick_qty = 0
            # Skip empty rows so the table doesn't bloat with 0-qty noise
            if open_qty == 0 and pick_qty == 0:
                skipped += 1
                continue
            # PO identifier: existing platform tries 5 different keys, mirror that
            po = (o.get('po') or o.get('poNumber') or o.get('customerPO')
                  or o.get('orderNo') or o.get('ctrlNo') or '')
            customer = (o.get('customer') or '').strip()
            # Date fields — multiple aliases as in existing platform
            start = o.get('startDate') or o.get('start_date') or o.get('shipDate')
            cancel = o.get('cancelDate') or o.get('cancel_date') or o.get('endDate')
            db_session.add(OpenOrder(
                style=base_style[:64],
                customer=customer[:64] if customer else None,
                po=str(po)[:128] if po else None,
                open_qty=open_qty,
                pick_qty=pick_qty,
                start_date=str(start)[:32] if start else None,
                cancel_date=str(cancel)[:32] if cancel else None,
                synced_at=now,
            ))
            added += 1
        db_session.commit()
        _stamp_sync('open_orders', 'ok', rows=added)
        log.info(f'  ✓ open_orders: {added} rows replaced ({skipped} skipped as empty)')
    except Exception as e:
        db_session.rollback()
        _stamp_sync('open_orders', 'error', error=str(e))
        log.error(f'  ✗ open_orders sync failed: {e}')


def init_db_post():
    pass  # placeholder — kept for backward compat in case anyone called this


# ──────────────────────────────────────────────────────────────────────────────
# Flask app
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# CORS — allow the frontend domain(s) to call us
if ALLOWED_ORIGINS == '*':
    CORS(app, supports_credentials=False)
    log.warning('CORS is wide open (ALLOWED_ORIGINS=*). Restrict for production.')
else:
    CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=False)
    log.info(f'CORS allowed origins: {ALLOWED_ORIGINS}')


@app.teardown_appcontext
def shutdown_session(exception=None):
    db_session.remove()


def require_auth(fn):
    """
    Auth shim. Currently a passthrough — wire in your real auth here.
    The function exists so the routes have a clear authorization decorator
    that you can fill in without touching the endpoint logic.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # Example: enforce a shared bearer token via env var
        required = os.environ.get('API_BEARER_TOKEN')
        if required:
            sent = request.headers.get('Authorization', '')
            if sent != f'Bearer {required}':
                return jsonify({'error': 'unauthorized'}), 401
        return fn(*args, **kwargs)
    return wrapper


# ──────────────────────────────────────────────────────────────────────────────
# Health / status
# ──────────────────────────────────────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    try:
        db_session.execute(func.now())
        db_ok = True
    except Exception as e:
        db_ok = False
        log.error(f'Health DB check failed: {e}')
    return jsonify({
        'status': 'ok' if db_ok else 'degraded',
        'db': db_ok,
        'existing_platform_url': EXISTING_PLATFORM_URL,
        'time': dt.datetime.utcnow().isoformat() + 'Z',
    })


# ──────────────────────────────────────────────────────────────────────────────
# Ledger endpoints
# ──────────────────────────────────────────────────────────────────────────────
@app.route('/ledger', methods=['GET'])
@require_auth
def get_ledger():
    """
    Return the full ledger plus seed info AND sync status.
    Frontend calls this on every page load to hydrate state.

    Side effect: if any sync source is >1hr stale, fires a non-blocking
    background sync. The current request returns whatever's in DB right
    now; the user sees the refreshed data on their next page load.
    """
    # Fire lazy sync BEFORE reading rows so on the first-ever request, the
    # initial sync at least kicks off (sync still happens in background thread).
    try:
        _maybe_lazy_sync()
    except Exception as e:
        log.error(f'Lazy sync trigger failed (continuing with stale data): {e}')

    rows = Ledger.query.all()
    seed = SeedInfo.query.first()
    sync_rows = {r.source: r.to_dict() for r in SyncLog.query.all()}
    return jsonify({
        'ledger': {r.style: r.to_dict() for r in rows},
        'seedInfo': seed.to_dict() if seed else {},
        'syncLog': sync_rows,
        'count': len(rows),
    })


@app.route('/ledger/seed', methods=['POST'])
@require_auth
def seed_ledger():
    """
    Seed the ledger from the existing platform's /inventory endpoint.
    Only allowed if the ledger is currently empty. To reset a seeded ledger,
    use /ledger/reset (which requires the password).

    Body (JSON): { rows: [{style, quantity, brand, color}, ...] }
    The frontend pre-rolls the data — keeps this endpoint dumb.
    """
    if Ledger.query.count() > 0:
        return jsonify({'error': 'already_seeded',
                        'message': 'Ledger already has data. Use /ledger/reset.'}), 409

    body = request.get_json(force=True, silent=True) or {}
    rows = body.get('rows', [])
    if not isinstance(rows, list) or not rows:
        return jsonify({'error': 'bad_request', 'message': 'rows[] required'}), 400

    return _do_seed(rows, source=body.get('source') or 'api',
                    file_name=body.get('fileName'),
                    api_item_count=body.get('apiItemCount', 0))


@app.route('/ledger/reset', methods=['POST'])
@require_auth
def reset_ledger():
    """
    Destructive reset. Wipes ledger + uploads, re-seeds from supplied rows.
    Password-gated via X-Reset-Password header OR JSON body { password }.

    Body (JSON): { password, rows: [...] }
    Returns 401 on bad password.
    """
    body = request.get_json(force=True, silent=True) or {}
    pw = request.headers.get('X-Reset-Password') or body.get('password', '')
    if pw != RESET_PASSWORD:
        log.warning('Reset attempted with wrong password from %s', request.remote_addr)
        return jsonify({'error': 'unauthorized',
                        'message': 'Invalid reset password'}), 401

    rows = body.get('rows', [])
    if not isinstance(rows, list) or not rows:
        return jsonify({'error': 'bad_request', 'message': 'rows[] required'}), 400

    # Wipe everything
    try:
        db_session.query(UploadItem).delete()
        db_session.query(Upload).delete()
        db_session.query(Ledger).delete()
        db_session.query(SeedInfo).delete()
        db_session.commit()
        log.info('Reset complete — all data deleted, reseeding')
    except Exception as e:
        db_session.rollback()
        log.error(f'Reset wipe failed: {e}')
        return jsonify({'error': 'wipe_failed', 'message': str(e)}), 500

    return _do_seed(rows, source='api-reset',
                    file_name=body.get('fileName'),
                    api_item_count=body.get('apiItemCount', 0))


def _do_seed(rows, source, file_name=None, api_item_count=0):
    """Shared seed logic — used by both /ledger/seed and /ledger/reset."""
    try:
        total_qty = 0
        for r in rows:
            style = str(r.get('style', '')).strip().upper()
            if not style:
                continue
            qty = int(r.get('quantity', 0) or 0)
            brand_obj = r.get('brand')
            brand_id = brand_obj.get('id') if isinstance(brand_obj, dict) else None
            entry = Ledger(
                style=style,
                quantity=qty,
                brand_id=brand_id,
                brand_data=brand_obj,
                color=r.get('color') or '',
                seeded=True,
            )
            db_session.add(entry)
            total_qty += qty

        info = SeedInfo(
            id=1,
            source=source,
            file_name=file_name,
            row_count=len(rows),
            total_qty=total_qty,
            api_item_count=api_item_count or len(rows),
        )
        db_session.add(info)
        db_session.commit()
        log.info(f'Seeded {len(rows)} styles, {total_qty} total units (source={source})')
        return jsonify({
            'ok': True,
            'rowCount': len(rows),
            'totalQty': total_qty,
            'seedInfo': info.to_dict(),
        })
    except Exception as e:
        db_session.rollback()
        log.error(f'Seed failed: {e}', exc_info=True)
        return jsonify({'error': 'seed_failed', 'message': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# Upload endpoints (packing lists + invoice reports)
# ──────────────────────────────────────────────────────────────────────────────
@app.route('/uploads', methods=['GET'])
@require_auth
def get_uploads():
    """
    Return all uploads (most-recent first). Optional ?type= to filter.
    Items are NOT included by default to keep the response small — fetch a
    single upload's items via GET /uploads/<id>.
    """
    q = Upload.query.order_by(Upload.uploaded_at.desc())
    type_filter = request.args.get('type')
    if type_filter:
        q = q.filter_by(type=type_filter)
    include_deleted = request.args.get('include_deleted') == '1'
    if not include_deleted:
        q = q.filter(Upload.deleted_at.is_(None))
    out = []
    for u in q.all():
        # Light listing — count, total, no items
        out.append({
            **u.to_dict(items=None),
            # Lightweight stats only
        })
    return jsonify({'uploads': out, 'count': len(out)})


@app.route('/uploads/<upload_id>', methods=['GET'])
@require_auth
def get_upload(upload_id):
    """Full upload detail including all items."""
    u = Upload.query.get(upload_id)
    if not u:
        return jsonify({'error': 'not_found'}), 404
    items = UploadItem.query.filter_by(upload_id=upload_id).all()
    return jsonify(u.to_dict(items=[it.to_dict() for it in items]))


@app.route('/uploads', methods=['POST'])
@require_auth
def create_upload():
    """
    Commit a parsed upload. Frontend does the file parsing client-side
    (because the parsers are already implemented there) and POSTs the
    final structured data. We just persist + apply to the ledger.

    Body (JSON):
      {
        id: "uuid-or-timestamp",
        type: "packing_list" | "invoice_report" | "open_orders",
        fileNames: [...],
        items: [ { style, quantity, brand, color }, ... ],
        parserMeta: {...}
      }

    `quantity` should be SIGNED — packing list = positive, invoice = negative.
    The frontend already handles this.
    """
    body = request.get_json(force=True, silent=True) or {}
    upload_id = body.get('id')
    items = body.get('items') or []
    upload_type = body.get('type')

    if not upload_id or not upload_type or not items:
        return jsonify({'error': 'bad_request',
                        'message': 'id, type, and items[] are required'}), 400

    if Upload.query.get(upload_id):
        return jsonify({'error': 'duplicate', 'message': 'Upload id exists'}), 409

    try:
        # Insert the upload header
        grand_total = sum(int(i.get('quantity', 0)) for i in items)
        u = Upload(
            id=upload_id,
            type=upload_type,
            file_names=body.get('fileNames') or [],
            item_count=len(items),
            grand_total=grand_total,
            parser_meta=body.get('parserMeta') or {},
        )
        db_session.add(u)

        # Insert items + apply to ledger atomically
        for it in items:
            style = str(it.get('style', '')).strip().upper()
            if not style:
                continue
            qty = int(it.get('quantity', 0) or 0)
            brand_obj = it.get('brand')
            brand_id = brand_obj.get('id') if isinstance(brand_obj, dict) else None

            db_session.add(UploadItem(
                upload_id=upload_id,
                style=style,
                quantity=qty,
                brand_id=brand_id,
                brand_data=brand_obj,
                color=it.get('color') or '',
            ))

            # Update ledger row (insert if missing)
            row = Ledger.query.get(style)
            if row is None:
                row = Ledger(
                    style=style, quantity=0, brand_id=brand_id,
                    brand_data=brand_obj, color=it.get('color') or '',
                    seeded=False,
                )
                db_session.add(row)
            row.quantity = (row.quantity or 0) + qty
            row.last_updated = dt.datetime.utcnow()
            # Keep brand metadata fresh
            if brand_obj and not row.brand_data:
                row.brand_data = brand_obj
                row.brand_id = brand_id

        db_session.commit()
        log.info(f'Upload {upload_id} ({upload_type}) committed: {len(items)} items, {grand_total} total')
        return jsonify({'ok': True, 'upload': u.to_dict()}), 201
    except Exception as e:
        db_session.rollback()
        log.error(f'Upload create failed: {e}', exc_info=True)
        return jsonify({'error': 'create_failed', 'message': str(e)}), 500


@app.route('/uploads/<upload_id>', methods=['PATCH'])
@require_auth
def edit_upload(upload_id):
    """
    Replace an upload's items. The ledger effect is computed as
    (new_items minus old_items) and applied to each affected style.
    """
    u = Upload.query.get(upload_id)
    if not u:
        return jsonify({'error': 'not_found'}), 404
    if u.deleted_at:
        return jsonify({'error': 'deleted', 'message': 'Cannot edit a deleted upload'}), 409

    body = request.get_json(force=True, silent=True) or {}
    new_items = body.get('items')
    if new_items is None:
        return jsonify({'error': 'bad_request', 'message': 'items[] required'}), 400

    try:
        # Compute net delta per style: (new - old)
        old_items = UploadItem.query.filter_by(upload_id=upload_id).all()
        delta = {}
        for it in old_items:
            delta[it.style] = delta.get(it.style, 0) - it.quantity
        for it in new_items:
            style = str(it.get('style', '')).strip().upper()
            if not style:
                continue
            qty = int(it.get('quantity', 0) or 0)
            delta[style] = delta.get(style, 0) + qty

        # Apply delta to ledger
        for style, change in delta.items():
            if change == 0:
                continue
            row = Ledger.query.get(style)
            if row is None:
                row = Ledger(style=style, quantity=0, seeded=False)
                db_session.add(row)
            row.quantity = (row.quantity or 0) + change
            row.last_updated = dt.datetime.utcnow()

        # Replace items
        UploadItem.query.filter_by(upload_id=upload_id).delete()
        for it in new_items:
            style = str(it.get('style', '')).strip().upper()
            if not style:
                continue
            qty = int(it.get('quantity', 0) or 0)
            brand_obj = it.get('brand')
            brand_id = brand_obj.get('id') if isinstance(brand_obj, dict) else None
            db_session.add(UploadItem(
                upload_id=upload_id, style=style, quantity=qty,
                brand_id=brand_id, brand_data=brand_obj,
                color=it.get('color') or '',
            ))

        u.item_count = len(new_items)
        u.grand_total = sum(int(i.get('quantity', 0)) for i in new_items)
        u.last_edited_at = dt.datetime.utcnow()
        db_session.commit()
        log.info(f'Upload {upload_id} edited: {len(new_items)} items, net delta applied')
        return jsonify({'ok': True, 'upload': u.to_dict()})
    except Exception as e:
        db_session.rollback()
        log.error(f'Upload edit failed: {e}', exc_info=True)
        return jsonify({'error': 'edit_failed', 'message': str(e)}), 500


@app.route('/uploads/<upload_id>', methods=['DELETE'])
@require_auth
def delete_upload(upload_id):
    """
    Soft-delete an upload and reverse its effect on the ledger.
    The upload row + items are kept for the audit trail (set deleted_at).
    """
    u = Upload.query.get(upload_id)
    if not u:
        return jsonify({'error': 'not_found'}), 404
    if u.deleted_at:
        return jsonify({'error': 'already_deleted'}), 409

    try:
        items = UploadItem.query.filter_by(upload_id=upload_id).all()
        for it in items:
            row = Ledger.query.get(it.style)
            if row is not None:
                row.quantity = (row.quantity or 0) - it.quantity
                row.last_updated = dt.datetime.utcnow()
        u.deleted_at = dt.datetime.utcnow()
        db_session.commit()
        log.info(f'Upload {upload_id} soft-deleted, ledger effect reversed')
        return jsonify({'ok': True})
    except Exception as e:
        db_session.rollback()
        log.error(f'Upload delete failed: {e}', exc_info=True)
        return jsonify({'error': 'delete_failed', 'message': str(e)}), 500


# ──────────────────────────────────────────────────────────────────────────────
# Proxy to existing platform — saves us re-implementing /inventory and /overrides
# ──────────────────────────────────────────────────────────────────────────────
@app.route('/proxy/inventory', methods=['GET'])
@require_auth
def proxy_inventory():
    """
    Pulls /inventory from the existing platform. This lets the frontend avoid
    CORS issues calling the existing platform directly — and lets us cache /
    rate-limit later if needed.
    """
    try:
        r = requests.get(f'{EXISTING_PLATFORM_URL}/inventory', timeout=60)
        r.raise_for_status()
        return jsonify(r.json())
    except requests.RequestException as e:
        log.error(f'Proxy /inventory failed: {e}')
        return jsonify({'error': 'upstream_failed', 'message': str(e)}), 502


# ──────────────────────────────────────────────────────────────────────────────
# SYNC ENDPOINTS — manual trigger + per-style breakdown lookups
# ──────────────────────────────────────────────────────────────────────────────
@app.route('/sync/from-existing', methods=['POST'])
@require_auth
def trigger_sync():
    """
    Force-refresh of committed/allocated/productions from the existing platform.
    Runs synchronously so the caller sees the new state immediately.
    The lazy hourly sync runs the same code via _maybe_lazy_sync() (which fires
    in a background thread — that's where the "non-blocking" property comes from).
    """
    try:
        run_full_sync()
        sync_rows = {r.source: r.to_dict() for r in SyncLog.query.all()}
        return jsonify({'ok': True, 'syncLog': sync_rows})
    except Exception as e:
        log.error(f'Manual sync failed: {e}', exc_info=True)
        return jsonify({'error': 'sync_failed', 'message': str(e)}), 500


@app.route('/allocations/<style>', methods=['GET'])
@require_auth
def get_allocations_for_style(style):
    """
    Return per-customer allocation breakdown for a given base style.
    Uses _extract_base_style() so callers can pass either base or full SKUs.
    """
    base = _extract_base_style(style)
    # Match SKUs whose base style equals `base`. Indexed via the sku column.
    rows = Allocation.query.filter(Allocation.sku.like(f'{base}%')).all()
    # Final filter in Python — handles edge cases where SKU starts with `base`
    # but isn't actually the same base style (rare but possible).
    filtered = [r.to_dict() for r in rows if _extract_base_style(r.sku) == base]
    # Pull the ledger row to also include current committed/allocated totals
    ledger_row = db_session.get(Ledger, base)
    return jsonify({
        'style': base,
        'allocations': filtered,
        'totalAllocated': ledger_row.allocated if ledger_row else 0,
        'totalCommitted': ledger_row.committed if ledger_row else 0,
    })


@app.route('/productions/<style>', methods=['GET'])
@require_auth
def get_productions_for_style(style):
    """
    Return all production rows for a given base style. View-only — frontend
    just renders this for context, never modifies the ledger from it.
    """
    base = _extract_base_style(style)
    rows = Production.query.filter(Production.style.like(f'{base}%')).all()
    filtered = [r.to_dict() for r in rows if _extract_base_style(r.style) == base]
    # Sort by ETD ascending (earliest first), pushing rows with no ETD to the end
    filtered.sort(key=lambda r: (r.get('etd') is None, r.get('etd') or ''))
    return jsonify({
        'style': base,
        'productions': filtered,
        'totalUnits': sum(r.get('units', 0) for r in filtered),
    })


@app.route('/open-orders/<style>', methods=['GET'])
@require_auth
def get_open_orders_for_style(style):
    """
    Return open-order rows for a given base style, plus pre-aggregated
    per-customer totals (the data the "A2000 Breakdown" view needs).
    """
    base = _extract_base_style(style)
    # All rows are stored at the base-style level (we strip size during sync)
    rows = OpenOrder.query.filter(OpenOrder.style == base).all()
    raw = [r.to_dict() for r in rows]

    # Aggregate by customer for the breakdown card (one row per customer)
    by_customer = {}
    for r in raw:
        cust = r.get('customer') or '—'
        cur = by_customer.setdefault(cust, {
            'customer': cust,
            'openQty': 0,
            'pickQty': 0,
            'totalQty': 0,
            'pos': set(),
            'startDate': None,    # earliest startDate across this customer's POs
            'cancelDate': None,   # earliest cancelDate
            'onPick': False,
        })
        cur['openQty'] += r.get('openQty', 0)
        cur['pickQty'] += r.get('pickQty', 0)
        cur['totalQty'] += r.get('totalQty', 0)
        if r.get('onPick'):
            cur['onPick'] = True
        if r.get('po'):
            cur['pos'].add(r.get('po'))
        # Track date range — keep earliest start and earliest cancel
        sd = r.get('startDate')
        if sd and (cur['startDate'] is None or sd < cur['startDate']):
            cur['startDate'] = sd
        cd = r.get('cancelDate')
        if cd and (cur['cancelDate'] is None or cd < cur['cancelDate']):
            cur['cancelDate'] = cd

    # Serialize sets and sort by total qty descending (biggest customer first)
    by_customer_list = []
    for v in by_customer.values():
        v['pos'] = sorted(v['pos'])
        by_customer_list.append(v)
    by_customer_list.sort(key=lambda r: -r['totalQty'])

    return jsonify({
        'style': base,
        'orders': raw,
        'byCustomer': by_customer_list,
        'totalQty': sum(r.get('totalQty', 0) for r in raw),
        'totalOpenQty': sum(r.get('openQty', 0) for r in raw),
        'totalPickQty': sum(r.get('pickQty', 0) for r in raw),
    })


@app.route('/proxy/overrides', methods=['GET'])
@require_auth
def proxy_overrides():
    """Same idea for /overrides."""
    try:
        r = requests.get(f'{EXISTING_PLATFORM_URL}/overrides', timeout=30)
        r.raise_for_status()
        return jsonify(r.json())
    except requests.RequestException as e:
        log.error(f'Proxy /overrides failed: {e}')
        return jsonify({'error': 'upstream_failed', 'message': str(e)}), 502


# ──────────────────────────────────────────────────────────────────────────────
# Error handlers
# ──────────────────────────────────────────────────────────────────────────────
@app.errorhandler(404)
def _404(e):
    return jsonify({'error': 'not_found'}), 404


@app.errorhandler(500)
def _500(e):
    log.error(f'500 error: {e}', exc_info=True)
    return jsonify({'error': 'internal_server_error'}), 500


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────
init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
