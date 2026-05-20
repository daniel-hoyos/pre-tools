"""
Control de Calidad - Fruto en Tolva
Flask backend for Elastic Beanstalk.

Storage is auto-detected:
  Local dev  → SQLite + local filesystem  (no AWS config needed)
  Production → DynamoDB + S3              (set S3_BUCKET env var)

Required environment variables for production (set in EB console):
  S3_BUCKET             e.g. my-qc-photos
  DYNAMO_REPORTS_TABLE  e.g. qc_reports   (default)
  DYNAMO_PHOTOS_TABLE   e.g. qc_photos    (default)
  AWS_DEFAULT_REGION    e.g. us-east-1
"""

import io
import os
import uuid
import base64
import tempfile
from datetime import datetime, timezone, timedelta
from functools import wraps

# Colombia Standard Time is always UTC-5 (no DST)
COT = timezone(timedelta(hours=-5))

from flask import (
    Flask, abort, jsonify, redirect, render_template,
    request, send_file, send_from_directory, session,
)
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from PIL import Image as PILImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ── App setup ─────────────────────────────────────────────────────────────────

application = Flask(__name__)
app = application
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Secret key for session signing — set SECRET_KEY in EB env vars for production
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

# Static app password — set APP_PASSWORD in EB env vars for production
APP_PASSWORD = os.environ.get("APP_PASSWORD", "calidad2024")


# ── Auth helpers ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return jsonify({"error": "No autorizado"}), 401
        return f(*args, **kwargs)
    return decorated

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
MAX_IMG_SIZE = (1200, 900)

CATEGORIES = [
    "Racimos verdes",
    "Racimos maduros",
    "Racimos sobre maduros",
    "Racimos podridos",
    "Pedúnculo largo",
]

# "General" is accepted for the pre-QC general photos step
VALID_PHOTO_CATEGORIES = set(CATEGORIES) | {"General"}

# ── Backend detection ─────────────────────────────────────────────────────────

S3_BUCKET            = os.environ.get("S3_BUCKET", "")
DYNAMO_REPORTS_TABLE = os.environ.get("DYNAMO_REPORTS_TABLE", "qc_reports")
DYNAMO_PHOTOS_TABLE  = os.environ.get("DYNAMO_PHOTOS_TABLE",  "qc_photos")
USE_AWS              = bool(S3_BUCKET)

if USE_AWS:
    import boto3
    from boto3.dynamodb.conditions import Key
    _dynamo    = boto3.resource("dynamodb")
    _s3        = boto3.client("s3")
    _rep_tbl   = _dynamo.Table(DYNAMO_REPORTS_TABLE)
    _photo_tbl = _dynamo.Table(DYNAMO_PHOTOS_TABLE)
    print(f"[storage] AWS mode — bucket={S3_BUCKET}  reports={DYNAMO_REPORTS_TABLE}  photos={DYNAMO_PHOTOS_TABLE}")
else:
    import sqlite3
    UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
    DATABASE   = os.path.join(BASE_DIR, "quality_control.db")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    print(f"[storage] Local mode — db={DATABASE}  uploads={UPLOAD_DIR}")

    def _get_db():
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db():
        conn = _get_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reports (
                id            TEXT PRIMARY KEY,
                transportista TEXT NOT NULL,
                proveedor     TEXT NOT NULL,
                lote          TEXT NOT NULL,
                notas         TEXT DEFAULT '',
                created_at    TEXT NOT NULL,
                photo_count   INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS photos (
                id         TEXT PRIMARY KEY,
                report_id  TEXT NOT NULL,
                filename   TEXT NOT NULL,
                category   TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (report_id) REFERENCES reports(id)
            );
        """)
        conn.commit()
        conn.close()

    _init_db()


# ── Image helpers ─────────────────────────────────────────────────────────────

def _process_image(image_b64: str) -> bytes:
    """Decode base64, resize to MAX_IMG_SIZE, return JPEG bytes."""
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]
    raw = base64.b64decode(image_b64)
    if not HAS_PIL:
        return raw
    img = PILImage.open(io.BytesIO(raw)).convert("RGB")
    img.thumbnail(MAX_IMG_SIZE, PILImage.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


# ── Storage layer — Reports ───────────────────────────────────────────────────

def db_list_reports():
    """Return list of report dicts ordered newest-first, each with photo_count."""
    if USE_AWS:
        resp  = _rep_tbl.scan()
        items = resp.get("Items", [])
        # DynamoDB numbers come back as Decimal — convert
        for item in items:
            item["photo_count"] = int(item.get("photo_count", 0))
        items.sort(key=lambda x: x["created_at"], reverse=True)
        return items
    else:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM reports ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


def db_get_report(report_id: str):
    """Return report dict or None."""
    if USE_AWS:
        resp = _rep_tbl.get_item(Key={"id": report_id})
        item = resp.get("Item")
        if item:
            item["photo_count"] = int(item.get("photo_count", 0))
        return item
    else:
        conn = _get_db()
        row  = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        conn.close()
        return dict(row) if row else None


def db_create_report(report_id, transportista, proveedor, lote, notas, created_at):
    if USE_AWS:
        _rep_tbl.put_item(Item={
            "id": report_id,
            "transportista": transportista,
            "proveedor":     proveedor,
            "lote":          lote,
            "notas":         notas,
            "created_at":    created_at,
            "photo_count":   0,
        })
    else:
        conn = _get_db()
        conn.execute(
            "INSERT INTO reports (id,transportista,proveedor,lote,notas,created_at,photo_count) "
            "VALUES (?,?,?,?,?,?,0)",
            (report_id, transportista, proveedor, lote, notas, created_at),
        )
        conn.commit()
        conn.close()


def db_delete_report(report_id):
    if USE_AWS:
        _rep_tbl.delete_item(Key={"id": report_id})
    else:
        conn = _get_db()
        conn.execute("DELETE FROM reports WHERE id=?", (report_id,))
        conn.commit()
        conn.close()


# ── Storage layer — Photos ────────────────────────────────────────────────────

def db_list_photos(report_id: str):
    """Return list of photo dicts for a report, ordered by category then time."""
    # Sort key: General photos first (step 0), then QC categories alphabetically
    def _sort_key(p):
        return (0 if p["category"] == "General" else 1, p["category"], p["created_at"])

    if USE_AWS:
        resp  = _photo_tbl.query(KeyConditionExpression=Key("report_id").eq(report_id))
        items = resp.get("Items", [])
        items.sort(key=_sort_key)
        return items
    else:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM photos WHERE report_id=? ORDER BY created_at",
            (report_id,),
        ).fetchall()
        conn.close()
        result = [dict(r) for r in rows]
        result.sort(key=_sort_key)
        return result


def db_get_photo(report_id: str, photo_id: str):
    if USE_AWS:
        resp = _photo_tbl.get_item(Key={"report_id": report_id, "id": photo_id})
        return resp.get("Item")
    else:
        conn  = _get_db()
        row   = conn.execute(
            "SELECT * FROM photos WHERE id=? AND report_id=?", (photo_id, report_id)
        ).fetchone()
        conn.close()
        return dict(row) if row else None


def db_create_photo(photo_id, report_id, filename, category, created_at):
    if USE_AWS:
        _photo_tbl.put_item(Item={
            "report_id":  report_id,
            "id":         photo_id,
            "filename":   filename,
            "category":   category,
            "created_at": created_at,
        })
        # Increment counter on parent report
        _rep_tbl.update_item(
            Key={"id": report_id},
            UpdateExpression="ADD photo_count :one",
            ExpressionAttributeValues={":one": 1},
        )
    else:
        conn = _get_db()
        conn.execute(
            "INSERT INTO photos (id,report_id,filename,category,created_at) VALUES (?,?,?,?,?)",
            (photo_id, report_id, filename, category, created_at),
        )
        conn.execute(
            "UPDATE reports SET photo_count = photo_count + 1 WHERE id=?", (report_id,)
        )
        conn.commit()
        conn.close()


def db_update_photo_category(report_id: str, photo_id: str, new_category: str):
    if USE_AWS:
        _photo_tbl.update_item(
            Key={"report_id": report_id, "id": photo_id},
            UpdateExpression="SET category = :cat",
            ExpressionAttributeValues={":cat": new_category},
        )
    else:
        conn = _get_db()
        conn.execute(
            "UPDATE photos SET category=? WHERE id=? AND report_id=?",
            (new_category, photo_id, report_id),
        )
        conn.commit()
        conn.close()


def db_delete_photo(report_id, photo_id):
    if USE_AWS:
        _photo_tbl.delete_item(Key={"report_id": report_id, "id": photo_id})
        _rep_tbl.update_item(
            Key={"id": report_id},
            UpdateExpression="ADD photo_count :neg",
            ExpressionAttributeValues={":neg": -1},
        )
    else:
        conn = _get_db()
        conn.execute("DELETE FROM photos WHERE id=?", (photo_id,))
        conn.execute(
            "UPDATE reports SET photo_count = MAX(0, photo_count - 1) WHERE id=?",
            (report_id,),
        )
        conn.commit()
        conn.close()


def db_delete_all_photos(report_id):
    """Delete all photos belonging to a report (used when deleting report)."""
    photos = db_list_photos(report_id)
    for p in photos:
        storage_delete(p["filename"])
        if USE_AWS:
            _photo_tbl.delete_item(Key={"report_id": report_id, "id": p["id"]})
        else:
            conn = _get_db()
            conn.execute("DELETE FROM photos WHERE id=?", (p["id"],))
            conn.commit()
            conn.close()


# ── Storage layer — File (S3 / local) ────────────────────────────────────────

def storage_save(image_bytes: bytes, filename: str):
    """Persist image bytes to S3 or local disk."""
    if USE_AWS:
        _s3.put_object(
            Bucket=S3_BUCKET,
            Key=filename,
            Body=image_bytes,
            ContentType="image/jpeg",
        )
    else:
        with open(os.path.join(UPLOAD_DIR, filename), "wb") as fh:
            fh.write(image_bytes)


def storage_delete(filename: str):
    """Delete image from S3 or local disk."""
    if USE_AWS:
        _s3.delete_object(Bucket=S3_BUCKET, Key=filename)
    else:
        fp = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(fp):
            os.remove(fp)


def storage_url(filename: str) -> str:
    """Return a URL to serve the image (presigned for S3, local path otherwise)."""
    if USE_AWS:
        return _s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": filename},
            ExpiresIn=3600,
        )
    return f"/uploads/{filename}"


def storage_download_to_temp(filename: str) -> str:
    """Download image to a temp file; return its path. Caller must delete it."""
    if USE_AWS:
        suffix = os.path.splitext(filename)[1] or ".jpg"
        tmp    = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        _s3.download_fileobj(S3_BUCKET, filename, tmp)
        tmp.flush()
        tmp.close()
        return tmp.name
    return os.path.join(UPLOAD_DIR, filename)


# ── Routes – Auth ─────────────────────────────────────────────────────────────

@application.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(force=True)
    if data.get("password") == APP_PASSWORD:
        session["logged_in"] = True
        return jsonify({"success": True})
    return jsonify({"error": "Contraseña incorrecta"}), 401


@application.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"success": True})


@application.route("/api/auth/status", methods=["GET"])
def auth_status():
    return jsonify({"logged_in": bool(session.get("logged_in"))})


# ── Routes – frontend ─────────────────────────────────────────────────────────

@application.route("/")
def index():
    return render_template("index.html")


@application.route("/uploads/<path:filename>")
def serve_upload(filename):
    if USE_AWS:
        return redirect(storage_url(filename))
    return send_from_directory(UPLOAD_DIR, filename)


# ── Routes – API: Reports ─────────────────────────────────────────────────────

@application.route("/api/reports", methods=["GET"])
@login_required
def list_reports():
    return jsonify(db_list_reports())


@application.route("/api/reports", methods=["POST"])
@login_required
def create_report():
    data = request.get_json(force=True)
    if not all(data.get(k) for k in ("transportista", "proveedor", "lote")):
        return jsonify({"error": "Faltan campos requeridos"}), 400

    report_id  = str(uuid.uuid4())
    created_at = datetime.now(COT).isoformat()
    db_create_report(
        report_id,
        data["transportista"], data["proveedor"],
        data["lote"], data.get("notas", ""),
        created_at,
    )
    return jsonify({"id": report_id, "created_at": created_at}), 201


@application.route("/api/reports/<report_id>", methods=["GET"])
@login_required
def get_report(report_id):
    report = db_get_report(report_id)
    if not report:
        abort(404)
    report["photos"] = db_list_photos(report_id)
    return jsonify(report)


@application.route("/api/reports/<report_id>", methods=["DELETE"])
@login_required
def delete_report(report_id):
    if not db_get_report(report_id):
        abort(404)
    db_delete_all_photos(report_id)
    db_delete_report(report_id)
    return jsonify({"success": True})


# ── Routes – API: Photos ──────────────────────────────────────────────────────

@application.route("/api/reports/<report_id>/photos", methods=["POST"])
@login_required
def upload_photo(report_id):
    if not db_get_report(report_id):
        abort(404)

    data      = request.get_json(force=True)
    category  = data.get("category", "")
    image_b64 = data.get("image", "")

    if category not in VALID_PHOTO_CATEGORIES:
        return jsonify({"error": "Categoría inválida"}), 400
    if not image_b64:
        return jsonify({"error": "Imagen requerida"}), 400

    img_bytes  = _process_image(image_b64)
    photo_id   = str(uuid.uuid4())
    filename   = f"{photo_id}.jpg"
    created_at = datetime.now(COT).isoformat()

    storage_save(img_bytes, filename)
    db_create_photo(photo_id, report_id, filename, category, created_at)

    return jsonify({
        "id":       photo_id,
        "filename": filename,
        "url":      storage_url(filename),
    }), 201


@application.route("/api/reports/<report_id>/photos/<photo_id>", methods=["PATCH"])
@login_required
def update_photo(report_id, photo_id):
    photo = db_get_photo(report_id, photo_id)
    if not photo:
        abort(404)
    data         = request.get_json(force=True)
    new_category = data.get("category", "")
    if new_category not in VALID_PHOTO_CATEGORIES:
        return jsonify({"error": "Categoría inválida"}), 400
    db_update_photo_category(report_id, photo_id, new_category)
    return jsonify({"success": True})


@application.route("/api/reports/<report_id>/photos/<photo_id>", methods=["DELETE"])
@login_required
def delete_photo(report_id, photo_id):
    photo = db_get_photo(report_id, photo_id)
    if not photo:
        abort(404)
    storage_delete(photo["filename"])
    db_delete_photo(report_id, photo_id)
    return jsonify({"success": True})


# ── Routes – API: PDF ─────────────────────────────────────────────────────────

@application.route("/api/reports/<report_id>/pdf")
@login_required
def download_pdf(report_id):
    report = db_get_report(report_id)
    if not report:
        abort(404)
    photos = db_list_photos(report_id)

    buf  = build_pdf(report, photos)
    lote = report["lote"].replace(" ", "_")
    date = report["created_at"][:10]
    name = f"Control_Calidad_{lote}_{date}.pdf"

    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=name)


# ── Routes – API: Stats ───────────────────────────────────────────────────────

def _cot_date(dt_str: str) -> str:
    """Return the Colombia (UTC-5) calendar date for a stored datetime string."""
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(COT).strftime("%Y-%m-%d")
    except Exception:
        return dt_str[:10]


@application.route("/api/stats", methods=["GET"])
@login_required
def get_stats():
    proveedor = request.args.get("proveedor", "")   # "" = all suppliers
    from_date = request.args.get("from", "")        # YYYY-MM-DD
    to_date   = request.args.get("to", "")          # YYYY-MM-DD

    reports = db_list_reports()

    if proveedor:
        reports = [r for r in reports if r["proveedor"] == proveedor]
    if from_date:
        reports = [r for r in reports if _cot_date(r["created_at"]) >= from_date]
    if to_date:
        reports = [r for r in reports if _cot_date(r["created_at"]) <= to_date]

    counts = {c: 0 for c in CATEGORIES}
    for report in reports:
        for photo in db_list_photos(report["id"]):
            if photo["category"] in CATEGORIES:
                counts[photo["category"]] += 1

    total_qc = sum(counts.values())
    denom    = total_qc or 1
    pcts     = {c: round(counts[c] / denom * 100, 1) for c in CATEGORIES}

    return jsonify({
        "report_count":    len(reports),
        "total_qc_photos": total_qc,
        "counts":          counts,
        "percentages":     pcts,
    })


# ── PDF builder ───────────────────────────────────────────────────────────────

def build_pdf(report: dict, photos: list) -> io.BytesIO:
    from reportlab.lib           import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units     import cm
    from reportlab.lib.styles    import ParagraphStyle
    from reportlab.lib.enums     import TA_CENTER, TA_LEFT
    from reportlab.pdfgen        import canvas as rl_canvas
    from reportlab.platypus      import (
        SimpleDocTemplate, Table, TableStyle,
        Image as RLImage, Paragraph, Spacer,
    )

    buf = io.BytesIO()
    PAGE_W, _ = A4
    MARGIN    = 1.4 * cm
    CONTENT_W = PAGE_W - 2 * MARGIN

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
    )

    C_HEADER = colors.HexColor("#2E7D32")
    C_ROW    = colors.HexColor("#E8F5E9")
    C_LABEL  = colors.HexColor("#F9FBE7")
    C_BLACK  = colors.black
    C_WHITE  = colors.white

    def ps(name, **kw):
        d = dict(fontName="Helvetica", fontSize=8, leading=10)
        d.update(kw)
        return ParagraphStyle(name, **d)

    st_h   = ps("h",   fontName="Helvetica-Bold", fontSize=11,
                        textColor=C_WHITE, alignment=TA_CENTER)
    st_col = ps("col", fontName="Helvetica-Bold", alignment=TA_CENTER)
    st_val = ps("val", alignment=TA_LEFT)
    st_num = ps("num", alignment=TA_CENTER)          # centered numbers
    st_cat = ps("cat", fontSize=7, alignment=TA_CENTER)

    def P(text, style):
        # Note: use `is None` so that 0 renders as "0" not ""
        return Paragraph("" if text is None else str(text), style)

    elements  = []
    tmp_files = []   # temp files to clean up after build

    # ── 1. Info header ────────────────────────────────────────────────────────
    # Treat stored naive timestamp as UTC, display in Colombian Standard Time (UTC-5)
    created_dt = datetime.fromisoformat(report["created_at"]).replace(tzinfo=timezone.utc).astimezone(COT)
    fecha_hora = created_dt.strftime("%d/%m/%Y  %H:%M")

    cw   = CONTENT_W / 5
    info = [
        [P("CONTROL DE CALIDAD DE FRUTO EN TOLVA", st_h), "", "", "", ""],
        [P("Fecha y Hora", st_col), P("Transportista", st_col),
         P("Proveedor",    st_col), P("Lote",          st_col),
         P("Notas Adicionales", st_col)],
        [P(fecha_hora,               st_val), P(report["transportista"], st_val),
         P(report["proveedor"],      st_val), P(report["lote"],          st_val),
         P(report.get("notas", ""), st_val)],
    ]
    t_info = Table(info, colWidths=[cw] * 5)
    t_info.setStyle(TableStyle([
        ("SPAN",          (0, 0), (4, 0)),
        ("BACKGROUND",    (0, 0), (4, 0), C_HEADER),
        ("BACKGROUND",    (0, 1), (4, 1), C_ROW),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",          (0, 0), (-1, -1), 0.5, C_BLACK),
        # Title row — slim, matching category label bars
        ("TOPPADDING",    (0, 0), (4, 0), 4),
        ("BOTTOMPADDING", (0, 0), (4, 0), 4),
        # Other rows
        ("TOPPADDING",    (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
    ]))
    elements.append(t_info)
    elements.append(Spacer(1, 0.3 * cm))

    # ── 2. Summary table ──────────────────────────────────────────────────────
    # Only QC categories count in the summary (General photos are excluded)
    qc_photos      = [p for p in photos if p["category"] in CATEGORIES]
    general_photos = [p for p in photos if p["category"] == "General"]
    counts = {c: sum(1 for p in qc_photos if p["category"] == c) for c in CATEGORIES}
    total  = sum(counts.values()) or 1

    cat_hdr = [
        "Racimos\nverdes", "Racimos\nmaduros",
        "Racimos\nsobre\nmaduros", "Racimos\npodridos", "Pedúnculo\nlargo",
    ]
    sw      = CONTENT_W / 7
    summary = [
        [P("Parámetros",  st_col)] + [P(h, st_col) for h in cat_hdr] + [P("Total", st_col)],
        [P("Unidades",    st_col)] + [P(counts[c], st_num) for c in CATEGORIES] + [P(sum(counts.values()), st_col)],
        [P("% del Total", st_col)] + [P(f"{counts[c]/total*100:.0f}%", st_num) for c in CATEGORIES] + [P("100%", st_col)],
    ]
    t_sum = Table(summary, colWidths=[sw] * 7)
    t_sum.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), C_ROW),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("GRID",          (0, 0), (-1, -1), 0.5, C_BLACK),
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(t_sum)
    elements.append(Spacer(1, 0.3 * cm))

    # ── 3. Photo sections ─────────────────────────────────────────────────────
    PHOTOS_PER_ROW = 5
    photo_cw = CONTENT_W / PHOTOS_PER_ROW
    photo_h  = photo_cw * 0.72   # ~4:3

    CAT_COLORS = {
        "General":               colors.HexColor("#455A64"),
        "Racimos verdes":        colors.HexColor("#2E7D32"),
        "Racimos maduros":       colors.HexColor("#F57F17"),
        "Racimos sobre maduros": colors.HexColor("#BF360C"),
        "Racimos podridos":      colors.HexColor("#4E342E"),
        "Pedúnculo largo":       colors.HexColor("#1565C0"),
    }
    CAT_LABELS = {
        "General":               "Fotos Generales",
        "Racimos verdes":        "Racimos Verdes",
        "Racimos maduros":       "Racimos Maduros",
        "Racimos sobre maduros": "Racimos Sobremaduros",
        "Racimos podridos":      "Racimos Podridos",
        "Pedúnculo largo":       "Pedúnculo Largo",
    }

    st_cat_hdr = ps("chdr", fontName="Helvetica-Bold", fontSize=10,
                     textColor=C_WHITE, leading=14)

    def _render_photo_section(section_cat, section_photos):
        """Append a labelled header + photo rows for one category."""
        hdr = Table(
            [[P(CAT_LABELS[section_cat], st_cat_hdr)]],
            colWidths=[CONTENT_W],
        )
        hdr.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), CAT_COLORS[section_cat]),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ]))
        elements.append(hdr)

        for i in range(0, len(section_photos), PHOTOS_PER_ROW):
            row_batch = section_photos[i : i + PHOTOS_PER_ROW]
            cells = []
            for p in row_batch:
                fp = storage_download_to_temp(p["filename"])
                if USE_AWS:
                    tmp_files.append(fp)
                if os.path.exists(fp):
                    cells.append(RLImage(fp, width=photo_cw - 4, height=photo_h - 4))
                else:
                    cells.append(P("", st_cat))
            while len(cells) < PHOTOS_PER_ROW:
                cells.append(P("", st_cat))

            pt = Table([cells],
                       colWidths=[photo_cw] * PHOTOS_PER_ROW,
                       rowHeights=[photo_h])
            pt.setStyle(TableStyle([
                ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
                ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                ("GRID",          (0, 0), (-1, -1), 0.5, C_BLACK),
                ("TOPPADDING",    (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            elements.append(pt)

        elements.append(Spacer(1, 0.25 * cm))

    # General photos appear first
    if general_photos:
        _render_photo_section("General", general_photos)

    # QC categories
    by_cat = {c: [p for p in qc_photos if p["category"] == c] for c in CATEGORIES}
    for cat in CATEGORIES:
        if by_cat[cat]:
            _render_photo_section(cat, by_cat[cat])

    # ── Page-number canvas ("1/2" style) ─────────────────────────────────────
    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_page_states)
            for state in self._saved_page_states:
                self.__dict__.update(state)
                self._draw_page_number(total)
                rl_canvas.Canvas.showPage(self)
            rl_canvas.Canvas.save(self)

        def _draw_page_number(self, total):
            self.setFont("Helvetica", 8)
            self.setFillColor(colors.HexColor("#757575"))
            self.drawCentredString(
                PAGE_W / 2,
                0.7 * cm,
                f"{self._pageNumber}/{total}",
            )

    doc.build(elements, canvasmaker=NumberedCanvas)

    # Clean up any temp files downloaded from S3
    for fp in tmp_files:
        try:
            os.remove(fp)
        except OSError:
            pass

    buf.seek(0)
    return buf


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    application.run(debug=True, host="0.0.0.0", port=8080)
