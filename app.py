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
    Mutated atomically when uploads are committed/edited/deleted.
    """
    __tablename__ = 'ledger'
    style = Column(String(64), primary_key=True)             # base style (no size suffix)
    quantity = Column(Integer, nullable=False, default=0)
    brand_id = Column(String(32), nullable=True, index=True)  # e.g. 'NAUTICA'
    brand_data = Column(JSON, nullable=True)                  # full brand obj snapshot
    color = Column(String(128), nullable=True)
    last_updated = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    seeded = Column(Boolean, default=False)                   # came from seed action

    def to_dict(self):
        return {
            'style': self.style,
            'quantity': self.quantity,
            'brand': self.brand_data,
            'color': self.color or '',
            'lastUpdated': self.last_updated.isoformat() if self.last_updated else None,
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


def init_db():
    """Create tables if they don't exist. Safe to call repeatedly."""
    Base.metadata.create_all(bind=engine)
    log.info(f'Database initialized: {DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL}')


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
    Return the full ledger plus seed info.
    Frontend calls this on every page load to hydrate state.
    """
    rows = Ledger.query.all()
    seed = SeedInfo.query.first()
    return jsonify({
        'ledger': {r.style: r.to_dict() for r in rows},
        'seedInfo': seed.to_dict() if seed else {},
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
