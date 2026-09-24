import os
import csv
import calendar
import hmac
import io
import secrets
import shutil
import sqlite3
import smtplib
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    Response,
    jsonify,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PIONEER_DATA_DIR", BASE_DIR)).expanduser().resolve()
DATABASE = DATA_DIR / "pioneer.db"
UPLOAD_DIR = DATA_DIR / "uploads"
BACKUP_DIR = DATA_DIR / "backups"

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
DOCUMENT_EXTENSIONS = IMAGE_EXTENSIONS | {"pdf", "doc", "docx", "xls", "xlsx", "txt", "zip"}
RECORDING_EXTENSIONS = {"mp3", "m4a", "wav", "mp4", "webm", "mov"}
TASK_FILE_EXTENSIONS = DOCUMENT_EXTENSIONS | RECORDING_EXTENSIONS

# Ключ хранится в базе, а подпись и цвет используются только в интерфейсе.
PRESENCE_STATUSES = (
    ("online", "🟢 В сети"),
    ("away", "🟡 Отошёл"),
    ("offline", "🔴 Не в сети"),
    ("vacation", "🌴 В отпуске"),
    ("sick", "🤒 На больничном"),
    ("remote", "🏠 На удалёнке"),
    ("trip", "🚗 В командировке"),
    ("training", "📚 На обучении"),
    ("meeting", "⭐ Важная встреча"),
)

# Только эти имена таблиц подставляются в SQL; параметры из формы остаются значениями.
CATALOGS = {
    "departments": ("Отделы", "departments", "id", "name"),
    "positions": ("Должности", "positions", "id", "name"),
    "statuses": ("Статусы присутствия", "presence_options", "key", "label"),
    "post-types": ("Типы публикаций", "post_types", "key", "label"),
    "task-statuses": ("Статусы задач", "task_status_options", "key", "label"),
    "event-types": ("Типы событий", "event_types", "key", "label"),
    "rooms": ("Помещения (залы)", "rooms", "id", "name"),
}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("PIONEER_SECRET_KEY", "pioneer-local-secret-change-me"),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    UPLOAD_FOLDER=str(UPLOAD_DIR),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PIONEER_HTTPS") == "1",
)
ACCESS_PASSWORD = os.environ.get("PIONEER_ACCESS_PASSWORD", "")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS presence_options (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#78827d'
);

CREATE TABLE IF NOT EXISTS post_types (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    department_id INTEGER NOT NULL,
    position TEXT NOT NULL,
    birth_date TEXT NOT NULL,
    photo TEXT,
    presence_status TEXT NOT NULL DEFAULT 'offline',
    is_dismissed INTEGER NOT NULL DEFAULT 0,
    email TEXT,
    access_code_hash TEXT,
    access_code_display TEXT,
    dismissed_at TEXT,
    dismissal_reason TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (department_id) REFERENCES departments(id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    department_id INTEGER NOT NULL,
    created_date TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'В работе' CHECK(status IN ('В работе', 'Выполнено')),
    completed_at TEXT,
    department_number INTEGER,
    FOREIGN KEY (department_id) REFERENCES departments(id)
);

CREATE TABLE IF NOT EXISTS task_assignees (
    task_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    PRIMARY KEY (task_id, employee_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_observers (
    task_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    PRIMARY KEY (task_id, employee_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    author_id INTEGER NOT NULL,
    post_type TEXT NOT NULL DEFAULT 'Обычный',
    audience_department_id INTEGER,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (author_id) REFERENCES employees(id),
    FOREIGN KEY (audience_department_id) REFERENCES departments(id)
);

CREATE TABLE IF NOT EXISTS post_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
    FOREIGN KEY (author_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS comment_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL,
    stored_name TEXT,
    original_name TEXT,
    link_url TEXT,
    FOREIGN KEY (comment_id) REFERENCES comments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS likes (
    post_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    PRIMARY KEY (post_id, employee_id),
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
    FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS post_reactions (
    post_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    reaction TEXT NOT NULL,
    PRIMARY KEY (post_id, employee_id),
    FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
    FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    meeting_at TEXT NOT NULL,
    link TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    organizer_id INTEGER,
    status TEXT NOT NULL DEFAULT 'Запланирована',
    recording_stored_name TEXT,
    recording_original_name TEXT,
    FOREIGN KEY (organizer_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS meeting_participants (
    meeting_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    PRIMARY KEY (meeting_id, employee_id),
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    previous_names TEXT NOT NULL DEFAULT '',
    new_names TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    task_id INTEGER,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'task',
    entity_id INTEGER,
    action_url TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS email_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    FOREIGN KEY (employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS workday_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    work_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    reminder_sent INTEGER NOT NULL DEFAULT 0,
    UNIQUE(employee_id, work_date),
    FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('general', 'private', 'group')),
    call_link TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chat_members (
    room_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    PRIMARY KEY(room_id, employee_id),
    FOREIGN KEY (room_id) REFERENCES chat_rooms(id),
    FOREIGN KEY (employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    stored_name TEXT,
    original_name TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (room_id) REFERENCES chat_rooms(id),
    FOREIGN KEY (author_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS task_status_options (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#78827d',
    icon TEXT NOT NULL DEFAULT '•'
);

CREATE TABLE IF NOT EXISTS event_types (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#78827d',
    icon TEXT NOT NULL DEFAULT '📅'
);

CREATE TABLE IF NOT EXISTS task_departments (
    task_id INTEGER NOT NULL,
    department_id INTEGER NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, department_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (department_id) REFERENCES departments(id)
);

CREATE TABLE IF NOT EXISTS task_approvers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    original_employee_id INTEGER NOT NULL,
    current_employee_id INTEGER NOT NULL,
    decision TEXT NOT NULL DEFAULT 'Ожидает' CHECK(decision IN ('Ожидает', 'Согласовано', 'На доработку')),
    comment TEXT NOT NULL DEFAULT '',
    decided_at TEXT,
    UNIQUE(task_id, original_employee_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (original_employee_id) REFERENCES employees(id),
    FOREIGN KEY (current_employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS task_substitutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    role_type TEXT NOT NULL,
    original_employee_id INTEGER NOT NULL,
    replacement_employee_id INTEGER NOT NULL,
    absence_status TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    source TEXT NOT NULL DEFAULT 'automatic',
    reason TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (original_employee_id) REFERENCES employees(id),
    FOREIGN KEY (replacement_employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (author_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS task_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (author_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS employee_absences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    status_key TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    returned_at TEXT,
    FOREIGN KEY (employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS employee_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    position_name TEXT NOT NULL,
    rate REAL NOT NULL DEFAULT 1.0,
    project TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_positions (
    task_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    employee_position_id INTEGER,
    PRIMARY KEY (task_id, employee_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    FOREIGN KEY (employee_position_id) REFERENCES employee_positions(id)
);

CREATE TABLE IF NOT EXISTS access_rights (
    employee_id INTEGER NOT NULL,
    feature TEXT NOT NULL,
    can_access INTEGER NOT NULL DEFAULT 1,
    backup_employee_id INTEGER,
    PRIMARY KEY (employee_id, feature),
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    FOREIGN KEY (backup_employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    responsible_employee_id INTEGER,
    color TEXT NOT NULL DEFAULT '#78827d',
    icon TEXT NOT NULL DEFAULT '▣',
    FOREIGN KEY (responsible_employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS room_bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL,
    booking_date TEXT NOT NULL,
    start_hour INTEGER NOT NULL,
    end_hour INTEGER NOT NULL,
    title TEXT NOT NULL,
    department_id INTEGER NOT NULL,
    responsible_employee_id INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (room_id) REFERENCES rooms(id),
    FOREIGN KEY (department_id) REFERENCES departments(id),
    FOREIGN KEY (responsible_employee_id) REFERENCES employees(id),
    FOREIGN KEY (created_by) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS room_booking_departments (
    booking_id INTEGER NOT NULL,
    department_id INTEGER NOT NULL,
    PRIMARY KEY (booking_id, department_id),
    FOREIGN KEY (booking_id) REFERENCES room_bookings(id) ON DELETE CASCADE,
    FOREIGN KEY (department_id) REFERENCES departments(id)
);

CREATE TABLE IF NOT EXISTS room_booking_responsibles (
    booking_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (booking_id, employee_id),
    FOREIGN KEY (booking_id) REFERENCES room_bookings(id) ON DELETE CASCADE,
    FOREIGN KEY (employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS department_task_sequences (
    department_id INTEGER PRIMARY KEY,
    last_number INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (department_id) REFERENCES departments(id)
);

CREATE TABLE IF NOT EXISTS weekly_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(employee_id, week_start),
    FOREIGN KEY (employee_id) REFERENCES employees(id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_reads (
    room_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(room_id, employee_id),
    FOREIGN KEY (room_id) REFERENCES chat_rooms(id),
    FOREIGN KEY (employee_id) REFERENCES employees(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_deadline ON tasks(status, deadline);
CREATE INDEX IF NOT EXISTS idx_notifications_employee_read ON notifications(employee_id, is_read, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_room ON chat_messages(room_id, id);
CREATE INDEX IF NOT EXISTS idx_task_substitutions_active ON task_substitutions(task_id, is_active);
"""


def get_db():
    """Открывает одно соединение SQLite на время обработки запроса."""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_all(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def catalog_rows(catalog):
    _title, table, _key, label = CATALOGS[catalog]
    return query_all(f"SELECT * FROM {table} ORDER BY {label} COLLATE NOCASE")


def valid_catalog_value(catalog, value, column=None):
    _title, table, key, _label = CATALOGS[catalog]
    return bool(query_one(f"SELECT 1 FROM {table} WHERE {column or key} = ?", (value,)))


def active_employees():
    return query_all("SELECT * FROM employees WHERE is_dismissed = 0 ORDER BY full_name")


def task_people(task_id, role):
    table = "task_assignees" if role == "assignees" else "task_observers"
    return query_all(
        f"SELECT e.* FROM {table} link JOIN employees e ON e.id = link.employee_id WHERE link.task_id = ? ORDER BY e.full_name",
        (task_id,),
    )


def people_names(people):
    return ", ".join(person["full_name"] + (" (уволен)" if person["is_dismissed"] else "") for person in people)


ABSENCE_KEYS = {"vacation", "sick", "trip", "training"}
LEADERSHIP_ROLES = {"director", "deputy", "admin"}
TASK_HISTORY_LABELS = {
    "initial": "Задача создана", "reassign": "Изменены исполнители", "observers": "Изменены наблюдатели",
    "approvers": "Изменены согласующие", "task-edit": "Изменены параметры задачи", "substitution": "Автозамена",
    "substitution-missing": "Автозамена не найдена", "return": "Возврат участника", "status": "Изменён этап",
    "review": "Результат отправлен на согласование", "revision": "Возврат на доработку", "approved": "Согласовано",
    "closed": "Задача закрыта", "completed": "Задача выполнена", "deadline": "Изменён дедлайн",
    "extension-request": "Запрошено продление", "comment": "Добавлен комментарий", "result": "Отправлен результат",
    "file": "Добавлен файл", "dismissal-transfer": "Передача при увольнении",
}


def is_absence_status(status):
    if status in ABSENCE_KEYS:
        return True
    row = query_one("SELECT label FROM presence_options WHERE key = ?", (status,))
    label = row["label"].casefold() if row else str(status).casefold()
    return any(word in label for word in ("отпуск", "больнич", "командиров", "обучен"))


def employee_can(feature, employee_id=None):
    employee_id = employee_id or session.get("current_employee_id")
    employee = query_one("SELECT * FROM employees WHERE id = ? AND is_dismissed = 0", (employee_id,)) if employee_id else None
    if not employee:
        return False
    explicit = query_one("SELECT can_access FROM access_rights WHERE employee_id = ? AND feature = ?", (employee_id, feature))
    if explicit is not None:
        return bool(explicit["can_access"])
    role = employee["portal_role"]
    defaults = {
        "analytics": role in LEADERSHIP_ROLES,
        "settings": role in LEADERSHIP_ROLES,
        "employees_admin": role in LEADERSHIP_ROLES,
        "rates": role in LEADERSHIP_ROLES,
        "news": role in LEADERSHIP_ROLES or role == "head",
        "rooms": role in LEADERSHIP_ROLES or role == "room_admin",
    }
    return defaults.get(feature, True)


def task_is_visible(task_id, employee_id=None):
    employee_id = employee_id or session.get("current_employee_id")
    employee = query_one("SELECT * FROM employees WHERE id=? AND is_dismissed=0", (employee_id,)) if employee_id else None
    if not employee:
        return False
    if employee["portal_role"] in LEADERSHIP_ROLES or query_one("SELECT 1 FROM task_approvers WHERE current_employee_id=? LIMIT 1", (employee_id,)):
        return True
    task = query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        return False
    participant = query_one(
        """SELECT 1 FROM task_assignees WHERE task_id=? AND employee_id=?
           UNION SELECT 1 FROM task_observers WHERE task_id=? AND employee_id=?
           UNION SELECT 1 FROM task_approvers WHERE task_id=? AND current_employee_id=?""",
        (task_id, employee_id, task_id, employee_id, task_id, employee_id),
    )
    if task["creator_id"] == employee_id or participant:
        return True
    return employee["portal_role"] == "head" and bool(query_one("SELECT 1 FROM task_departments WHERE task_id=? AND department_id=?", (task_id, employee["department_id"])))


def require_feature(feature):
    if feature in {"settings", "employees_admin", "news"} and not query_one("SELECT 1 FROM employees WHERE is_dismissed = 0 LIMIT 1"):
        return -1
    employee_id = require_current_employee()
    if not employee_id or not employee_can(feature, employee_id):
        flash("У выбранного профиля нет доступа к этому разделу.", "error")
        return None
    return employee_id


@app.before_request
def require_site_login():
    public_endpoints = {"site_login", "employee_login", "site_logout", "static"}
    if request.endpoint in public_endpoints:
        return None
    if ACCESS_PASSWORD and not session.get("site_authenticated"):
        return redirect(url_for("site_login"))
    personal_mode = os.environ.get("PIONEER_REQUIRE_PERSONAL_LOGIN", "auto")
    login_ready = bool(query_one("""SELECT 1 FROM employees WHERE is_dismissed=0
        AND portal_role IN ('director','deputy','admin') AND email IS NOT NULL AND TRIM(email)!='' AND access_code_hash IS NOT NULL"""))
    if (personal_mode == "1" or (personal_mode == "auto" and login_ready)) and not session.get("employee_authenticated"):
        return redirect(url_for("employee_login"))
    return None


@app.route("/site-login", methods=["GET", "POST"])
def site_login():
    if not ACCESS_PASSWORD:
        return redirect(url_for("employee_login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        if hmac.compare_digest(password, ACCESS_PASSWORD):
            session.clear()
            session["site_authenticated"] = True
            return redirect(url_for("employee_login"))
        flash("Неверный пароль портала.", "error")
    return render_template("site_login.html")


@app.post("/site-logout")
def site_logout():
    session.clear()
    return redirect(url_for("site_login"))


def generate_access_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        if not query_one("SELECT 1 FROM employees WHERE access_code_display = ?", (code,)):
            return code


def send_email(recipient, subject, body, employee_id=None):
    """Сохраняет письмо в журнал и отправляет его, если SMTP настроен."""
    db = get_db()
    now = datetime.now().isoformat(timespec="minutes")
    cursor = db.execute(
        "INSERT INTO email_outbox (employee_id, recipient, subject, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (employee_id, recipient, subject, body, now),
    )
    outbox_id = cursor.lastrowid
    smtp_host = os.environ.get("PIONEER_SMTP_HOST", "").strip()
    if not smtp_host:
        db.commit()
        return False
    message = EmailMessage()
    message["From"] = os.environ.get("PIONEER_SMTP_FROM") or os.environ.get("PIONEER_SMTP_USER") or "pioneer@localhost"
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        port = int(os.environ.get("PIONEER_SMTP_PORT", "587"))
        use_ssl = os.environ.get("PIONEER_SMTP_SSL") == "1"
        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_class(smtp_host, port, timeout=15) as server:
            if not use_ssl and os.environ.get("PIONEER_SMTP_TLS", "1") == "1":
                server.starttls()
            username = os.environ.get("PIONEER_SMTP_USER", "")
            password = os.environ.get("PIONEER_SMTP_PASSWORD", "")
            if username:
                server.login(username, password)
            server.send_message(message)
        db.execute("UPDATE email_outbox SET status='sent', attempts=1, sent_at=? WHERE id=?", (now, outbox_id))
        db.commit()
        return True
    except (OSError, smtplib.SMTPException) as error:
        db.execute("UPDATE email_outbox SET status='failed', attempts=1, last_error=? WHERE id=?", (str(error)[:500], outbox_id))
        db.commit()
        return False


@app.route("/login", methods=["GET", "POST"])
def employee_login():
    if ACCESS_PASSWORD and not session.get("site_authenticated"):
        return redirect(url_for("site_login"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().casefold()
        code = request.form.get("code", "").strip().upper()
        employee = query_one("SELECT * FROM employees WHERE lower(email)=?", (email,)) if email else None
        if employee and employee["is_dismissed"]:
            flash("Доступ закрыт: сотрудник находится в архиве.", "error")
        elif employee and employee["access_code_hash"] and check_password_hash(employee["access_code_hash"], code):
            site_authenticated = session.get("site_authenticated")
            session.clear()
            if site_authenticated:
                session["site_authenticated"] = True
            session["current_employee_id"] = employee["id"]
            session["employee_authenticated"] = True
            session["status_check_required"] = True
            db = get_db()
            notify_entity(db, [employee["id"]], "employee", employee["id"], f"login-reminder-{uuid.uuid4().hex}", "Проверьте свой статус присутствия и включите таймер.", url_for("profile"))
            db.commit()
            return redirect(url_for("dashboard"))
        else:
            flash("Неверная электронная почта или код доступа.", "error")
    return render_template("employee_login.html")


def default_approvers():
    employees = active_employees()
    director = next((e for e in employees if e["portal_role"] == "director"), None)
    deputies = [e for e in employees if e["portal_role"] == "deputy"]
    if not director:
        director = next((e for e in employees if "директор" in e["position"].casefold() and "зам" not in e["position"].casefold()), None)
    deputy = next((e for e in deputies if "перв" in e["position"].casefold()), deputies[0] if deputies else None)
    return [e for e in (director, deputy) if e]


def task_approver_rows(task_id):
    return query_all(
        """SELECT ta.*, original.full_name AS original_name, current.full_name AS current_name,
                  original.photo AS original_photo, current.photo AS current_photo
           FROM task_approvers ta JOIN employees original ON original.id = ta.original_employee_id
           JOIN employees current ON current.id = ta.current_employee_id
           WHERE ta.task_id = ? ORDER BY ta.id""",
        (task_id,),
    )


def task_participant_ids(task_id):
    rows = query_all(
        """SELECT employee_id AS id FROM task_assignees WHERE task_id = ?
           UNION SELECT employee_id FROM task_observers WHERE task_id = ?
           UNION SELECT current_employee_id FROM task_approvers WHERE task_id = ?""",
        (task_id, task_id, task_id),
    )
    result = {row["id"] for row in rows}
    creator = query_one("SELECT creator_id FROM tasks WHERE id=?", (task_id,))
    if creator and creator["creator_id"]:
        result.add(creator["creator_id"])
    return result


def history_event(db, task_id, event, new_names, reason="", previous_names=""):
    db.execute(
        "INSERT INTO task_history (task_id, event, previous_names, new_names, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, event, previous_names, new_names, reason, datetime.now().isoformat(timespec="minutes")),
    )


def find_replacement(employee, role_type, excluded=()):
    excluded = set(excluded) | {employee["id"]}
    if employee["substitute_id"]:
        chosen = query_one(
            "SELECT * FROM employees WHERE id = ? AND is_dismissed = 0",
            (employee["substitute_id"],),
        )
        if chosen and chosen["id"] not in excluded and not is_absence_status(chosen["presence_status"]):
            return chosen
    candidates = active_employees()
    available = [candidate for candidate in candidates if candidate["id"] not in excluded and not is_absence_status(candidate["presence_status"])]
    if role_type == "approver":
        leaders = [candidate for candidate in available if candidate["portal_role"] in LEADERSHIP_ROLES or candidate["portal_role"] == "head"]
        return leaders[0] if leaders else None
    same_department = [candidate for candidate in available if candidate["department_id"] == employee["department_id"]]
    return same_department[0] if same_department else (available[0] if available else None)


def apply_absence_substitutions(db, employee_id, status, start_date, end_date):
    employee = query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))
    status_row = query_one("SELECT label FROM presence_options WHERE key = ?", (status,))
    status_label = status_row["label"] if status_row else status
    links = []
    for role_type, table, column in (("assignee", "task_assignees", "employee_id"), ("observer", "task_observers", "employee_id")):
        for row in query_all(f"SELECT task_id FROM {table} WHERE {column} = ? AND task_id IN (SELECT id FROM tasks WHERE status = 'В работе')", (employee_id,)):
            links.append((role_type, table, row["task_id"]))
    for row in query_all("SELECT task_id FROM task_approvers WHERE current_employee_id = ? AND task_id IN (SELECT id FROM tasks WHERE status = 'В работе')", (employee_id,)):
        links.append(("approver", "task_approvers", row["task_id"]))
    for role_type, table, task_id in links:
        active_chain = query_all("SELECT replacement_employee_id FROM task_substitutions WHERE task_id = ? AND is_active = 1", (task_id,))
        if role_type == "approver":
            occupied = query_all("SELECT current_employee_id AS employee_id FROM task_approvers WHERE task_id = ?", (task_id,))
        else:
            occupied = query_all(f"SELECT employee_id FROM {table} WHERE task_id = ?", (task_id,))
        excluded = [row["replacement_employee_id"] for row in active_chain] + [row["employee_id"] for row in occupied]
        replacement = find_replacement(employee, role_type, excluded)
        if not replacement:
            task = query_one("SELECT title,creator_id FROM tasks WHERE id=?", (task_id,))
            approvers = {row["current_employee_id"] for row in task_approver_rows(task_id)}
            notify(db, approvers | ({task["creator_id"]} if task and task["creator_id"] else set()), task_id, f"substitution-missing-{uuid.uuid4().hex}", f"Для {employee['full_name']} не найдена доступная автозамена в задаче «{task['title']}». Назначьте замену вручную.", url_for("edit_task", task_id=task_id))
            history_event(db, task_id, "substitution-missing", "Замена не найдена", f"{employee['full_name']}: {status_label}")
            continue
        if role_type == "approver":
            db.execute("UPDATE task_approvers SET current_employee_id = ?, decision = 'Ожидает', comment = '', decided_at = NULL WHERE task_id = ? AND current_employee_id = ?", (replacement["id"], task_id, employee_id))
            action = "Проверку задач временно осуществляет"
        else:
            db.execute(f"DELETE FROM {table} WHERE task_id = ? AND employee_id = ?", (task_id, employee_id))
            db.execute(f"INSERT OR IGNORE INTO {table} (task_id, employee_id) VALUES (?, ?)", (task_id, replacement["id"]))
            if role_type == "assignee":
                replacement_position = query_one("SELECT id FROM employee_positions WHERE employee_id=? ORDER BY is_primary DESC,id LIMIT 1", (replacement["id"],))
                if replacement_position:
                    db.execute("INSERT OR REPLACE INTO task_positions VALUES (?,?,?)", (task_id, replacement["id"], replacement_position["id"]))
            action = "Задача перенаправлена на" if role_type == "assignee" else "Наблюдение передано"
        now = datetime.now().isoformat(timespec="minutes")
        db.execute(
            """INSERT INTO task_substitutions
               (task_id, role_type, original_employee_id, replacement_employee_id, absence_status, start_date, end_date, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, role_type, employee_id, replacement["id"], status_label, start_date, end_date, now),
        )
        text = f"Внимание! {employee['full_name']} находится в статусе «{status_label}» с {start_date} по {end_date}. {action} {replacement['full_name']}."
        recipients = task_participant_ids(task_id) | {employee_id, replacement["id"]}
        task = query_one("SELECT title FROM tasks WHERE id = ?", (task_id,))
        notify(db, recipients, task_id, f"substitution-{uuid.uuid4().hex}", text)
        history_event(db, task_id, "substitution", replacement["full_name"], text, employee["full_name"])


def restore_employee_substitutions(db, employee_id):
    employee = query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))
    if not employee or employee["is_dismissed"]:
        db.execute(
            "UPDATE task_substitutions SET is_active=0, ended_at=? WHERE original_employee_id=? AND is_active=1",
            (datetime.now().isoformat(timespec="minutes"), employee_id),
        )
        return
    substitutions = query_all(
        "SELECT * FROM task_substitutions WHERE original_employee_id = ? AND is_active = 1 ORDER BY id",
        (employee_id,),
    )
    for substitution in substitutions:
        task = query_one("SELECT * FROM tasks WHERE id = ?", (substitution["task_id"],))
        if not task or task["status"] == "Выполнено":
            db.execute("UPDATE task_substitutions SET is_active = 0, ended_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="minutes"), substitution["id"]))
            continue
        role_type = substitution["role_type"]
        active_chain = query_all("SELECT * FROM task_substitutions WHERE task_id = ? AND role_type = ? AND is_active = 1 ORDER BY id", (task["id"], role_type))
        descendant_ids = {substitution["replacement_employee_id"]}
        changed = True
        while changed:
            changed = False
            for item in active_chain:
                if item["original_employee_id"] in descendant_ids and item["replacement_employee_id"] not in descendant_ids:
                    descendant_ids.add(item["replacement_employee_id"])
                    changed = True
        if role_type == "approver":
            placeholders = ",".join("?" for _ in descendant_ids)
            db.execute(f"UPDATE task_approvers SET current_employee_id = ? WHERE task_id = ? AND current_employee_id IN ({placeholders})", (employee_id, task["id"], *descendant_ids))
        else:
            table = "task_assignees" if role_type == "assignee" else "task_observers"
            placeholders = ",".join("?" for _ in descendant_ids)
            db.execute(f"DELETE FROM {table} WHERE task_id = ? AND employee_id IN ({placeholders})", (task["id"], *descendant_ids))
            db.execute(f"INSERT OR IGNORE INTO {table} (task_id, employee_id) VALUES (?, ?)", (task["id"], employee_id))
            if role_type == "assignee":
                db.execute(f"DELETE FROM task_positions WHERE task_id=? AND employee_id IN ({placeholders})", (task["id"], *descendant_ids))
        end_time = datetime.now().isoformat(timespec="minutes")
        for item in active_chain:
            if item["original_employee_id"] == employee_id or item["original_employee_id"] in descendant_ids:
                db.execute("UPDATE task_substitutions SET is_active = 0, ended_at = ? WHERE id = ?", (end_time, item["id"]))
        text = f"{employee['full_name']} вернулся. Подмена снята, задачи возвращены к сотруднику."
        notify(db, task_participant_ids(task["id"]) | {employee_id}, task["id"], f"return-{uuid.uuid4().hex}", text)
        history_event(db, task["id"], "return", employee["full_name"], text)


def update_employee_presence(db, employee_id, status, start_date=None, end_date=None):
    employee = query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))
    if is_absence_status(status):
        if not start_date or not end_date:
            raise ValueError("Вы не указали период. Заполните, пожалуйста.")
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if end < start:
            raise ValueError("Дата окончания отсутствия не может быть раньше даты начала.")
        if employee and employee["absence_start"]:
            restore_employee_substitutions(db, employee_id)
            db.execute("UPDATE employee_absences SET returned_at=? WHERE employee_id=? AND returned_at IS NULL", (datetime.now().isoformat(timespec="minutes"), employee_id))
        starts_now = start <= date.today()
        available_status = "online" if is_absence_status(employee["presence_status"]) else employee["presence_status"]
        db.execute("UPDATE employees SET presence_status = ?, absence_start = ?, absence_end = ? WHERE id = ?", (status if starts_now else available_status, start_date, end_date, employee_id))
        db.execute("INSERT INTO employee_absences (employee_id, status_key, start_date, end_date, created_at) VALUES (?, ?, ?, ?, ?)", (employee_id, status, start_date, end_date, datetime.now().isoformat(timespec="minutes")))
        if starts_now:
            apply_absence_substitutions(db, employee_id, status, start_date, end_date)
    else:
        if employee and employee["absence_start"]:
            restore_employee_substitutions(db, employee_id)
            db.execute("UPDATE employee_absences SET returned_at = ? WHERE employee_id = ? AND returned_at IS NULL", (datetime.now().isoformat(timespec="minutes"), employee_id))
            db.execute("UPDATE employees SET absence_start = NULL, absence_end = NULL WHERE id = ?", (employee_id,))
            db.execute("UPDATE employees SET presence_status = ?, absence_start = NULL, absence_end = NULL WHERE id = ?", (status, employee_id))
        else:
            db.execute("UPDATE employees SET presence_status = ?, absence_start = NULL, absence_end = NULL WHERE id = ?", (status, employee_id))
    if status == "training":
        heads = query_all("SELECT id FROM employees WHERE is_dismissed=0 AND portal_role IN ('head','director','deputy') AND department_id=?", (employee["department_id"],))
        notify_entity(db, [row["id"] for row in heads if row["id"] != employee_id], "employee", employee_id, "training-status", f"Сотрудник {employee['full_name']} отметил статус «На обучении».", url_for("employees"))


def workday_session(employee_id, work_date=None):
    return query_one("SELECT * FROM workday_sessions WHERE employee_id=? AND work_date=?", (employee_id, work_date or date.today().isoformat()))


def start_workday(employee_id):
    today = date.today().isoformat()
    db = get_db()
    db.execute("INSERT OR IGNORE INTO workday_sessions (employee_id, work_date, started_at) VALUES (?, ?, ?)", (employee_id, today, datetime.now().isoformat(timespec="minutes")))
    db.commit()
    return workday_session(employee_id, today)


def stop_workday(employee_id):
    session_row = workday_session(employee_id)
    if session_row and not session_row["ended_at"]:
        get_db().execute("UPDATE workday_sessions SET ended_at=? WHERE id=?", (datetime.now().isoformat(timespec="minutes"), session_row["id"]))
        get_db().commit()
    return workday_session(employee_id)


def workday_hours(session_row):
    if not session_row:
        return 0
    end = session_row["ended_at"] or datetime.now().isoformat(timespec="minutes")
    return round(max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(session_row["started_at"])).total_seconds()) / 3600, 2)


def ensure_workday_reminder(employee_id):
    today_session = workday_session(employee_id)
    if not today_session or today_session["ended_at"] or today_session["reminder_sent"]:
        return
    if datetime.now().time() >= datetime.strptime("16:30", "%H:%M").time():
        db = get_db()
        notify_entity(db, [employee_id], "workday", today_session["id"], "workday-reminder", "Скоро конец рабочего дня. Не забудьте выключить таймер и сменить статус.", url_for("profile"))
        db.execute("UPDATE workday_sessions SET reminder_sent=1 WHERE id=?", (today_session["id"],))
        db.commit()


def notify(db, employee_ids, task_id, kind, text, action_url=""):
    """Уникальная пара адресат/задача/событие не допускает повторных оповещений."""
    now = datetime.now().isoformat(timespec="minutes")
    for employee_id in set(employee_ids):
        db.execute(
            """INSERT INTO notifications (employee_id, task_id, kind, text, created_at, entity_type, entity_id, action_url)
               SELECT id, ?, ?, ?, ?, 'task', ?, ? FROM employees
               WHERE id = ? AND is_dismissed = 0 AND NOT EXISTS
               (SELECT 1 FROM notifications WHERE employee_id=? AND task_id=? AND kind=?)""",
            (task_id, kind, text, now, task_id, action_url, employee_id, employee_id, task_id, kind),
        )


def notify_entity(db, employee_ids, entity_type, entity_id, kind, text, action_url=""):
    now = datetime.now().isoformat(timespec="minutes")
    for employee_id in set(employee_ids):
        db.execute(
            """INSERT INTO notifications (employee_id, kind, text, created_at, entity_type, entity_id, action_url)
               SELECT id, ?, ?, ?, ?, ?, ? FROM employees
               WHERE id=? AND is_dismissed=0 AND NOT EXISTS
               (SELECT 1 FROM notifications WHERE employee_id=? AND entity_type=? AND entity_id=? AND kind=?)""",
            (kind, text, now, entity_type, entity_id, action_url, employee_id, employee_id, entity_type, entity_id, kind),
        )


def notify_assignments(db, task_id, title, assignees, observers):
    notify(db, assignees, task_id, "assigned", f"Вам назначена задача: {title}")
    notify(db, observers, task_id, "observing", f"Вас добавили в наблюдатели задачи: {title}")


def parse_id_list(values):
    """Форма присылает идентификаторы строками; оставляем только целые числа."""
    result = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return result


def save_upload(file_storage, allowed_extensions):
    if not file_storage or not file_storage.filename:
        return None
    original_name = secure_filename(file_storage.filename)
    extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
    if extension not in allowed_extensions:
        raise ValueError("Недопустимый тип файла")
    stored_name = f"{uuid.uuid4().hex}.{extension}"
    file_storage.save(UPLOAD_DIR / stored_name)
    return stored_name, original_name


def store_task_file(db, task_id, employee_id, file_storage):
    uploaded = save_upload(file_storage, TASK_FILE_EXTENSIONS)
    if not uploaded:
        return None
    version = query_one("SELECT COALESCE(MAX(version),0)+1 AS value FROM task_files WHERE task_id=? AND original_name=?", (task_id, uploaded[1]))["value"]
    db.execute("INSERT INTO task_files (task_id,author_id,stored_name,original_name,version,created_at) VALUES (?,?,?,?,?,?)", (task_id, employee_id, uploaded[0], uploaded[1], version, datetime.now().isoformat(timespec="minutes")))
    actor = query_one("SELECT full_name FROM employees WHERE id=?", (employee_id,))["full_name"]
    history_event(db, task_id, "file", f"{uploaded[1]} (версия {version})", actor)
    return uploaded[1]


def require_current_employee():
    employee_id = session.get("current_employee_id")
    if not employee_id or not query_one("SELECT id FROM employees WHERE id = ? AND is_dismissed = 0", (employee_id,)):
        flash("Сначала выберите свой профиль через аватарку справа вверху.", "error")
        return None
    return employee_id


def task_visual_state(task):
    if task["status"] == "Выполнено":
        return "completed", "Выполнено"
    workflow_status = task["workflow_status"] if "workflow_status" in task.keys() else task["status"]
    if workflow_status == "На проверке":
        return "review", "На согласовании"
    if workflow_status == "На доработке":
        return "revision", "На доработке"
    if workflow_status == "Новая":
        return "active", "Новая"
    deadline = datetime.fromisoformat(task["deadline"])
    remaining = deadline - datetime.now()
    if remaining.total_seconds() < 0:
        return "overdue", "Просрочено"
    if remaining < timedelta(days=1):
        return "soon", "Срок близко"
    return "active", "В работе"


def task_rows(status):
    rows = query_all(
        """
        SELECT t.*, d.name AS department_name,
               GROUP_CONCAT(DISTINCT a.full_name || CASE WHEN a.is_dismissed = 1 THEN ' (уволен)' ELSE '' END) AS assignee_names,
               GROUP_CONCAT(DISTINCT o.full_name || CASE WHEN o.is_dismissed = 1 THEN ' (уволен)' ELSE '' END) AS observer_names
        FROM tasks t
        JOIN departments d ON d.id = t.department_id
        LEFT JOIN task_assignees ta ON ta.task_id = t.id
        LEFT JOIN employees a ON a.id = ta.employee_id
        LEFT JOIN task_observers tor ON tor.task_id = t.id
        LEFT JOIN employees o ON o.id = tor.employee_id
        WHERE t.status = ?
        GROUP BY t.id
         ORDER BY CASE t.priority WHEN 'Критическая' THEN 0 WHEN 'Срочная' THEN 1 WHEN 'Важная' THEN 2 ELSE 3 END, t.deadline ASC
        """,
        (status,),
    )
    return [(row, *task_visual_state(row)) for row in rows]


def create_daily_backup():
    """При первом запуске за день сохраняет копию базы и оставляет семь последних."""
    if not DATABASE.exists() or DATABASE.stat().st_size == 0:
        return
    BACKUP_DIR.mkdir(exist_ok=True)
    target = BACKUP_DIR / f"pioneer-{date.today().isoformat()}.db"
    if not target.exists():
        shutil.copy2(DATABASE, target)
    backups = sorted(BACKUP_DIR.glob("pioneer-*.db"), reverse=True)
    for old_backup in backups[7:]:
        old_backup.unlink()


def init_database():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    BACKUP_DIR.mkdir(exist_ok=True)
    create_daily_backup()
    connection = sqlite3.connect(DATABASE)
    existing_tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    connection.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS не дополняет старые таблицы: мигрируем их без потери строк.
    def add_column(table, definition):
        name = definition.split()[0]
        if not any(column[1] == name for column in connection.execute(f"PRAGMA table_info({table})")):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    add_column("employees", "presence_status TEXT NOT NULL DEFAULT 'offline'")
    add_column("employees", "is_dismissed INTEGER NOT NULL DEFAULT 0")
    add_column("employees", "absence_start TEXT")
    add_column("employees", "absence_end TEXT")
    add_column("employees", "substitute_id INTEGER")
    add_column("employees", "free_slots TEXT NOT NULL DEFAULT ''")
    add_column("employees", "portal_role TEXT NOT NULL DEFAULT 'employee'")
    add_column("employees", "email TEXT")
    add_column("employees", "access_code_hash TEXT")
    add_column("employees", "access_code_display TEXT")
    add_column("employees", "dismissed_at TEXT")
    add_column("employees", "dismissal_reason TEXT NOT NULL DEFAULT ''")
    add_column("task_substitutions", "source TEXT NOT NULL DEFAULT 'automatic'")
    add_column("task_substitutions", "reason TEXT NOT NULL DEFAULT ''")
    add_column("departments", "color TEXT NOT NULL DEFAULT '#78827d'")
    add_column("departments", "icon TEXT NOT NULL DEFAULT '•'")
    add_column("positions", "color TEXT NOT NULL DEFAULT '#78827d'")
    add_column("positions", "icon TEXT NOT NULL DEFAULT '•'")
    add_column("presence_options", "icon TEXT NOT NULL DEFAULT '•'")
    add_column("post_types", "color TEXT NOT NULL DEFAULT '#78827d'")
    add_column("post_types", "icon TEXT NOT NULL DEFAULT '•'")
    add_column("tasks", "creator_id INTEGER")
    add_column("tasks", "workflow_status TEXT NOT NULL DEFAULT 'В работе'")
    add_column("tasks", "priority TEXT NOT NULL DEFAULT 'Обычная'")
    add_column("tasks", "critical_time TEXT")
    add_column("tasks", "overdue_reason TEXT NOT NULL DEFAULT ''")
    add_column("tasks", "source_message_id INTEGER")
    add_column("tasks", "department_number INTEGER")
    add_column("posts", "subject_employee_id INTEGER")
    add_column("meetings", "event_type TEXT NOT NULL DEFAULT 'meeting'")
    add_column("meetings", "room_id INTEGER")
    add_column("meetings", "organizer_id INTEGER")
    add_column("meetings", "status TEXT NOT NULL DEFAULT 'Запланирована'")
    add_column("meetings", "recording_stored_name TEXT")
    add_column("meetings", "recording_original_name TEXT")
    notification_columns = {column[1]: column for column in connection.execute("PRAGMA table_info(notifications)")}
    if notification_columns.get("task_id") and notification_columns["task_id"][3]:
        connection.execute("DROP INDEX IF EXISTS idx_notifications_employee_read")
        connection.execute("ALTER TABLE notifications RENAME TO notifications_legacy")
        connection.execute(
            """CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL, task_id INTEGER,
                kind TEXT NOT NULL, text TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, entity_type TEXT NOT NULL DEFAULT 'task', entity_id INTEGER,
                action_url TEXT NOT NULL DEFAULT '', FOREIGN KEY (employee_id) REFERENCES employees(id),
                FOREIGN KEY (task_id) REFERENCES tasks(id))"""
        )
        connection.execute(
            """INSERT INTO notifications (id, employee_id, task_id, kind, text, is_read, created_at, entity_id)
               SELECT id, employee_id, task_id, kind, text, is_read, created_at, task_id FROM notifications_legacy"""
        )
        connection.execute("DROP TABLE notifications_legacy")
        connection.execute("CREATE INDEX idx_notifications_employee_read ON notifications(employee_id, is_read, created_at)")
    else:
        add_column("notifications", "entity_type TEXT NOT NULL DEFAULT 'task'")
        add_column("notifications", "entity_id INTEGER")
        add_column("notifications", "action_url TEXT NOT NULL DEFAULT ''")
    count = connection.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
    if count == 0 and "departments" not in existing_tables:
        connection.executemany(
            "INSERT INTO departments (name) VALUES (?)",
            [("Руководство",), ("Отдел продаж",), ("Отдел разработки",), ("Бухгалтерия",)],
        )
        departments = dict(connection.execute("SELECT name, id FROM departments").fetchall())
        today = date.today()
        employees = [
            ("Анна Петрова", departments["Руководство"], "Директор", today.replace(year=1988).isoformat(), None),
            ("Иван Соколов", departments["Отдел разработки"], "Разработчик", "1992-05-14", None),
            ("Мария Волкова", departments["Отдел продаж"], "Менеджер", "1995-11-03", None),
        ]
        connection.executemany(
            "INSERT INTO employees (full_name, department_id, position, birth_date, photo) VALUES (?, ?, ?, ?, ?)",
            employees,
        )
        employee_ids = [row[0] for row in connection.execute("SELECT id FROM employees ORDER BY id").fetchall()]
        now = datetime.now().replace(second=0, microsecond=0)
        cursor = connection.execute(
            """INSERT INTO tasks
               (title, description, department_id, created_date, deadline, status)
               VALUES (?, ?, ?, ?, ?, 'В работе')""",
            (
                "Подготовить презентацию проекта",
                "Собрать результаты команды и подготовить краткую презентацию.",
                departments["Руководство"],
                today.isoformat(),
                (now + timedelta(days=3)).isoformat(timespec="minutes"),
            ),
        )
        task_id = cursor.lastrowid
        connection.execute("INSERT INTO task_assignees VALUES (?, ?)", (task_id, employee_ids[1]))
        connection.execute("INSERT INTO task_observers VALUES (?, ?)", (task_id, employee_ids[0]))
        connection.execute(
            """INSERT INTO posts (author_id, post_type, audience_department_id, text, created_at)
               VALUES (?, 'Важное объявление', NULL, ?, ?)""",
            (employee_ids[0], "Добро пожаловать в корпоративный портал ПИОНЕР!", now.isoformat(timespec="minutes")),
        )
    # Начальные значения переносим лишь один раз: удалённые пользователем записи не возвращаются.
    if "positions" not in existing_tables:
        connection.execute("INSERT OR IGNORE INTO positions (name) SELECT DISTINCT position FROM employees WHERE TRIM(position) != ''")
    if "presence_options" not in existing_tables:
        colors = ("#23865f", "#b58a0d", "#c33f3f", "#d27616", "#2875bb", "#78827d", "#824bb4", "#269cc3", "#d53342")
        connection.executemany(
            "INSERT INTO presence_options (key, label, color) VALUES (?, ?, ?)",
            [(key, label, color) for (key, label), color in zip(PRESENCE_STATUSES, colors)],
        )
        connection.execute(
            "INSERT OR IGNORE INTO presence_options (key, label) SELECT DISTINCT presence_status, presence_status FROM employees WHERE presence_status NOT IN (SELECT key FROM presence_options)"
        )
    if "post_types" not in existing_tables:
        connection.executemany(
            "INSERT INTO post_types (key, label) VALUES (?, ?)",
            [(value, value) for value in ("Обычный", "Важное объявление", "Новый сотрудник")],
        )
        connection.execute(
            "INSERT OR IGNORE INTO post_types (key, label) SELECT DISTINCT post_type, post_type FROM posts WHERE post_type NOT IN (SELECT key FROM post_types)"
        )
    if "task_status_options" not in existing_tables:
        connection.executemany(
            "INSERT OR IGNORE INTO task_status_options (key, label, color, icon) VALUES (?, ?, ?, ?)",
            [("new", "Новая", "#78827d", "○"), ("work", "В работе", "#78827d", "●"), ("review", "На проверке", "#2875bb", "◉"),
             ("revision", "На доработке", "#d27616", "↻"), ("done", "Выполнено", "#23865f", "✓")],
        )
    connection.execute("INSERT OR IGNORE INTO task_status_options (key, label, color, icon) VALUES ('new', 'Новая', '#78827d', '○')")
    if "event_types" not in existing_tables:
        connection.executemany(
            "INSERT OR IGNORE INTO event_types (key, label, color, icon) VALUES (?, ?, ?, ?)",
             [("meeting", "Встреча", "#2875bb", "📅"), ("birthday", "День рождения", "#d27616", "🎉")],
        )
    connection.executemany(
        "INSERT OR IGNORE INTO event_types (key, label, color, icon) VALUES (?, ?, ?, ?)",
        [("vks", "ВКС", "#6657b8", "V"), ("training", "Обучение", "#23865f", "U")],
    )
    if "rooms" not in existing_tables:
        connection.executemany("INSERT OR IGNORE INTO rooms (name) VALUES (?)", [("Актовый зал",), ("Конференц-зал",)])
    connection.executemany(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
        [("portal_name", "ПИОНЕР. Внутренний портал"), ("work_start", "09:00"), ("work_end", "18:00"),
         ("notification_sound", "1"), ("analytics_period", "month"), ("high_result_threshold", "90"),
         ("overdue_threshold", "20"), ("sick_days_threshold", "10"), ("work_weekends", ""),
         ("primary_color", "#D4AF37"), ("portal_logo", ""), ("brand_palette_migrated", "0")],
    )
    if connection.execute("SELECT value FROM app_settings WHERE key='brand_palette_migrated'").fetchone()[0] == "0":
        connection.execute("UPDATE app_settings SET value='#D4AF37' WHERE key='primary_color'")
        connection.execute("UPDATE app_settings SET value='1' WHERE key='brand_palette_migrated'")
    connection.execute(
        """INSERT INTO employee_positions (employee_id, position_name, rate, is_primary)
           SELECT e.id, e.position, 1.0, 1 FROM employees e
           WHERE NOT EXISTS (SELECT 1 FROM employee_positions ep WHERE ep.employee_id = e.id AND ep.is_primary = 1)"""
    )
    connection.execute(
        "INSERT OR IGNORE INTO task_departments (task_id, department_id, is_primary) SELECT id, department_id, 1 FROM tasks"
    )
    connection.execute("INSERT OR IGNORE INTO post_reactions (post_id,employee_id,reaction) SELECT post_id,employee_id,'heart' FROM likes")
    connection.execute("INSERT OR IGNORE INTO room_booking_departments (booking_id,department_id) SELECT id,department_id FROM room_bookings")
    connection.execute("INSERT OR IGNORE INTO room_booking_responsibles (booking_id,employee_id,is_primary) SELECT id,responsible_employee_id,1 FROM room_bookings")
    for department_id, in connection.execute("SELECT id FROM departments"):
        existing_numbers = [row[0] for row in connection.execute(
            "SELECT id FROM tasks WHERE department_id=? AND department_number IS NULL ORDER BY created_date,id", (department_id,)
        )]
        current = connection.execute("SELECT COALESCE(MAX(department_number),0) FROM tasks WHERE department_id=?", (department_id,)).fetchone()[0]
        for task_id in existing_numbers:
            current += 1
            connection.execute("UPDATE tasks SET department_number=? WHERE id=?", (current, task_id))
        connection.execute("INSERT OR REPLACE INTO department_task_sequences (department_id,last_number) VALUES (?,?)", (department_id, current))
    # Роли выводятся из должности один раз; затем их можно уточнить в настройках.
    for employee in connection.execute("SELECT id, position, portal_role FROM employees"):
        if employee[2] != "employee":
            continue
        position = employee[1].casefold()
        role = "director" if "директор" in position and "зам" not in position else "deputy" if "директор" in position and "зам" in position else "head" if any(word in position for word in ("началь", "завед", "руковод")) else "employee"
        connection.execute("UPDATE employees SET portal_role = ? WHERE id = ?", (role, employee[0]))
    for employee_id, in connection.execute("SELECT id FROM employees WHERE access_code_hash IS NULL"):
        while True:
            code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
            if not connection.execute("SELECT 1 FROM employees WHERE access_code_display=?", (code,)).fetchone():
                break
        connection.execute("UPDATE employees SET access_code_hash=?,access_code_display=? WHERE id=?", (generate_password_hash(code), code, employee_id))
    approver_ids = [row[0] for row in connection.execute(
        "SELECT id FROM employees WHERE is_dismissed = 0 AND portal_role IN ('director','deputy') ORDER BY CASE portal_role WHEN 'director' THEN 0 ELSE 1 END, id LIMIT 2"
    )]
    if len(approver_ids) == 2:
        for task_id, in connection.execute("SELECT id FROM tasks WHERE status = 'В работе'"):
            if connection.execute("SELECT COUNT(*) FROM task_approvers WHERE task_id = ?", (task_id,)).fetchone()[0] == 0:
                connection.executemany(
                    "INSERT INTO task_approvers (task_id, original_employee_id, current_employee_id) VALUES (?, ?, ?)",
                    [(task_id, employee_id, employee_id) for employee_id in approver_ids],
                )
    if not connection.execute("SELECT 1 FROM chat_rooms WHERE kind = 'general'").fetchone():
        connection.execute("INSERT INTO chat_rooms (name, kind) VALUES ('Общий чат ПИОНЕР', 'general')")
    connection.commit()
    connection.close()


@app.context_processor
def inject_layout_data():
    employees = active_employees()
    current_id = session.get("current_employee_id")
    current_employee = query_one("SELECT * FROM employees WHERE id = ? AND is_dismissed = 0", (current_id,)) if current_id else None
    if current_id and not current_employee:
        session.pop("current_employee_id", None)
    unread = 0
    my_tasks = 0
    if current_employee:
        # Будущие периоды активируются при первом обращении к порталу в день начала.
        starting = query_all("""SELECT e.id,a.status_key,a.start_date,a.end_date FROM employees e
            JOIN employee_absences a ON a.employee_id=e.id AND a.returned_at IS NULL
            WHERE e.is_dismissed=0 AND e.absence_start=a.start_date AND e.absence_end=a.end_date
              AND a.start_date<=? AND e.presence_status!=a.status_key""", (date.today().isoformat(),))
        db = get_db()
        for absence in starting:
            db.execute("UPDATE employees SET presence_status=? WHERE id=?", (absence["status_key"], absence["id"]))
            apply_absence_substitutions(db, absence["id"], absence["status_key"], absence["start_date"], absence["end_date"])
        expired = query_all("SELECT id FROM employees WHERE absence_end IS NOT NULL AND absence_end < ?", (date.today().isoformat(),))
        for employee in expired:
            notify_entity(db, [employee["id"]], "employee", employee["id"], "absence-ended", "Период отсутствия завершён. Проверьте свой статус и при необходимости установите «В сети».", url_for("profile"))
            update_employee_presence(db, employee["id"], "online")
        if expired:
            db.commit()
        # Просрочку создаём при открытии любой страницы, без фонового сервера.
        overdue = query_all("SELECT id, title FROM tasks WHERE status = 'В работе' AND deadline < ?", (datetime.now().isoformat(timespec="minutes"),))
        db = get_db()
        for task in overdue:
            recipients = [person["id"] for role in ("assignees", "observers") for person in task_people(task["id"], role)]
            recipients.extend(row["current_employee_id"] for row in task_approver_rows(task["id"]))
            notify(db, recipients, task["id"], "overdue", f"Просрочена задача: {task['title']}")
        db.commit()
        ensure_workday_reminder(current_id)
        unread = query_one("SELECT COUNT(*) AS total FROM notifications WHERE employee_id = ? AND is_read = 0", (current_id,))["total"]
        my_tasks = query_one(
            """SELECT COUNT(DISTINCT t.id) AS total FROM tasks t
               LEFT JOIN task_assignees ta ON ta.task_id = t.id
               LEFT JOIN task_observers tor ON tor.task_id = t.id
               WHERE t.status = 'В работе' AND (ta.employee_id = ? OR tor.employee_id = ?)""",
            (current_id, current_id),
        )["total"]
    settings_rows = query_all("SELECT key, value FROM app_settings")
    app_options = {row["key"]: row["value"] for row in settings_rows}
    return {
        "layout_employees": employees, "current_employee": current_employee, "unread_count": unread,
        "my_task_count": my_tasks, "portal_name": app_options.get("portal_name", "ПИОНЕР. Внутренний портал"),
        "portal_logo": app_options.get("portal_logo", ""), "primary_color": app_options.get("primary_color", "#D4AF37"),
        "can_analytics": employee_can("analytics") if current_employee else False,
        "can_settings": employee_can("settings") if current_employee else False,
        "can_employees_admin": employee_can("employees_admin") if current_employee else False,
        "can_rates": employee_can("rates") if current_employee else False,
        "notification_sound": app_options.get("notification_sound", "1") == "1",
        "workday": workday_session(current_id) if current_employee else None,
        "workday_hours": workday_hours(workday_session(current_id)) if current_employee else 0,
        "status_check_required": bool(current_employee and session.get("status_check_required")),
        "layout_statuses": catalog_rows("statuses"),
    }


@app.template_filter("ru_datetime")
def ru_datetime(value):
    if not value:
        return "—"
    parsed = value if isinstance(value, (date, datetime)) else datetime.fromisoformat(value)
    return parsed.strftime("%d.%m.%Y %H:%M")


@app.template_filter("ru_date")
def ru_date(value):
    if not value:
        return "—"
    parsed = value if isinstance(value, (date, datetime)) else date.fromisoformat(value)
    return parsed.strftime("%d.%m.%Y")


@app.template_filter("full_years")
def full_years(value):
    birthday = date.fromisoformat(value)
    today = date.today()
    return today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))


@app.template_filter("birthday_genitive")
def birthday_genitive(full_name):
    """Склоняет типичные русские ФИО; неизменяемые фамилии оставляет как есть."""
    words = full_name.split()
    if len(words) < 2:
        return full_name
    surname_endings = ("ов", "ев", "ёв", "ин", "ова", "ева", "ёва", "ина", "ский", "ская", "цкий", "цкая", "ая", "ый", "ой")
    surname_first = len(words) >= 3 and words[0].lower().endswith(surname_endings)
    surname_index, name_index = (0, 1) if surname_first else (1, 0)
    patronymic = words[2].lower() if len(words) >= 3 else ""
    name = words[name_index].lower()
    female = patronymic.endswith(("вна", "чна")) or (
        not patronymic.endswith("ич") and name.endswith(("а", "я"))
        and name not in {"илья", "никита", "кузьма", "фома", "лука"}
    ) or name in {"любовь", "нинэль"}

    def decline(word, role):
        lower = word.lower()
        if role == "surname":
            if lower.endswith(("ская", "цкая", "ая")):
                return word[:-2] + "ой"
            if lower.endswith(("ский", "цкий")):
                return word[:-2] + "ого"
            if lower.endswith(("ый", "ой")):
                return word[:-2] + "ого" if not female else word
            if lower.endswith(("ова", "ева", "ёва", "ина")):
                return word[:-1] + "ой"
            if lower.endswith(("ов", "ев", "ёв", "ин")) and not female:
                return word + "а"
            return word
        if role == "patronymic" and lower.endswith(("овна", "евна", "ёвна", "ична")):
            return word[:-1] + "ы"
        if lower.endswith("ия"):
            return word[:-1] + "и"
        if lower.endswith("ья"):
            return word[:-1] + "и"
        if lower.endswith("я"):
            return word[:-1] + "и"
        if lower.endswith("а"):
            return word[:-1] + ("и" if lower[-2] in "гкхжчшщ" else "ы")
        if lower.endswith(("й", "ь")):
            return word[:-1] + ("и" if female else "я")
        if not female and lower[-1] in "бвгджзклмнпрстфхцчшщ":
            return word + "а"
        return word

    result = words[:]
    result[name_index] = decline(words[name_index], "name")
    result[surname_index] = decline(words[surname_index], "surname")
    if len(words) >= 3:
        result[2] = decline(words[2], "patronymic")
    return " ".join(result)


@app.route("/")
def dashboard():
    today = date.today()
    employees = query_all(
        """SELECT e.*, d.name AS department_name
           FROM employees e JOIN departments d ON d.id = e.department_id WHERE e.is_dismissed = 0 ORDER BY e.full_name"""
    )
    birthdays = [e for e in employees if date.fromisoformat(e["birth_date"]).strftime("%m-%d") == today.strftime("%m-%d")]
    if birthdays:
        author = (default_approvers() or employees[:1])
        if author:
            db = get_db()
            for employee in birthdays:
                exists = query_one("SELECT 1 FROM posts WHERE subject_employee_id = ? AND created_at >= ? AND created_at < ?", (employee["id"], today.isoformat(), (today + timedelta(days=1)).isoformat()))
                if not exists:
                    db.execute(
                        "INSERT INTO posts (author_id, post_type, text, created_at, subject_employee_id) VALUES (?, 'Обычный', ?, ?, ?)",
                        (author[0]["id"], f"Сегодня день рождения у {birthday_genitive(employee['full_name'])}! Поздравляем!", datetime.now().isoformat(timespec="minutes"), employee["id"]),
                    )
            db.commit()
    current_id = session.get("current_employee_id")
    active = [item for item in task_rows("В работе") if current_id and task_is_visible(item[0]["id"], current_id)]
    completed = [item for item in task_rows("Выполнено") if current_id and task_is_visible(item[0]["id"], current_id)]
    stats = {
        "active": len(active),
        "overdue": sum(1 for _row, state, _label in active if state == "overdue"),
        "completed": len(completed),
    }
    posts = load_posts(limit=3)
    meetings = query_all(
        "SELECT * FROM meetings WHERE meeting_at >= ? AND status!='Отменена' ORDER BY meeting_at LIMIT 4",
        (datetime.now().isoformat(timespec="minutes"),),
    )
    today_meetings = query_all(
        "SELECT * FROM meetings WHERE meeting_at >= ? AND meeting_at < ? AND status!='Отменена' ORDER BY meeting_at",
        (today.isoformat(), (today + timedelta(days=1)).isoformat()),
    )
    important_posts = query_all(
        """SELECT p.*, e.full_name AS author_name FROM posts p
           JOIN employees e ON e.id = p.author_id
           WHERE p.post_type = 'Важное объявление' AND p.created_at >= ? AND p.created_at < ?
           ORDER BY p.created_at DESC""",
        (today.isoformat(), (today + timedelta(days=1)).isoformat()),
    )
    task_avatars = {task["id"]: task_people(task["id"], "assignees") for task, _state, _label in active[:5]}
    my_today_tasks = [item for item in active if current_id and any(person["id"] == current_id for person in task_people(item[0]["id"], "assignees")) and item[0]["deadline"][:10] <= today.isoformat()]
    recent_notifications = query_all("SELECT * FROM notifications WHERE employee_id=? ORDER BY created_at DESC,id DESC LIMIT 5", (current_id,)) if current_id else []
    return render_template(
        "dashboard.html", birthdays=birthdays, stats=stats, tasks=active[:5], posts=posts,
        meetings=meetings, today_meetings=today_meetings, important_posts=important_posts, task_avatars=task_avatars,
        my_today_tasks=my_today_tasks, recent_notifications=recent_notifications,
    )


@app.post("/current-employee")
def set_current_employee():
    if session.get("employee_authenticated"):
        flash("В персональном режиме смена пользователя доступна только после выхода.", "error")
        return redirect(request.referrer or url_for("dashboard"))
    employee_id = request.form.get("employee_id", type=int)
    if employee_id and query_one("SELECT id FROM employees WHERE id = ? AND is_dismissed = 0", (employee_id,)):
        session["current_employee_id"] = employee_id
    else:
        session.pop("current_employee_id", None)
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/profile/confirm-status")
def confirm_status():
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("employee_login"))
    status = request.form.get("presence_status", "")
    if not valid_catalog_value("statuses", status):
        flash("Выберите актуальный статус.", "error")
    else:
        try:
            update_employee_presence(get_db(), employee_id, status, request.form.get("absence_start"), request.form.get("absence_end"))
            start_workday(employee_id)
            get_db().commit()
            session.pop("status_check_required", None)
            flash("Статус подтверждён.", "success")
        except ValueError as error:
            get_db().rollback()
            flash(str(error), "error")
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/workday/start")
def start_workday_route():
    employee_id = require_current_employee()
    if employee_id:
        start_workday(employee_id)
        flash("Таймер рабочего дня включён.", "success")
    return redirect(request.referrer or url_for("profile"))


@app.post("/workday/stop")
def stop_workday_route():
    employee_id = require_current_employee()
    if employee_id:
        stop_workday(employee_id)
        flash("Таймер рабочего дня остановлен.", "success")
    return redirect(request.referrer or url_for("profile"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    employee_id = session.get("current_employee_id")
    if not employee_id:
        return render_template("profile_select.html", employees=active_employees())
    employee = query_one(
        """SELECT e.*, d.name AS department_name, s.label AS status_label, s.color AS status_color
           FROM employees e JOIN departments d ON d.id = e.department_id
           LEFT JOIN presence_options s ON s.key = e.presence_status WHERE e.id = ? AND e.is_dismissed = 0""",
        (employee_id,),
    )
    if not employee:
        session.pop("current_employee_id", None)
        flash("Выберите действующего сотрудника для входа в профиль.", "error")
        return redirect(url_for("profile"))
    statuses = catalog_rows("statuses")
    if request.method == "POST":
        status = request.form.get("presence_status", "")
        if not valid_catalog_value("statuses", status):
            flash("Выберите статус из списка. Если список пуст, добавьте статус в Настройках.", "error")
        else:
            try:
                uploaded = save_upload(request.files.get("photo"), IMAGE_EXTENSIONS)
                db = get_db()
                update_employee_presence(
                    db, employee_id, status,
                    request.form.get("absence_start") or None,
                    request.form.get("absence_end") or None,
                )
                if uploaded:
                    db.execute("UPDATE employees SET photo = ? WHERE id = ?", (uploaded[0], employee_id))
                db.execute("UPDATE employees SET free_slots = ? WHERE id = ?", (request.form.get("free_slots", "").strip(), employee_id))
                db.commit()
                flash("Личный профиль обновлён.", "success")
                return redirect(url_for("profile"))
            except ValueError as error:
                get_db().rollback()
                flash(str(error), "error")
    my_tasks = query_all(
        """SELECT DISTINCT t.*, d.name AS department_name FROM tasks t JOIN departments d ON d.id = t.department_id
           LEFT JOIN task_assignees ta ON ta.task_id = t.id LEFT JOIN task_observers tor ON tor.task_id = t.id
           WHERE ta.employee_id = ? OR tor.employee_id = ? ORDER BY t.status, t.deadline""",
        (employee_id, employee_id),
    )
    return render_template("profile.html", employee=employee, statuses=statuses, my_tasks=my_tasks, workday=workday_session(employee_id), workday_hours=workday_hours(workday_session(employee_id)))


@app.route("/settings")
def settings():
    if not require_feature("settings"):
        return redirect(url_for("profile"))
    sections = [(slug, config[0], config[2], config[3], catalog_rows(slug)) for slug, config in CATALOGS.items()]
    rights = query_all("""SELECT e.id, e.full_name, e.portal_role, d.name AS department_name FROM employees e
        JOIN departments d ON d.id=e.department_id WHERE e.is_dismissed=0 ORDER BY e.full_name""")
    rights_map = {(row["employee_id"], row["feature"]): row for row in query_all("SELECT * FROM access_rights")}
    options = {row["key"]: row["value"] for row in query_all("SELECT * FROM app_settings")}
    return render_template("settings.html", sections=sections, employees=rights, rights_map=rights_map, options=options)


@app.get("/settings/export")
def export_settings():
    if not require_feature("settings"):
        return redirect(url_for("profile"))
    options = query_all("SELECT key,value FROM app_settings ORDER BY key")
    rights = query_all("""SELECT e.full_name,e.portal_role,ar.feature,ar.can_access FROM employees e
        LEFT JOIN access_rights ar ON ar.employee_id=e.id WHERE e.is_dismissed=0 ORDER BY e.full_name,ar.feature""")
    if request.args.get("format") == "docx":
        from docx import Document
        document = Document()
        document.add_heading("ПИОНЕР. Настройки и права", 0)
        document.add_heading("Общие параметры", level=1)
        for row in options:
            document.add_paragraph(f"{row['key']}: {row['value']}")
        table = document.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        for cell, title in zip(table.rows[0].cells, ["Сотрудник", "Роль", "Право", "Доступ"]):
            cell.text = title
        for row in rights:
            access_label = "По роли" if row["feature"] is None else ("Да" if row["can_access"] else "Нет")
            for cell, value in zip(table.add_row().cells, [row["full_name"], row["portal_role"], row["feature"] or "Все права", access_label]):
                cell.text = str(value)
        output = io.BytesIO()
        document.save(output)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": "attachment; filename=pioneer-settings.docx"})
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Параметры"
    sheet.append(["Параметр", "Значение"])
    for row in options:
        sheet.append([row["key"], row["value"]])
    rights_sheet = workbook.create_sheet("Права")
    rights_sheet.append(["Сотрудник", "Роль", "Право", "Доступ"])
    for row in rights:
        access_label = "По роли" if row["feature"] is None else ("Да" if row["can_access"] else "Нет")
        rights_sheet.append([row["full_name"], row["portal_role"], row["feature"] or "Все права", access_label])
    output = io.BytesIO()
    workbook.save(output)
    return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=pioneer-settings.xlsx"})


@app.post("/settings/<catalog>/add")
def add_catalog_value(catalog):
    if not require_feature("settings"):
        return redirect(url_for("profile"))
    if catalog not in CATALOGS:
        abort(404)
    _title, table, key, label_column = CATALOGS[catalog]
    name = request.form.get("name", "").strip()
    color = request.form.get("color", "#78827d")
    icon = request.form.get("icon", "•").strip()[:4] or "•"
    if not name or len(name) > 100:
        flash("Введите название длиной от 1 до 100 символов.", "error")
    elif any(row[label_column].casefold() == name.casefold() for row in catalog_rows(catalog)):
        flash("Такое значение уже есть в справочнике.", "error")
    elif len(color) != 7 or color[0] != "#" or any(ch not in "0123456789abcdefABCDEF" for ch in color[1:]):
        flash("Выберите корректный цвет статуса.", "error")
    else:
        db = get_db()
        try:
            if catalog == "statuses":
                db.execute("INSERT INTO presence_options (key, label, color, icon) VALUES (?, ?, ?, ?)", (uuid.uuid4().hex, name, color, icon))
            elif catalog in {"task-statuses", "event-types"}:
                db.execute(f"INSERT INTO {table} (key, label, color, icon) VALUES (?, ?, ?, ?)", (uuid.uuid4().hex, name, color, icon))
            elif catalog == "post-types":
                db.execute("INSERT INTO post_types (key, label, color, icon) VALUES (?, ?, ?, ?)", (uuid.uuid4().hex, name, color, icon))
            elif catalog == "rooms":
                db.execute("INSERT INTO rooms (name, responsible_employee_id, color, icon) VALUES (?, ?, ?, ?)", (name, request.form.get("responsible_employee_id", type=int), color, icon))
            else:
                db.execute(f"INSERT INTO {table} ({label_column}, color, icon) VALUES (?, ?, ?)", (name, color, icon))
            db.commit()
            flash("Значение добавлено.", "success")
        except sqlite3.IntegrityError:
            db.rollback()
            flash("Такое значение уже существует.", "error")
    return redirect(url_for("settings", _anchor=catalog))


@app.post("/settings/<catalog>/<value>/rename")
def rename_catalog_value(catalog, value):
    if not require_feature("settings"):
        return redirect(url_for("profile"))
    if catalog not in CATALOGS:
        abort(404)
    _title, table, key, label_column = CATALOGS[catalog]
    old = query_one(f"SELECT * FROM {table} WHERE {key} = ?", (value,))
    if not old:
        abort(404)
    name = request.form.get("name", "").strip()
    color = request.form.get("color") or old["color"] or "#78827d"
    icon = (request.form.get("icon") or old["icon"] or "•")[:4]
    if not name or len(name) > 100:
        flash("Введите название длиной от 1 до 100 символов.", "error")
    elif any(row[key] != old[key] and row[label_column].casefold() == name.casefold() for row in catalog_rows(catalog)):
        flash("Такое значение уже есть в справочнике.", "error")
    elif len(color) != 7 or color[0] != "#" or any(ch not in "0123456789abcdefABCDEF" for ch in color[1:]):
        flash("Выберите корректный цвет статуса.", "error")
    else:
        db = get_db()
        try:
            db.execute(f"UPDATE {table} SET {label_column} = ? WHERE {key} = ?", (name, value))
            if catalog == "statuses":
                db.execute("UPDATE presence_options SET color = ?, icon = ? WHERE key = ?", (color, icon, value))
            elif catalog in {"task-statuses", "event-types"}:
                db.execute(f"UPDATE {table} SET color = ?, icon = ? WHERE {key} = ?", (color, icon, value))
            elif catalog == "rooms":
                db.execute("UPDATE rooms SET color = ?, icon = ?, responsible_employee_id = ? WHERE id = ?", (color, icon, request.form.get("responsible_employee_id", type=int), value))
            else:
                db.execute(f"UPDATE {table} SET color = ?, icon = ? WHERE {key} = ?", (color, icon, value))
            if catalog == "positions":
                db.execute("UPDATE employees SET position = ? WHERE position = ?", (name, old["name"]))
            db.commit()
            flash("Название обновлено.", "success")
        except sqlite3.IntegrityError:
            db.rollback()
            flash("Такое название уже существует.", "error")
    return redirect(url_for("settings", _anchor=catalog))


@app.post("/settings/<catalog>/<value>/delete")
def delete_catalog_value(catalog, value):
    if not require_feature("settings"):
        return redirect(url_for("profile"))
    if catalog not in CATALOGS:
        abort(404)
    _title, table, key, _label = CATALOGS[catalog]
    row = query_one(f"SELECT * FROM {table} WHERE {key} = ?", (value,))
    if not row:
        abort(404)
    # Для каждого справочника проверяем только его реальные ссылки в других таблицах.
    if catalog == "departments":
        dependencies = (("employees", "department_id", row[key]), ("tasks", "department_id", row[key]), ("posts", "audience_department_id", row[key]))
    elif catalog == "positions":
        dependencies = (("employees", "position", row["name"]),)
    elif catalog == "statuses":
        dependencies = (("employees", "presence_status", row[key]),)
    elif catalog == "post-types":
        dependencies = (("posts", "post_type", row[key]),)
    elif catalog == "rooms":
        dependencies = (("room_bookings", "room_id", row[key]),)
    else:
        dependencies = ()
    if any(query_one(f"SELECT 1 FROM {dependent_table} WHERE {column} = ? LIMIT 1", (identifier,)) for dependent_table, column, identifier in dependencies):
        flash("Удаление невозможно: значение используется в текущих записях.", "error")
    else:
        try:
            get_db().execute(f"DELETE FROM {table} WHERE {key} = ?", (value,))
            get_db().commit()
            flash("Значение удалено.", "success")
        except sqlite3.IntegrityError:
            get_db().rollback()
            flash("Удаление невозможно: значение используется в текущих записях.", "error")
    return redirect(url_for("settings", _anchor=catalog))


@app.post("/settings/rights")
def save_rights():
    if not require_feature("settings"):
        return redirect(url_for("profile"))
    actor = query_one("SELECT portal_role FROM employees WHERE id=? AND is_dismissed=0", (session.get("current_employee_id"),))
    if not actor or actor["portal_role"] not in {"director", "admin"}:
        flash("Назначать роли и персональные права может только директор или администратор.", "error")
        return redirect(url_for("settings", _anchor="rights"))
    features = ("rooms", "analytics", "news", "employees_admin", "settings", "rates")
    employees = active_employees()
    db = get_db()
    for employee in employees:
        role = request.form.get(f"role_{employee['id']}", "employee")
        if role not in {"employee", "head", "director", "deputy", "admin", "room_admin"}:
            role = "employee"
        db.execute("UPDATE employees SET portal_role = ?, substitute_id = ? WHERE id = ?", (role, request.form.get(f"substitute_{employee['id']}", type=int), employee["id"]))
        for feature in features:
            db.execute(
                "INSERT OR REPLACE INTO access_rights (employee_id, feature, can_access, backup_employee_id) VALUES (?, ?, ?, ?)",
                (employee["id"], feature, 1 if request.form.get(f"{feature}_{employee['id']}") else 0, request.form.get(f"backup_{feature}_{employee['id']}", type=int)),
            )
    db.commit()
    flash("Роли, права и запасные сотрудники сохранены.", "success")
    return redirect(url_for("settings", _anchor="rights"))


@app.post("/settings/options")
def save_options():
    if not require_feature("settings"):
        return redirect(url_for("profile"))
    allowed = {"portal_name", "work_start", "work_end", "work_weekends", "notification_sound", "analytics_period", "high_result_threshold", "overdue_threshold", "sick_days_threshold", "primary_color"}
    work_start = request.form.get("work_start", "")
    work_end = request.form.get("work_end", "")
    try:
        if work_start and work_end and datetime.strptime(work_end, "%H:%M") <= datetime.strptime(work_start, "%H:%M"):
            raise ValueError("Окончание рабочего дня должно быть позже начала.")
        if request.form.get("analytics_period", "month") not in {"week", "month", "quarter", "six_months", "year", "all"}:
            raise ValueError("Выберите допустимый период аналитики.")
        for key in ("high_result_threshold", "overdue_threshold"):
            if not 0 <= int(request.form.get(key, "0")) <= 100:
                raise ValueError("Процентные пороги должны быть от 0 до 100.")
        if int(request.form.get("sick_days_threshold", "0")) < 0:
            raise ValueError("Порог дней болезни не может быть отрицательным.")
    except (ValueError, TypeError) as error:
        flash(str(error), "error")
        return redirect(url_for("settings", _anchor="options"))
    db = get_db()
    for key in allowed:
        value = "1" if key == "notification_sound" and request.form.get(key) else "0" if key == "notification_sound" else request.form.get(key, "").strip()
        if value:
            db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    try:
        uploaded = save_upload(request.files.get("portal_logo"), IMAGE_EXTENSIONS)
        if uploaded:
            db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('portal_logo', ?)", (uploaded[0],))
    except ValueError as error:
        db.rollback()
        flash(str(error), "error")
        return redirect(url_for("settings", _anchor="options"))
    db.commit()
    flash("Параметры портала сохранены.", "success")
    return redirect(url_for("settings", _anchor="options"))


@app.route("/tasks")
def tasks():
    rows = filtered_task_rows("В работе")
    current_id = session.get("current_employee_id")
    rows = [item for item in rows if current_id and task_is_visible(item[0]["id"], current_id)]
    people = {task["id"]: (task_people(task["id"], "assignees"), task_people(task["id"], "observers")) for task, _state, _label in rows}
    return render_template("tasks.html", tasks=rows, people=people, title="Задачи в работе", completed=False, departments=catalog_rows("departments"))


@app.route("/tasks/completed")
def completed_tasks():
    rows = filtered_task_rows("Выполнено")
    current_id = session.get("current_employee_id")
    rows = [item for item in rows if current_id and task_is_visible(item[0]["id"], current_id)]
    people = {task["id"]: (task_people(task["id"], "assignees"), task_people(task["id"], "observers")) for task, _state, _label in rows}
    return render_template("tasks.html", tasks=rows, people=people, title="Выполненные задачи", completed=True, departments=catalog_rows("departments"))


def filtered_task_rows(status):
    rows = task_rows(status)
    department_id = request.args.get("department_id", type=int)
    workflow = request.args.get("workflow", "")
    priority = request.args.get("priority", "")
    deadline_from = request.args.get("deadline_from", "")
    deadline_to = request.args.get("deadline_to", "")
    if department_id:
        rows = [item for item in rows if item[0]["department_id"] == department_id]
    if workflow:
        rows = [item for item in rows if item[0]["workflow_status"] == workflow]
    if priority:
        rows = [item for item in rows if item[0]["priority"] == priority]
    if deadline_from:
        rows = [item for item in rows if item[0]["deadline"][:10] >= deadline_from]
    if deadline_to:
        rows = [item for item in rows if item[0]["deadline"][:10] <= deadline_to]
    return rows


@app.get("/tasks/export")
def export_tasks():
    status = "Выполнено" if request.args.get("completed") == "1" else "В работе"
    rows = filtered_task_rows(status)
    current_id = session.get("current_employee_id")
    rows = [item for item in rows if current_id and task_is_visible(item[0]["id"], current_id)]
    if request.args.get("format") == "docx":
        from docx import Document
        document = Document()
        document.add_heading("ПИОНЕР. Задачи", 0)
        table = document.add_table(rows=1, cols=8)
        table.style = "Table Grid"
        headers = ["№", "Задача", "Отдел", "Приоритет", "Статус", "Дедлайн", "Исполнители", "Наблюдатели"]
        for cell, header in zip(table.rows[0].cells, headers):
            cell.text = header
        for task, _state, label in rows:
            cells = table.add_row().cells
            values = [task["department_number"] or task["id"], task["title"], task["department_name"], task["priority"], label, ru_datetime(task["deadline"]), task["assignee_names"] or "", task["observer_names"] or ""]
            for cell, value in zip(cells, values):
                cell.text = str(value)
        output = io.BytesIO()
        document.save(output)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": "attachment; filename=pioneer-tasks.docx"})
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Задачи"
    sheet.append(["№", "Задача", "Отдел", "Приоритет", "Статус", "Дедлайн", "Исполнители", "Наблюдатели"])
    for task, _state, label in rows:
        sheet.append([task["department_number"] or task["id"], task["title"], task["department_name"], task["priority"], label, task["deadline"], task["assignee_names"] or "", task["observer_names"] or ""])
    for cell in sheet[1]:
        cell.font = cell.font.copy(bold=True)
    output = io.BytesIO()
    workbook.save(output)
    return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=pioneer-tasks.xlsx"})


def task_form_data():
    departments = query_all("SELECT * FROM departments ORDER BY name")
    employees = active_employees()
    return departments, employees, default_approvers()


def department_heads(department_ids):
    result = []
    for employee in active_employees():
        if employee["department_id"] not in set(department_ids) or employee["portal_role"] != "head":
            continue
        if is_absence_status(employee["presence_status"]):
            replacement = find_replacement(employee, "observer", result)
            if replacement:
                result.append(replacement["id"])
        else:
            result.append(employee["id"])
    return result


def parse_task_form():
    helper_departments = parse_id_list(request.form.getlist("helper_departments"))
    assignees = set(parse_id_list(request.form.getlist("assignees")))
    assignees.update(parse_id_list(request.form.getlist("helper_assignees")))
    observers = set(parse_id_list(request.form.getlist("observers")))
    department_id = request.form.get("department_id", type=int)
    observers.update(department_heads(([department_id] if department_id else []) + helper_departments))
    return {
        "title": request.form.get("title", "").strip(),
        "description": request.form.get("description", "").strip(),
        "department_id": department_id,
        "deadline": request.form.get("deadline", ""),
        "assignees": sorted(assignees),
        "observers": sorted(observers),
        "approvers": parse_id_list(request.form.getlist("approvers")),
        "helper_departments": list(dict.fromkeys(helper_departments)),
        "priority": request.form.get("priority", "Обычная"),
        "critical_time": request.form.get("critical_time", ""),
        "overdue_reason": request.form.get("overdue_reason", "").strip(),
    }


def validate_task_form(data, task_id=None):
    participant_ids = data["assignees"] + data["observers"] + data["approvers"]
    valid_priorities = {"Обычная", "Важная", "Срочная", "Критическая"}
    if not data["title"] or not data["department_id"] or not valid_catalog_value("departments", data["department_id"]) or not data["deadline"] or not data["assignees"]:
        return "Заполните название, отдел-исполнитель, дедлайн и выберите исполнителя."
    if len(set(data["approvers"])) != 2:
        return "Выберите двух согласующих: директора и первого заместителя либо их заменяющих."
    if data["priority"] not in valid_priorities or (data["priority"] == "Критическая" and not data["critical_time"]):
        return "Для критической задачи обязательно укажите время исполнения сегодня."
    if any(not query_one("SELECT 1 FROM employees WHERE id = ? AND is_dismissed = 0", (item,)) for item in participant_ids):
        return "В задаче выбран недействующий сотрудник."
    try:
        datetime.fromisoformat(data["deadline"])
    except ValueError:
        return "Укажите корректный дедлайн."
    return None


def save_task_relations(db, task_id, data, replace=False):
    if replace:
        for table in ("task_assignees", "task_observers", "task_departments", "task_positions"):
            db.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
        placeholders = ",".join("?" for _ in data["approvers"])
        if placeholders:
            db.execute(f"DELETE FROM task_approvers WHERE task_id=? AND original_employee_id NOT IN ({placeholders})", (task_id, *data["approvers"]))
    db.executemany("INSERT OR IGNORE INTO task_assignees VALUES (?, ?)", [(task_id, item) for item in data["assignees"]])
    db.executemany("INSERT OR IGNORE INTO task_observers VALUES (?, ?)", [(task_id, item) for item in data["observers"]])
    db.execute("INSERT OR IGNORE INTO task_departments VALUES (?, ?, 1)", (task_id, data["department_id"]))
    db.executemany("INSERT OR IGNORE INTO task_departments VALUES (?, ?, 0)", [(task_id, item) for item in data["helper_departments"] if item != data["department_id"]])
    for employee_id in data["approvers"]:
        if not query_one("SELECT 1 FROM task_approvers WHERE task_id=? AND original_employee_id=?", (task_id, employee_id)):
            approver = query_one("SELECT * FROM employees WHERE id=?", (employee_id,))
            replacement = find_replacement(approver, "approver", [row["current_employee_id"] for row in task_approver_rows(task_id)]) if approver and is_absence_status(approver["presence_status"]) else None
            current_id = replacement["id"] if replacement else employee_id
            db.execute("INSERT INTO task_approvers (task_id, original_employee_id, current_employee_id) VALUES (?, ?, ?)", (task_id, employee_id, current_id))
            if replacement:
                db.execute("""INSERT INTO task_substitutions (task_id,role_type,original_employee_id,replacement_employee_id,absence_status,start_date,end_date,created_at)
                    VALUES (?,?,?,?,?,?,?,?)""", (task_id, "approver", employee_id, current_id, approver["presence_status"], approver["absence_start"] or date.today().isoformat(), approver["absence_end"] or date.today().isoformat(), datetime.now().isoformat(timespec="minutes")))
    for employee_id in data["assignees"]:
        position_id = request.form.get(f"position_{employee_id}", type=int)
        if position_id and query_one("SELECT 1 FROM employee_positions WHERE id = ? AND employee_id = ?", (position_id, employee_id)):
            db.execute("INSERT OR REPLACE INTO task_positions VALUES (?, ?, ?)", (task_id, employee_id, position_id))


@app.route("/tasks/new", methods=["GET", "POST"])
def new_task():
    if not require_current_employee():
        return redirect(url_for("profile"))
    departments, employees, default_selected_approvers = task_form_data()
    source_message = request.args.get("message", "").strip()
    if request.method == "POST":
        data = parse_task_form()
        error = validate_task_form(data)
        if error:
            flash(error, "error")
        else:
            try:
                db = get_db()
                db.execute("INSERT OR IGNORE INTO department_task_sequences (department_id,last_number) VALUES (?,0)", (data["department_id"],))
                db.execute("UPDATE department_task_sequences SET last_number=last_number+1 WHERE department_id=?", (data["department_id"],))
                department_number = db.execute("SELECT last_number FROM department_task_sequences WHERE department_id=?", (data["department_id"],)).fetchone()[0]
                cursor = db.execute(
                    """INSERT INTO tasks
                       (title, description, department_id, created_date, deadline, creator_id, workflow_status, priority, critical_time, overdue_reason, department_number)
                       VALUES (?, ?, ?, ?, ?, ?, 'Новая', ?, ?, ?, ?)""",
                    (data["title"], data["description"], data["department_id"], date.today().isoformat(), data["deadline"],
                     session.get("current_employee_id"), data["priority"], data["critical_time"] or None, data["overdue_reason"], department_number),
                )
                task_id = cursor.lastrowid
                save_task_relations(db, task_id, data)
                initial_roles = f"Исполнители: {people_names(task_people(task_id, 'assignees'))}; наблюдатели: {people_names(task_people(task_id, 'observers'))}; согласующие: {', '.join(row['current_name'] for row in task_approver_rows(task_id))}"
                history_event(db, task_id, "initial", initial_roles, "Задача создана", query_one("SELECT full_name FROM employees WHERE id=?", (session.get("current_employee_id"),))["full_name"])
                notify_assignments(db, task_id, data["title"], data["assignees"], data["observers"])
                notify(db, [row["current_employee_id"] for row in task_approver_rows(task_id)], task_id, "approver", f"Вы назначены согласующим задачи: {data['title']}")
                db.commit()
                flash("Задача создана.", "success")
                return redirect(url_for("tasks"))
            except (ValueError, sqlite3.IntegrityError):
                flash("Проверьте корректность введённых данных.", "error")
    selected_approvers = parse_id_list(request.form.getlist("approvers")) if request.method == "POST" else [e["id"] for e in default_selected_approvers]
    positions_by_employee = {employee["id"]: query_all("SELECT * FROM employee_positions WHERE employee_id = ? ORDER BY is_primary DESC, id", (employee["id"],)) for employee in employees}
    selected_position_map = {employee_id: request.form.get(f"position_{employee_id}", type=int) for employee_id in parse_id_list(request.form.getlist("assignees"))}
    return render_template(
        "task_form.html", task=None, departments=departments, employees=employees,
        selected_assignees=parse_id_list(request.form.getlist("assignees")), selected_observers=parse_id_list(request.form.getlist("observers")),
        selected_approvers=selected_approvers, helper_departments=parse_id_list(request.form.getlist("helper_departments")),
        positions_by_employee=positions_by_employee, selected_position_map=selected_position_map, source_message=source_message,
    )


@app.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
def edit_task(task_id):
    task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        abort(404)
    if not task_is_visible(task_id):
        flash("Эта задача не относится к выбранному профилю.", "error")
        return redirect(url_for("tasks"))
    departments, employees, _default_selected_approvers = task_form_data()
    # Сохраняем уволенных участников старых задач видимыми при редактировании.
    known_ids = {employee["id"] for employee in employees}
    for role in ("assignees", "observers"):
        for person in task_people(task_id, role):
            if person["id"] not in known_ids:
                employees.append(person)
                known_ids.add(person["id"])
    previous_names = people_names(task_people(task_id, "assignees"))
    selected_assignees = [row["employee_id"] for row in query_all("SELECT employee_id FROM task_assignees WHERE task_id = ?", (task_id,))]
    selected_observers = [row["employee_id"] for row in query_all("SELECT employee_id FROM task_observers WHERE task_id = ?", (task_id,))]
    previous_assignees = selected_assignees[:]
    previous_observers = selected_observers[:]
    selected_approvers = [row["original_employee_id"] for row in task_approver_rows(task_id)]
    previous_observer_names = people_names(task_people(task_id, "observers"))
    previous_approver_names = ", ".join(row["current_name"] for row in task_approver_rows(task_id))
    previous_approvers = set(selected_approvers)
    helper_departments = [row["department_id"] for row in query_all("SELECT department_id FROM task_departments WHERE task_id = ? AND is_primary = 0", (task_id,))]
    if request.method == "POST":
        current_id = require_current_employee()
        if task["creator_id"] and task["creator_id"] != current_id and not employee_can("settings", current_id):
            flash("Редактировать параметры задачи может её автор или руководство.", "error")
            return redirect(url_for("edit_task", task_id=task_id))
        data = parse_task_form()
        error = validate_task_form(data, task_id)
        selected_assignees, selected_observers, selected_approvers, helper_departments = data["assignees"], data["observers"], data["approvers"], data["helper_departments"]
        if error:
            flash(error, "error")
        else:
            try:
                db = get_db()
                department_number = task["department_number"]
                if task["department_id"] != data["department_id"]:
                    db.execute("INSERT OR IGNORE INTO department_task_sequences (department_id,last_number) VALUES (?,0)", (data["department_id"],))
                    db.execute("UPDATE department_task_sequences SET last_number=last_number+1 WHERE department_id=?", (data["department_id"],))
                    department_number = db.execute("SELECT last_number FROM department_task_sequences WHERE department_id=?", (data["department_id"],)).fetchone()[0]
                db.execute(
                    """UPDATE tasks SET title = ?, description = ?, department_id = ?, deadline = ?, priority = ?,
                       critical_time = ?, overdue_reason = ?, department_number=? WHERE id = ?""",
                    (data["title"], data["description"], data["department_id"], data["deadline"], data["priority"],
                     data["critical_time"] or None, data["overdue_reason"], department_number, task_id),
                )
                save_task_relations(db, task_id, data, replace=True)
                changed_fields = []
                for label, old_value, new_value in (
                    ("Название", task["title"], data["title"]), ("ТЗ", task["description"], data["description"]),
                    ("Приоритет", task["priority"], data["priority"]), ("Дедлайн", task["deadline"], data["deadline"]),
                    ("Отдел", task["department_id"], data["department_id"]),
                ):
                    if old_value != new_value:
                        changed_fields.append(f"{label}: {old_value or '—'} → {new_value or '—'}")
                if changed_fields:
                    actor = query_one("SELECT full_name FROM employees WHERE id=?", (current_id,))["full_name"]
                    history_event(db, task_id, "task-edit", "; ".join(changed_fields), f"Изменил {actor}")
                if set(previous_assignees) != set(selected_assignees):
                    history_event(db, task_id, "reassign", people_names(task_people(task_id, "assignees")), "Изменено при редактировании задачи", previous_names)
                if set(previous_observers) != set(selected_observers):
                    history_event(db, task_id, "observers", people_names(task_people(task_id, "observers")), "Изменены наблюдатели", previous_observer_names)
                if previous_approvers != set(selected_approvers):
                    history_event(db, task_id, "approvers", ", ".join(row["current_name"] for row in task_approver_rows(task_id)), "Изменены согласующие", previous_approver_names)
                notify(db, set(selected_assignees) - set(previous_assignees), task_id, f"assigned-edit-{uuid.uuid4().hex}", f"Вам назначена задача: {data['title']}")
                notify(db, set(selected_observers) - set(previous_observers), task_id, f"observing-edit-{uuid.uuid4().hex}", f"Вас добавили в наблюдатели задачи: {data['title']}")
                notify(db, set(selected_approvers) - previous_approvers, task_id, f"approver-edit-{uuid.uuid4().hex}", f"Вы назначены согласующим задачи: {data['title']}")
                notify(db, set(previous_assignees) - set(selected_assignees), task_id, f"assigned-removed-{uuid.uuid4().hex}", f"Вы больше не являетесь исполнителем задачи: {data['title']}")
                notify(db, set(previous_observers) - set(selected_observers), task_id, f"observer-removed-{uuid.uuid4().hex}", f"Вы больше не являетесь наблюдателем задачи: {data['title']}")
                notify(db, previous_approvers - set(selected_approvers), task_id, f"approver-removed-{uuid.uuid4().hex}", f"Вы больше не являетесь согласующим задачи: {data['title']}")
                if task["deadline"] != data["deadline"]:
                    notify(db, task_participant_ids(task_id), task_id, f"deadline-{uuid.uuid4().hex}", f"Срок задачи «{data['title']}» изменён: {ru_datetime(data['deadline'])}")
                db.commit()
                flash("Задача обновлена.", "success")
                return redirect(url_for("tasks" if task["status"] == "В работе" else "completed_tasks"))
            except (ValueError, sqlite3.IntegrityError):
                flash("Проверьте корректность введённых данных.", "error")
        task = dict(task)
        task.update(title=data["title"], description=data["description"], department_id=data["department_id"], deadline=data["deadline"], priority=data["priority"], critical_time=data["critical_time"], overdue_reason=data["overdue_reason"])
    history = query_all("SELECT * FROM task_history WHERE task_id = ? ORDER BY id", (task_id,))
    comments = query_all("SELECT tc.*, e.full_name, e.photo FROM task_comments tc JOIN employees e ON e.id = tc.author_id WHERE task_id = ? ORDER BY tc.id", (task_id,))
    files = query_all("SELECT tf.*, e.full_name FROM task_files tf JOIN employees e ON e.id = tf.author_id WHERE task_id = ? ORDER BY tf.id DESC", (task_id,))
    substitutions = query_all("""SELECT ts.*, o.full_name AS original_name, r.full_name AS replacement_name FROM task_substitutions ts
        JOIN employees o ON o.id=ts.original_employee_id JOIN employees r ON r.id=ts.replacement_employee_id WHERE task_id=? ORDER BY ts.id""", (task_id,))
    positions_by_employee = {employee["id"]: query_all("SELECT * FROM employee_positions WHERE employee_id = ? ORDER BY is_primary DESC, id", (employee["id"],)) for employee in employees}
    selected_position_map = {row["employee_id"]: row["employee_position_id"] for row in query_all("SELECT * FROM task_positions WHERE task_id=?", (task_id,))}
    creator = query_one("SELECT id,full_name,position FROM employees WHERE id=?", (task["creator_id"],)) if task["creator_id"] else None
    participant_positions = query_all("""SELECT e.id,e.full_name,COALESCE(ep.position_name,e.position) AS position_name,
        COALESCE(ep.rate,1.0) AS rate,COALESCE(ep.project,'') AS project FROM task_assignees ta
        JOIN employees e ON e.id=ta.employee_id LEFT JOIN task_positions tp ON tp.task_id=ta.task_id AND tp.employee_id=e.id
        LEFT JOIN employee_positions ep ON ep.id=tp.employee_position_id WHERE ta.task_id=? ORDER BY e.full_name""", (task_id,))
    current_id = session.get("current_employee_id")
    current_employee = query_one("SELECT * FROM employees WHERE id=?", (current_id,)) if current_id else None
    current_approval = next((row for row in task_approver_rows(task_id) if row["current_employee_id"] == current_id), None)
    is_assignee = current_id in selected_assignees
    is_observer = current_id in selected_observers or bool(current_employee and current_employee["portal_role"] == "head" and query_one("SELECT 1 FROM task_departments WHERE task_id=? AND department_id=?", (task_id, current_employee["department_id"])))
    can_edit_task = bool(current_employee and (task["creator_id"] == current_id or employee_can("settings", current_id)))
    is_global_approver = bool(current_id and query_one("SELECT 1 FROM task_approvers WHERE current_employee_id=? LIMIT 1", (current_id,)))
    can_view_task_kitchen = bool(current_employee and (current_employee["portal_role"] in LEADERSHIP_ROLES or (is_global_approver and not is_assignee and not is_observer)))
    latest_revision = query_one("SELECT * FROM task_history WHERE task_id=? AND event='revision' ORDER BY id DESC LIMIT 1", (task_id,))
    return render_template(
        "task_form.html", task=task, departments=departments, employees=employees,
        selected_assignees=selected_assignees, selected_observers=selected_observers, selected_approvers=selected_approvers,
        helper_departments=helper_departments, history=history, current_assignees=people_names(task_people(task_id, "assignees")),
        active_assignees=active_employees(), approver_rows=task_approver_rows(task_id), comments=comments, files=files,
        substitutions=substitutions, positions_by_employee=positions_by_employee, selected_position_map=selected_position_map,
        creator=creator, participant_positions=participant_positions, source_message="", now=datetime.now().isoformat(timespec="minutes"),
        current_approval=current_approval, is_assignee=is_assignee, is_observer=is_observer,
        can_edit_task=can_edit_task, can_view_task_kitchen=can_view_task_kitchen, latest_revision=latest_revision,
        is_global_approver=is_global_approver,
        history_labels=TASK_HISTORY_LABELS,
        visible_comments=[row for row in comments if row["author_id"] == current_id],
        visible_files=[row for row in files if row["author_id"] == current_id],
    )


@app.post("/tasks/<int:task_id>/reassign")
def reassign_task(task_id):
    task = query_one("SELECT * FROM tasks WHERE id = ? AND status = 'В работе'", (task_id,))
    if not task:
        abort(404)
    current_id = require_current_employee()
    if task["creator_id"] and task["creator_id"] != current_id and not employee_can("settings", current_id):
        flash("Менять исполнителей может автор задачи или руководство.", "error")
        return redirect(url_for("edit_task", task_id=task_id))
    assignees = set(parse_id_list(request.form.getlist("assignees")))
    reason = request.form.get("reason", "").strip()
    if not assignees or not reason or any(not query_one("SELECT 1 FROM employees WHERE id = ? AND is_dismissed = 0", (item,)) for item in assignees):
        flash("Выберите действующего исполнителя и укажите причину переназначения.", "error")
        return redirect(url_for("edit_task", task_id=task_id))
    previous = task_people(task_id, "assignees")
    previous_ids = {person["id"] for person in previous}
    db = get_db()
    db.execute("DELETE FROM task_assignees WHERE task_id = ?", (task_id,))
    db.executemany("INSERT INTO task_assignees VALUES (?, ?)", [(task_id, item) for item in assignees])
    db.execute(
        "INSERT INTO task_history (task_id, event, previous_names, new_names, reason, created_at) VALUES (?, 'reassign', ?, ?, ?, ?)",
        (task_id, people_names(previous), people_names(task_people(task_id, "assignees")), reason, datetime.now().isoformat(timespec="minutes")),
    )
    notify(db, assignees - previous_ids, task_id, f"assigned-reassign-{uuid.uuid4().hex}", f"Вам переназначена задача: {task['title']}")
    db.commit()
    flash("Исполнители переназначены. Причина записана в историю.", "success")
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/replace-participant")
def replace_task_participant(task_id):
    task = query_one("SELECT * FROM tasks WHERE id=? AND status='В работе'", (task_id,))
    if not task:
        abort(404)
    current_id = require_current_employee()
    employee = query_one("SELECT * FROM employees WHERE id=?", (current_id,)) if current_id else None
    is_approver = bool(current_id and query_one("SELECT 1 FROM task_approvers WHERE current_employee_id=? LIMIT 1", (current_id,)))
    if not employee or not (is_approver or employee["portal_role"] in LEADERSHIP_ROLES or employee_can("settings", current_id)):
        flash("Ручную замену может выполнить согласователь или руководство.", "error")
        return redirect(url_for("edit_task", task_id=task_id))
    role_type = request.form.get("role_type", "")
    original_id = request.form.get("original_employee_id", type=int)
    replacement_id = request.form.get("replacement_employee_id", type=int)
    reason = request.form.get("reason", "").strip()
    replacement = query_one("SELECT * FROM employees WHERE id=? AND is_dismissed=0", (replacement_id,)) if replacement_id else None
    original = query_one("SELECT * FROM employees WHERE id=?", (original_id,)) if original_id else None
    if role_type not in {"assignee", "observer", "approver"} or not original or not replacement or original_id == replacement_id or is_absence_status(replacement["presence_status"]) or not reason:
        flash("Выберите роль, действующего доступного сотрудника и укажите причину замены.", "error")
        return redirect(url_for("edit_task", task_id=task_id))
    db = get_db()
    if role_type == "assignee":
        if not query_one("SELECT 1 FROM task_assignees WHERE task_id=? AND employee_id=?", (task_id, original_id)):
            flash("Выбранный сотрудник не является исполнителем этой задачи.", "error")
            return redirect(url_for("edit_task", task_id=task_id))
        db.execute("DELETE FROM task_assignees WHERE task_id=? AND employee_id=?", (task_id, original_id))
        db.execute("DELETE FROM task_positions WHERE task_id=? AND employee_id=?", (task_id, original_id))
        db.execute("INSERT OR IGNORE INTO task_assignees VALUES (?,?)", (task_id, replacement_id))
        position = query_one("SELECT id FROM employee_positions WHERE employee_id=? ORDER BY is_primary DESC,id LIMIT 1", (replacement_id,))
        if position:
            db.execute("INSERT OR REPLACE INTO task_positions VALUES (?,?,?)", (task_id, replacement_id, position["id"]))
    elif role_type == "observer":
        if not query_one("SELECT 1 FROM task_observers WHERE task_id=? AND employee_id=?", (task_id, original_id)):
            flash("Выбранный сотрудник не является наблюдателем этой задачи.", "error")
            return redirect(url_for("edit_task", task_id=task_id))
        db.execute("DELETE FROM task_observers WHERE task_id=? AND employee_id=?", (task_id, original_id))
        db.execute("INSERT OR IGNORE INTO task_observers VALUES (?,?)", (task_id, replacement_id))
    else:
        approval = query_one("SELECT * FROM task_approvers WHERE task_id=? AND current_employee_id=?", (task_id, original_id))
        conflict = query_one("SELECT 1 FROM task_approvers WHERE task_id=? AND current_employee_id=?", (task_id, replacement_id))
        if not approval or conflict:
            flash("Выбранного согласующего нельзя заменить этим сотрудником.", "error")
            return redirect(url_for("edit_task", task_id=task_id))
        db.execute("UPDATE task_approvers SET current_employee_id=?,decision='Ожидает',comment='',decided_at=NULL WHERE id=?", (replacement_id, approval["id"]))
    now = datetime.now().isoformat(timespec="minutes")
    db.execute("""INSERT INTO task_substitutions
        (task_id,role_type,original_employee_id,replacement_employee_id,absence_status,start_date,end_date,is_active,created_at,ended_at,source,reason)
        VALUES (?,?,?,?,?,?,?,0,?,?, 'manual',?)""", (task_id, role_type, original_id, replacement_id, "Ручная замена", date.today().isoformat(), date.today().isoformat(), now, now, reason))
    role_label = {"assignee": "исполнитель", "observer": "наблюдатель", "approver": "согласующий"}[role_type]
    text = f"{employee['full_name']} вручную заменил роль «{role_label}»: {original['full_name']} → {replacement['full_name']}. Причина: {reason}"
    history_event(db, task_id, "substitution", replacement["full_name"], text, original["full_name"])
    notify(db, task_participant_ids(task_id) | {original_id, replacement_id}, task_id, f"manual-substitution-{uuid.uuid4().hex}", text, url_for("edit_task", task_id=task_id))
    db.commit()
    flash("Замена выполнена. Все участники уведомлены, действие записано в историю.", "success")
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/complete")
def complete_task(task_id):
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("tasks"))
    allowed = query_one("SELECT 1 FROM task_assignees WHERE task_id = ? AND employee_id = ?", (task_id, employee_id))
    if not allowed:
        flash("Отправить работу на проверку может только исполнитель.", "error")
    else:
        task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not task or task["status"] == "Выполнено":
            return redirect(url_for("completed_tasks"))
        if task["workflow_status"] not in {"В работе", "На доработке"}:
            flash("Сначала переведите задачу в работу.", "error")
            return redirect(url_for("edit_task", task_id=task_id))
        reason = request.form.get("overdue_reason", "").strip()
        result_comment = request.form.get("result_comment", "").strip()
        if datetime.fromisoformat(task["deadline"]) < datetime.now() and not (reason or task["overdue_reason"]):
            flash("Для просроченной задачи укажите причину перед отправкой на проверку.", "error")
            return redirect(url_for("edit_task", task_id=task_id))
        approvers = task_approver_rows(task_id)
        if len(approvers) != 2:
            flash("В задаче должны быть указаны два согласующих.", "error")
            return redirect(url_for("edit_task", task_id=task_id))
        db = get_db()
        if result_comment:
            db.execute("INSERT INTO task_comments (task_id,author_id,text,created_at) VALUES (?,?,?,?)", (task_id, employee_id, result_comment, datetime.now().isoformat(timespec="minutes")))
            actor = query_one("SELECT full_name FROM employees WHERE id=?", (employee_id,))["full_name"]
            history_event(db, task_id, "result", result_comment, f"Результат отправил {actor}")
        try:
            uploaded_name = store_task_file(db, task_id, employee_id, request.files.get("attachment"))
        except ValueError as error:
            db.rollback()
            flash(str(error), "error")
            return redirect(url_for("edit_task", task_id=task_id))
        db.execute("UPDATE tasks SET workflow_status = 'На проверке', overdue_reason = ? WHERE id = ?", (reason or task["overdue_reason"], task_id))
        db.execute("UPDATE task_approvers SET decision = 'Ожидает', comment = '', decided_at = NULL WHERE task_id = ?", (task_id,))
        notify(db, [row["current_employee_id"] for row in approvers], task_id, f"review-{uuid.uuid4().hex}", f"Задача «{task['title']}» ожидает вашего решения")
        notify(db, [person["id"] for person in task_people(task_id, "observers")], task_id, f"submitted-{uuid.uuid4().hex}", f"Исполнитель отправил задачу «{task['title']}» на проверку")
        result_details = result_comment or "Без комментария"
        if uploaded_name:
            result_details += f"; файл: {uploaded_name}"
        history_event(db, task_id, "review", "На проверке", f"Исполнитель отправил результат: {result_details}")
        db.commit()
        flash("Задача отправлена двум согласующим на проверку.", "success")
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/extension-request")
def request_task_extension(task_id):
    employee_id = require_current_employee()
    task = query_one("SELECT * FROM tasks WHERE id=? AND status='В работе'", (task_id,))
    if not task:
        abort(404)
    if not employee_id or not query_one("SELECT 1 FROM task_assignees WHERE task_id=? AND employee_id=?", (task_id, employee_id)):
        flash("Запросить продление может только исполнитель задачи.", "error")
        return redirect(url_for("edit_task", task_id=task_id))
    comment = request.form.get("comment", "").strip()
    proposed_deadline = request.form.get("proposed_deadline", "")
    try:
        proposed = datetime.fromisoformat(proposed_deadline)
        if not comment or proposed <= datetime.now():
            raise ValueError
    except ValueError:
        flash("Укажите причину и новый срок в будущем.", "error")
        return redirect(url_for("edit_task", task_id=task_id))
    actor = query_one("SELECT full_name FROM employees WHERE id=?", (employee_id,))
    recipients = {person["id"] for person in task_people(task_id, "observers")}
    recipients.update(row["current_employee_id"] for row in task_approver_rows(task_id))
    text = f"{actor['full_name']} просит продлить задачу «{task['title']}» до {ru_datetime(proposed_deadline)}. Причина: {comment}"
    db = get_db()
    history_event(db, task_id, "extension-request", proposed_deadline, comment, task["deadline"])
    notify(db, recipients, task_id, f"extension-request-{uuid.uuid4().hex}", text)
    db.commit()
    flash("Запрос продления отправлен наблюдателям и согласующим.", "success")
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/deadline")
def change_task_deadline(task_id):
    employee_id = require_current_employee()
    task = query_one("SELECT * FROM tasks WHERE id=? AND status='В работе'", (task_id,))
    employee = query_one("SELECT * FROM employees WHERE id=?", (employee_id,)) if employee_id else None
    approval = query_one("SELECT 1 FROM task_approvers WHERE current_employee_id=? LIMIT 1", (employee_id,)) if employee_id else None
    if not task:
        abort(404)
    if not employee or (not approval and employee["portal_role"] not in LEADERSHIP_ROLES):
        flash("Изменить дедлайн может только согласующий.", "error")
        return redirect(url_for("edit_task", task_id=task_id))
    deadline = request.form.get("deadline", "")
    reason = request.form.get("reason", "").strip()
    try:
        datetime.fromisoformat(deadline)
        if not reason:
            raise ValueError
    except ValueError:
        flash("Укажите новый дедлайн и причину изменения.", "error")
        return redirect(url_for("edit_task", task_id=task_id))
    db = get_db()
    db.execute("UPDATE tasks SET deadline=? WHERE id=?", (deadline, task_id))
    history_event(db, task_id, "deadline", deadline, f"{employee['full_name']}: {reason}", task["deadline"])
    notify(db, task_participant_ids(task_id) - {employee_id}, task_id, f"deadline-{uuid.uuid4().hex}", f"Срок задачи «{task['title']}» изменён до {ru_datetime(deadline)}. Причина: {reason}")
    db.commit()
    flash("Новый дедлайн сохранён, участники уведомлены.", "success")
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/close")
def close_task(task_id):
    employee_id = require_current_employee()
    task = query_one("SELECT * FROM tasks WHERE id=? AND status='В работе'", (task_id,))
    approval = query_one("SELECT * FROM task_approvers WHERE task_id=? AND current_employee_id=?", (task_id, employee_id)) if employee_id else None
    if not task:
        abort(404)
    if not approval or task["workflow_status"] != "На проверке":
        flash("Закрыть задачу может назначенный согласующий после отправки на проверку.", "error")
        return redirect(url_for("edit_task", task_id=task_id))
    actor = query_one("SELECT full_name FROM employees WHERE id=?", (employee_id,))
    now = datetime.now().isoformat(timespec="minutes")
    db = get_db()
    db.execute("UPDATE task_approvers SET decision='Согласовано', decided_at=? WHERE id=?", (now, approval["id"]))
    db.execute("UPDATE tasks SET status='Выполнено', workflow_status='Выполнено', completed_at=? WHERE id=?", (now, task_id))
    departments = [row["department_id"] for row in query_all("SELECT department_id FROM task_departments WHERE task_id=?", (task_id,))]
    recipients = task_participant_ids(task_id) | set(department_heads(departments))
    history_event(db, task_id, "closed", "Выполнено", f"Задачу закрыл согласующий {actor['full_name']}", task["workflow_status"])
    notify(db, recipients, task_id, f"completed-{uuid.uuid4().hex}", f"Ваша задача «{task['title']}» закрыта согласующим {actor['full_name']} и перенесена в выполненные")
    db.commit()
    flash("Задача закрыта и перенесена в выполненные.", "success")
    return redirect(url_for("completed_tasks"))


@app.post("/tasks/<int:task_id>/start")
def start_task(task_id):
    employee_id = require_current_employee()
    task = query_one("SELECT * FROM tasks WHERE id=? AND status='В работе'", (task_id,))
    allowed = query_one("SELECT 1 FROM task_assignees WHERE task_id=? AND employee_id=?", (task_id, employee_id)) if employee_id else None
    if not task or not allowed:
        flash("Начать задачу может назначенный исполнитель.", "error")
    elif task["workflow_status"] == "Новая":
        db = get_db()
        db.execute("UPDATE tasks SET workflow_status='В работе' WHERE id=?", (task_id,))
        actor = query_one("SELECT full_name FROM employees WHERE id=?", (employee_id,))
        history_event(db, task_id, "status", "В работе", actor["full_name"], "Новая")
        db.commit()
        flash("Задача переведена в работу.", "success")
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/delete")
def delete_task(task_id):
    employee_id = require_current_employee()
    task = query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        abort(404)
    if not employee_id or task["creator_id"] != employee_id or task["workflow_status"] != "Новая":
        flash("Удалить можно только свою новую задачу до начала работы.", "error")
    else:
        get_db().execute("DELETE FROM tasks WHERE id=?", (task_id,))
        get_db().commit()
        flash("Новая задача удалена.", "success")
        return redirect(url_for("tasks"))
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/approve")
def approve_task(task_id):
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("profile"))
    task = query_one("SELECT * FROM tasks WHERE id = ? AND status = 'В работе' AND workflow_status = 'На проверке'", (task_id,))
    approval = query_one("SELECT * FROM task_approvers WHERE task_id = ? AND current_employee_id = ?", (task_id, employee_id))
    if not task or not approval:
        flash("Согласовать эту задачу может только назначенный согласующий на этапе проверки.", "error")
        return redirect(url_for("edit_task", task_id=task_id))
    db = get_db()
    now = datetime.now().isoformat(timespec="minutes")
    db.execute("UPDATE task_approvers SET decision = 'Согласовано', comment = '', decided_at = ? WHERE id = ?", (now, approval["id"]))
    actor = query_one("SELECT full_name FROM employees WHERE id = ?", (employee_id,))
    history_event(db, task_id, "approved", actor["full_name"], "Согласовано")
    pending = query_one("SELECT COUNT(*) AS total FROM task_approvers WHERE task_id = ? AND decision != 'Согласовано'", (task_id,))["total"]
    if pending == 0:
        db.execute("UPDATE tasks SET status = 'Выполнено', workflow_status = 'Выполнено', completed_at = ? WHERE id = ?", (now, task_id))
        departments = [row["department_id"] for row in query_all("SELECT department_id FROM task_departments WHERE task_id = ?", (task_id,))]
        recipients = task_participant_ids(task_id) | set(department_heads(departments))
        notify(db, recipients, task_id, f"completed-{uuid.uuid4().hex}", f"Ваша задача «{task['title']}» согласована, закрыта и перенесена в выполненные")
        history_event(db, task_id, "completed", "Выполнено", "Оба согласующих приняли задачу")
        flash("Оба согласующих приняли задачу. Она перемещена в выполненные.", "success")
    else:
        others = [row["current_employee_id"] for row in task_approver_rows(task_id) if row["id"] != approval["id"]]
        notify(db, others, task_id, f"approval-peer-{uuid.uuid4().hex}", f"Второй согласующий ({actor['full_name']}) принял решение по задаче «{task['title']}»: согласовано")
        flash("Ваше согласование сохранено. Ожидается решение второго согласующего.", "success")
    db.commit()
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/revision")
def revise_task(task_id):
    employee_id = require_current_employee()
    task = query_one("SELECT * FROM tasks WHERE id = ? AND status = 'В работе' AND workflow_status = 'На проверке'", (task_id,))
    approval = query_one("SELECT * FROM task_approvers WHERE task_id = ? AND current_employee_id = ?", (task_id, employee_id)) if employee_id else None
    comment = request.form.get("comment", "").strip()
    deadline = request.form.get("new_deadline", "")
    if not task or not approval:
        flash("Отправить задачу на доработку может только назначенный согласующий.", "error")
    elif not comment or not deadline:
        flash("Комментарий и новый дедлайн обязательны для доработки.", "error")
    else:
        try:
            datetime.fromisoformat(deadline)
            db = get_db()
            now = datetime.now().isoformat(timespec="minutes")
            db.execute("UPDATE task_approvers SET decision = 'На доработку', comment = ?, decided_at = ? WHERE id = ?", (comment, now, approval["id"]))
            db.execute("UPDATE tasks SET workflow_status = 'На доработке', deadline = ? WHERE id = ?", (deadline, task_id))
            actor = query_one("SELECT full_name FROM employees WHERE id = ?", (employee_id,))
            text = f"Задача «{task['title']}» отправлена на доработку. Комментарий: {comment}. Новый дедлайн: {ru_datetime(deadline)}."
            departments = [row["department_id"] for row in query_all("SELECT department_id FROM task_departments WHERE task_id = ?", (task_id,))]
            recipients = task_participant_ids(task_id) | set(department_heads(departments))
            notify(db, recipients - {employee_id}, task_id, f"revision-{uuid.uuid4().hex}", text)
            history_event(db, task_id, "revision", deadline, f"{actor['full_name']}: {comment}", task["deadline"])
            db.commit()
            flash("Задача возвращена на доработку, участники и руководители уведомлены.", "success")
        except ValueError:
            flash("Укажите корректный новый дедлайн.", "error")
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/notify-assignees")
def notify_task_assignees(task_id):
    employee_id = require_current_employee()
    employee = query_one("SELECT * FROM employees WHERE id = ?", (employee_id,)) if employee_id else None
    task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    departments = {row["department_id"] for row in query_all("SELECT department_id FROM task_departments WHERE task_id = ?", (task_id,))}
    if not employee or not task or employee["portal_role"] != "head" or employee["department_id"] not in departments:
        flash("Сообщить исполнителям может руководитель участвующего отдела.", "error")
    else:
        latest = query_one("SELECT reason FROM task_history WHERE task_id = ? AND event = 'revision' ORDER BY id DESC", (task_id,))
        text = f"{employee['full_name']} сообщает: задача «{task['title']}» требует доработки. {latest['reason'] if latest else ''}"
        notify(get_db(), [person["id"] for person in task_people(task_id, "assignees")], task_id, f"head-reminder-{uuid.uuid4().hex}", text)
        get_db().commit()
        flash("Исполнители уведомлены от вашего имени.", "success")
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/comment")
def add_task_comment(task_id):
    employee_id = require_current_employee()
    text = request.form.get("text", "").strip()
    if employee_id and task_is_visible(task_id, employee_id) and text and query_one("SELECT 1 FROM tasks WHERE id = ?", (task_id,)):
        db = get_db()
        db.execute("INSERT INTO task_comments (task_id, author_id, text, created_at) VALUES (?, ?, ?, ?)", (task_id, employee_id, text, datetime.now().isoformat(timespec="minutes")))
        actor = query_one("SELECT full_name FROM employees WHERE id = ?", (employee_id,))["full_name"]
        history_event(db, task_id, "comment", text, actor)
        notify(db, task_participant_ids(task_id) - {employee_id}, task_id, f"comment-{uuid.uuid4().hex}", f"Новый комментарий к задаче: {text[:160]}", url_for("edit_task", task_id=task_id))
        db.commit()
    else:
        flash("Комментарий не может быть пустым.", "error")
    return redirect(url_for("edit_task", task_id=task_id))


@app.post("/tasks/<int:task_id>/file")
def add_task_file(task_id):
    employee_id = require_current_employee()
    if not employee_id or not task_is_visible(task_id, employee_id) or not query_one("SELECT 1 FROM tasks WHERE id = ?", (task_id,)):
        abort(404)
    try:
        db = get_db()
        uploaded_name = store_task_file(db, task_id, employee_id, request.files.get("attachment"))
        if not uploaded_name:
            raise ValueError("Выберите файл для загрузки.")
        notify(db, task_participant_ids(task_id) - {employee_id}, task_id, f"file-{uuid.uuid4().hex}", f"В задачу добавлен файл: {uploaded_name}", url_for("edit_task", task_id=task_id))
        db.commit()
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("edit_task", task_id=task_id))


@app.route("/employees")
def employees():
    rows = query_all(
        """SELECT e.*, d.name AS department_name FROM employees e
           JOIN departments d ON d.id = e.department_id WHERE e.is_dismissed = 0 ORDER BY e.full_name"""
    )
    grouped = [(department, [employee for employee in rows if employee["department_id"] == department["id"]]) for department in catalog_rows("departments")]
    return render_template("employees.html", employees=rows, grouped_employees=grouped, presence_statuses=catalog_rows("statuses"), departments=catalog_rows("departments"), positions=catalog_rows("positions"))


def employee_export_response(rows, filename, document_format=False):
    if document_format:
        from docx import Document
        document = Document()
        document.add_heading("ПИОНЕР. Сотрудники", 0)
        table = document.add_table(rows=1, cols=8)
        table.style = "Table Grid"
        for cell, title in zip(table.rows[0].cells, ["ФИО", "Email", "Отдел", "Должности и ставки", "Статус", "Дата рождения", "Дата увольнения", "Причина увольнения"]):
            cell.text = title
        for employee in rows:
            positions = query_all("SELECT position_name,rate,project FROM employee_positions WHERE employee_id=? ORDER BY is_primary DESC,id", (employee["id"],))
            position_text = "; ".join(f"{item['position_name']} — {item['rate']} ставки" + (f" ({item['project']})" if item["project"] else "") for item in positions) or employee["position"]
            cells = table.add_row().cells
            for cell, value in zip(cells, [employee["full_name"], employee["email"] or "", employee["department_name"], position_text, employee["status_label"] or employee["presence_status"], ru_date(employee["birth_date"]), employee["dismissed_at"] or "", employee["dismissal_reason"] or ""]):
                cell.text = str(value)
        output = io.BytesIO()
        document.save(output)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": f"attachment; filename={filename}.docx"})
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Сотрудники"
    sheet.append(["ФИО", "Email", "Отдел", "Основная должность", "Все должности и ставки", "Статус", "Дата рождения", "Дата увольнения", "Причина увольнения"])
    for employee in rows:
        positions = query_all("SELECT position_name,rate,project FROM employee_positions WHERE employee_id=? ORDER BY is_primary DESC,id", (employee["id"],))
        position_text = "; ".join(f"{item['position_name']} — {item['rate']} ставки" + (f" ({item['project']})" if item["project"] else "") for item in positions)
        sheet.append([employee["full_name"], employee["email"] or "", employee["department_name"], employee["position"], position_text, employee["status_label"] or employee["presence_status"], employee["birth_date"], employee["dismissed_at"] or "", employee["dismissal_reason"] or ""])
    for cell in sheet[1]:
        cell.font = cell.font.copy(bold=True)
    sheet.freeze_panes = "A2"
    for column, width in zip("ABCDEFGHI", (32, 28, 25, 28, 55, 20, 16, 20, 35)):
        sheet.column_dimensions[column].width = width
    output = io.BytesIO()
    workbook.save(output)
    return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/employees/export")
def export_employees():
    if not require_feature("employees_admin"):
        return redirect(url_for("employees"))
    rows = query_all("""SELECT e.*,d.name AS department_name,s.label AS status_label FROM employees e
        JOIN departments d ON d.id=e.department_id LEFT JOIN presence_options s ON s.key=e.presence_status
        WHERE e.is_dismissed=0 ORDER BY e.full_name""")
    return employee_export_response(rows, "pioneer-employees", request.args.get("format") == "docx")


@app.get("/employees/<int:employee_id>")
def employee_card(employee_id):
    employee = query_one("""SELECT e.*, d.name AS department_name, s.label AS status_label, s.color AS status_color
        FROM employees e JOIN departments d ON d.id=e.department_id LEFT JOIN presence_options s ON s.key=e.presence_status
        WHERE e.id=?""", (employee_id,))
    if not employee:
        abort(404)
    positions = query_all("SELECT * FROM employee_positions WHERE employee_id=? ORDER BY is_primary DESC,id", (employee_id,))
    department_head = query_one("""SELECT * FROM employees WHERE department_id=? AND portal_role='head' AND is_dismissed=0 ORDER BY id LIMIT 1""", (employee["department_id"],))
    personal_substitute = query_one("SELECT * FROM employees WHERE id=? AND is_dismissed=0", (employee["substitute_id"],)) if employee["substitute_id"] else None
    role_task_counts = {
        "assignee": query_one("""SELECT COUNT(DISTINCT t.id) AS total FROM tasks t LEFT JOIN task_assignees a ON a.task_id=t.id
            WHERE t.status='В работе' AND (a.employee_id=? OR t.creator_id=?)""", (employee_id, employee_id))["total"],
        "observer": query_one("SELECT COUNT(DISTINCT t.id) AS total FROM tasks t JOIN task_observers o ON o.task_id=t.id WHERE t.status='В работе' AND o.employee_id=?", (employee_id,))["total"],
        "approver": query_one("SELECT COUNT(DISTINCT t.id) AS total FROM tasks t JOIN task_approvers a ON a.task_id=t.id WHERE t.status='В работе' AND a.current_employee_id=?", (employee_id,))["total"],
    }
    active_task_count = query_one(
        """SELECT COUNT(DISTINCT t.id) AS total FROM tasks t
           LEFT JOIN task_assignees a ON a.task_id=t.id LEFT JOIN task_observers o ON o.task_id=t.id
           LEFT JOIN task_approvers p ON p.task_id=t.id
           WHERE t.status='В работе' AND (t.creator_id=? OR a.employee_id=? OR o.employee_id=? OR p.current_employee_id=?)""",
        (employee_id, employee_id, employee_id, employee_id),
    )["total"]
    return render_template("employee_card.html", employee=employee, positions=positions, department_head=department_head,
                           personal_substitute=personal_substitute, role_task_counts=role_task_counts,
                           active_task_count=active_task_count, replacements=[e for e in active_employees() if e["id"] != employee_id])


@app.get("/employees/archive")
def employee_archive():
    if not require_feature("employees_admin"):
        return redirect(url_for("employees"))
    q = request.args.get("q", "").strip()
    department_id = request.args.get("department_id", type=int)
    sort = request.args.get("sort", "dismissed_desc")
    conditions = ["e.is_dismissed=1"]
    params = []
    if q:
        conditions.append("(e.full_name LIKE ? OR e.position LIKE ? OR e.dismissal_reason LIKE ?)")
        params.extend([f"%{q}%"] * 3)
    if department_id:
        conditions.append("e.department_id=?")
        params.append(department_id)
    orders = {"dismissed_desc": "COALESCE(e.dismissed_at,'') DESC", "dismissed_asc": "COALESCE(e.dismissed_at,'')", "name": "e.full_name", "department": "d.name,e.full_name"}
    rows = query_all(f"""SELECT e.*, d.name AS department_name,s.label AS status_label FROM employees e JOIN departments d ON d.id=e.department_id
        LEFT JOIN presence_options s ON s.key=e.presence_status WHERE {' AND '.join(conditions)} ORDER BY {orders.get(sort, orders['dismissed_desc'])}""", params)
    return render_template("employee_archive.html", employees=rows, departments=catalog_rows("departments"), selected_department=department_id, q=q, sort=sort)


@app.get("/employees/archive/export")
def export_employee_archive():
    if not require_feature("employees_admin"):
        return redirect(url_for("employees"))
    q = request.args.get("q", "").strip()
    department_id = request.args.get("department_id", type=int)
    sort = request.args.get("sort", "dismissed_desc")
    conditions = ["e.is_dismissed=1"]
    params = []
    if q:
        conditions.append("(e.full_name LIKE ? OR e.position LIKE ? OR e.dismissal_reason LIKE ?)")
        params.extend([f"%{q}%"] * 3)
    if department_id:
        conditions.append("e.department_id=?")
        params.append(department_id)
    orders = {"dismissed_desc": "COALESCE(e.dismissed_at,'') DESC", "dismissed_asc": "COALESCE(e.dismissed_at,'')", "name": "e.full_name", "department": "d.name,e.full_name"}
    rows = query_all(f"""SELECT e.*,d.name AS department_name,s.label AS status_label FROM employees e
        JOIN departments d ON d.id=e.department_id LEFT JOIN presence_options s ON s.key=e.presence_status
        WHERE {' AND '.join(conditions)} ORDER BY {orders.get(sort, orders['dismissed_desc'])}""", params)
    return employee_export_response(rows, "pioneer-employees-archive", request.args.get("format") == "docx")


@app.post("/employees/<int:employee_id>/access-code")
def regenerate_access_code(employee_id):
    if not require_feature("employees_admin"):
        return redirect(url_for("employees"))
    employee = query_one("SELECT * FROM employees WHERE id=? AND is_dismissed=0", (employee_id,))
    if not employee:
        abort(404)
    if not employee["email"]:
        flash("Сначала укажите электронную почту сотрудника.", "error")
        return redirect(url_for("employee_form", employee_id=employee_id))
    code = generate_access_code()
    db = get_db()
    db.execute("UPDATE employees SET access_code_hash=?, access_code_display=? WHERE id=?", (generate_password_hash(code), code, employee_id))
    db.commit()
    login_url = url_for("employee_login", _external=True)
    sent = send_email(employee["email"], "Доступ в ПИОНЕР", f"Здравствуйте, {employee['full_name']}!\n\nВаш пароль: {code}\nВойти в портал: {login_url}\n", employee_id)
    flash("Новый код создан" + (" и отправлен на почту." if sent else ". SMTP не настроен: код доступен в карточке."), "success")
    return redirect(url_for("employee_card", employee_id=employee_id))


@app.post("/employees/<int:employee_id>/dismiss")
def dismiss_employee(employee_id):
    if not require_feature("employees_admin"):
        return redirect(url_for("employees"))
    employee = query_one("SELECT * FROM employees WHERE id = ? AND is_dismissed = 0", (employee_id,))
    if not employee:
        abort(404)
    fallback_id = request.form.get("replacement_id", type=int)
    replacement_ids = {
        "assignee": request.form.get("assignee_replacement_id", type=int) or fallback_id,
        "observer": request.form.get("observer_replacement_id", type=int) or fallback_id,
        "approver": request.form.get("approver_replacement_id", type=int) or fallback_id,
    }
    replacements = {
        role: query_one("SELECT * FROM employees WHERE id=? AND is_dismissed=0 AND id!=?", (replacement_id, employee_id)) if replacement_id else None
        for role, replacement_id in replacement_ids.items()
    }
    active_task_ids = [row["id"] for row in query_all(
        """SELECT DISTINCT t.id FROM tasks t LEFT JOIN task_assignees a ON a.task_id=t.id
           LEFT JOIN task_observers o ON o.task_id=t.id LEFT JOIN task_approvers p ON p.task_id=t.id
           WHERE t.status='В работе' AND (t.creator_id=? OR a.employee_id=? OR o.employee_id=? OR p.current_employee_id=?)""",
        (employee_id, employee_id, employee_id, employee_id),
    )]
    required_roles = {
        "assignee": bool(query_one("""SELECT 1 FROM tasks t LEFT JOIN task_assignees a ON a.task_id=t.id
            WHERE t.status='В работе' AND (a.employee_id=? OR t.creator_id=?) LIMIT 1""", (employee_id, employee_id))),
        "observer": bool(query_one("SELECT 1 FROM tasks t JOIN task_observers o ON o.task_id=t.id WHERE t.status='В работе' AND o.employee_id=? LIMIT 1", (employee_id,))),
        "approver": bool(query_one("SELECT 1 FROM tasks t JOIN task_approvers a ON a.task_id=t.id WHERE t.status='В работе' AND a.current_employee_id=? LIMIT 1", (employee_id,))),
    }
    if any(required_roles[role] and not replacements[role] for role in required_roles):
        flash("Перед увольнением выберите новых исполнителя, наблюдателя и согласующего для всех активных ролей.", "error")
        return redirect(url_for("employee_card", employee_id=employee_id))
    if replacements["approver"]:
        conflict = query_one("""SELECT 1 FROM task_approvers departing JOIN task_approvers existing ON existing.task_id=departing.task_id
            WHERE departing.current_employee_id=? AND existing.current_employee_id=? AND existing.id!=departing.id LIMIT 1""", (employee_id, replacements["approver"]["id"]))
        if conflict:
            flash("Выбранный согласующий уже назначен в одной из задач. Выберите другого сотрудника для передачи согласования.", "error")
            return redirect(url_for("employee_card", employee_id=employee_id))
    db = get_db()
    if active_task_ids:
        for task_id in active_task_ids:
            transferred = []
            if query_one("SELECT 1 FROM task_assignees WHERE task_id=? AND employee_id=?", (task_id, employee_id)):
                replacement = replacements["assignee"]
                db.execute("INSERT OR IGNORE INTO task_assignees (task_id,employee_id) VALUES (?,?)", (task_id, replacement["id"]))
                db.execute("DELETE FROM task_assignees WHERE task_id=? AND employee_id=?", (task_id, employee_id))
                replacement_position = query_one("SELECT id FROM employee_positions WHERE employee_id=? ORDER BY is_primary DESC,id LIMIT 1", (replacement["id"],))
                db.execute("DELETE FROM task_positions WHERE task_id=? AND employee_id=?", (task_id, employee_id))
                if replacement_position:
                    db.execute("INSERT OR REPLACE INTO task_positions VALUES (?,?,?)", (task_id, replacement["id"], replacement_position["id"]))
                transferred.append(("assignee", "исполнитель", replacement))
            if query_one("SELECT 1 FROM task_observers WHERE task_id=? AND employee_id=?", (task_id, employee_id)):
                replacement = replacements["observer"]
                db.execute("INSERT OR IGNORE INTO task_observers (task_id,employee_id) VALUES (?,?)", (task_id, replacement["id"]))
                db.execute("DELETE FROM task_observers WHERE task_id=? AND employee_id=?", (task_id, employee_id))
                transferred.append(("observer", "наблюдатель", replacement))
            approvers = query_all("SELECT id FROM task_approvers WHERE task_id=? AND current_employee_id=?", (task_id, employee_id))
            for approver in approvers:
                replacement = replacements["approver"]
                db.execute("UPDATE task_approvers SET current_employee_id=?, decision='Ожидает', comment='', decided_at=NULL WHERE id=?", (replacement["id"], approver["id"]))
                transferred.append(("approver", "согласующий", replacement))
            if query_one("SELECT 1 FROM tasks WHERE id=? AND creator_id=?", (task_id, employee_id)):
                replacement = replacements["assignee"]
                db.execute("UPDATE tasks SET creator_id=? WHERE id=?", (replacement["id"], task_id))
                transferred.append(("creator", "автор задачи", replacement))
            for role_type, role_label, replacement in transferred:
                transfer_text = f"Задача передана от {employee['full_name']} к {replacement['full_name']} (увольнение)"
                history_event(db, task_id, "dismissal-transfer", transfer_text, role_label, employee["full_name"])
                now = datetime.now().isoformat(timespec="minutes")
                db.execute("""INSERT INTO task_substitutions
                    (task_id,role_type,original_employee_id,replacement_employee_id,absence_status,start_date,end_date,is_active,created_at,ended_at,source,reason)
                    VALUES (?,?,?,?,?,?,?,0,?,?, 'dismissal',?)""", (task_id, role_type, employee_id, replacement["id"], "Увольнение", date.today().isoformat(), date.today().isoformat(), now, now, request.form.get("dismissal_reason", "").strip()))
                notify(db, {replacement["id"]} | task_participant_ids(task_id), task_id, f"dismissal-transfer-{role_type}-{employee_id}", f"{transfer_text}. Роль: {role_label}.", url_for("edit_task", task_id=task_id))
    now = datetime.now().isoformat(timespec="minutes")
    db.execute("UPDATE task_substitutions SET is_active=0, ended_at=? WHERE original_employee_id=? AND is_active=1", (now, employee_id))
    db.execute("UPDATE employees SET is_dismissed=1, dismissed_at=?, dismissal_reason=?, access_code_hash=NULL, access_code_display=NULL WHERE id=?",
               (now, request.form.get("dismissal_reason", "").strip(), employee_id))
    db.execute(
        "INSERT INTO posts (author_id, post_type, text, created_at) VALUES (?, 'Обычный', ?, ?)",
        (session.get("current_employee_id") or employee_id, f"{employee['full_name']} покинул нашу команду. Желаем успехов на новом месте!", datetime.now().isoformat(timespec="minutes")),
    )
    db.commit()
    if session.get("current_employee_id") == employee_id:
        session.clear()
    flash("Сотрудник отмечен как уволенный, объявление опубликовано.", "success")
    return redirect(url_for("employees"))


@app.post("/employees/<int:employee_id>/status")
def update_presence_status(employee_id):
    current_id = require_current_employee()
    if current_id is None:
        return redirect(url_for("employees"))
    if current_id != employee_id:
        flash("Можно менять только свой статус присутствия.", "error")
        return redirect(url_for("employees"))
    status = request.form.get("presence_status", "")
    if not valid_catalog_value("statuses", status):
        flash("Выберите статус из списка.", "error")
        return redirect(url_for("employees"))
    try:
        update_employee_presence(get_db(), employee_id, status, request.form.get("absence_start"), request.form.get("absence_end"))
        get_db().commit()
        flash("Статус обновлён.", "success")
    except ValueError as error:
        get_db().rollback()
        flash(str(error), "error")
    return redirect(url_for("employees"))


@app.route("/employees/new", methods=["GET", "POST"])
@app.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
def employee_form(employee_id=None):
    if not require_feature("employees_admin"):
        return redirect(url_for("employees"))
    first_employee = not query_one("SELECT 1 FROM employees WHERE is_dismissed=0 LIMIT 1")
    employee = query_one("SELECT * FROM employees WHERE id = ?", (employee_id,)) if employee_id else None
    if employee_id and not employee:
        abort(404)
    if employee and employee["is_dismissed"]:
        flash("Карточка уволенного сотрудника хранится в архиве и не редактируется.", "error")
        return redirect(url_for("employees"))
    departments = query_all("SELECT * FROM departments ORDER BY name")
    positions = catalog_rows("positions")
    statuses_available = bool(catalog_rows("statuses"))
    additional_positions = query_all("SELECT * FROM employee_positions WHERE employee_id = ? AND is_primary = 0 ORDER BY id", (employee_id,)) if employee_id else []
    primary_position = query_one("SELECT * FROM employee_positions WHERE employee_id=? AND is_primary=1 ORDER BY id LIMIT 1", (employee_id,)) if employee_id else None
    substitutes = active_employees()
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        department_id = request.form.get("department_id", type=int)
        position = request.form.get("position", "").strip()
        email = request.form.get("email", "").strip().casefold()
        birth_date = request.form.get("birth_date", "")
        substitute_id = request.form.get("substitute_id", type=int)
        substitute = query_one("SELECT * FROM employees WHERE id=? AND is_dismissed=0", (substitute_id,)) if substitute_id else None
        email_exists = email and query_one("SELECT 1 FROM employees WHERE lower(email)=? AND id!=?", (email, employee_id or -1))
        if not full_name or not valid_catalog_value("departments", department_id) or not valid_catalog_value("positions", position, "name") or not birth_date:
            flash("Заполните все обязательные поля.", "error")
        elif email_exists:
            flash("Эта электронная почта уже используется другим сотрудником.", "error")
        elif substitute_id and (not substitute or substitute_id == employee_id):
            flash("Подменный сотрудник должен быть действующим сотрудником и не может заменять сам себя.", "error")
        else:
            try:
                date.fromisoformat(birth_date)
                if not employee and not statuses_available:
                    flash("Добавьте первый статус присутствия в Настройках.", "error")
                    return render_template("employee_form.html", employee=employee, departments=departments, positions=positions, statuses_available=False, primary_position=primary_position, additional_positions=additional_positions, substitutes=substitutes, can_rates=employee_can("rates"))
                photo = employee["photo"] if employee else None
                uploaded = save_upload(request.files.get("photo"), IMAGE_EXTENSIONS)
                if uploaded:
                    photo = uploaded[0]
                db = get_db()
                if employee:
                    db.execute(
                        "UPDATE employees SET full_name=?, department_id=?, position=?, birth_date=?, photo=?, email=? WHERE id=?",
                        (full_name, department_id, position, birth_date, photo, email or None, employee_id),
                    )
                else:
                    available_statuses = catalog_rows("statuses")
                    db.execute(
                        "INSERT INTO employees (full_name, department_id, position, birth_date, photo, presence_status, email) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (full_name, department_id, position, birth_date, photo, available_statuses[0]["key"] if not valid_catalog_value("statuses", "offline") else "offline", email or None),
                    )
                    employee_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                    if first_employee:
                        db.execute("UPDATE employees SET portal_role='admin' WHERE id=?", (employee_id,))
                        session["current_employee_id"] = employee_id
                current_role = employee["portal_role"] if employee else ("admin" if first_employee else "employee")
                if current_role not in LEADERSHIP_ROLES and current_role != "room_admin":
                    new_role = "head" if request.form.get("is_department_head") == "1" else "employee"
                    if new_role == "head":
                        db.execute("UPDATE employees SET portal_role='employee' WHERE department_id=? AND portal_role='head' AND id!=?", (department_id, employee_id))
                    db.execute("UPDATE employees SET portal_role=? WHERE id=?", (new_role, employee_id))
                db.execute("DELETE FROM employee_positions WHERE employee_id = ?", (employee_id,))
                db.execute("INSERT INTO employee_positions (employee_id, position_name, rate, project, is_primary) VALUES (?, ?, ?, ?, 1)", (employee_id, position, request.form.get("primary_rate", type=float) or 1.0, request.form.get("primary_project", "").strip()))
                if employee_can("rates"):
                    names = request.form.getlist("additional_position")
                    rates = request.form.getlist("additional_rate")
                    projects = request.form.getlist("additional_project")
                    for index, name in enumerate(names):
                        name = name.strip()
                        if name and valid_catalog_value("positions", name, "name"):
                            rate = float(rates[index]) if index < len(rates) and rates[index] else 1.0
                            db.execute("INSERT INTO employee_positions (employee_id, position_name, rate, project, is_primary) VALUES (?, ?, ?, ?, 0)", (employee_id, name, rate, projects[index].strip() if index < len(projects) else ""))
                db.execute("UPDATE employees SET substitute_id = ?, free_slots = ? WHERE id = ?", (substitute_id, request.form.get("free_slots", "").strip(), employee_id))
                new_access_code = None
                if email and (not employee or not employee["access_code_hash"]):
                    new_access_code = generate_access_code()
                    db.execute("UPDATE employees SET access_code_hash=?, access_code_display=? WHERE id=?", (generate_password_hash(new_access_code), new_access_code, employee_id))
                db.commit()
                if new_access_code:
                    login_url = url_for("employee_login", _external=True)
                    sent = send_email(email, "Доступ в ПИОНЕР", f"Здравствуйте, {full_name}!\n\nВаш пароль: {new_access_code}\nВойти в портал: {login_url}\n", employee_id)
                    flash("Данные сохранены. Код доступа создан" + (" и отправлен на почту." if sent else " и показан в карточке сотрудника."), "success")
                else:
                    flash("Данные сотрудника сохранены.", "success")
                return redirect(url_for("employees"))
            except (ValueError, sqlite3.IntegrityError) as error:
                flash(str(error) if isinstance(error, ValueError) else "Не удалось сохранить сотрудника.", "error")
    return render_template("employee_form.html", employee=employee, departments=departments, positions=positions, statuses_available=statuses_available, primary_position=primary_position, additional_positions=additional_positions, substitutes=substitutes, can_rates=employee_can("rates"))


def load_posts(limit=None):
    sql = """
        SELECT p.*, e.full_name AS author_name, e.photo AS author_photo, COALESCE(pt.label, p.post_type) AS type_label,
               subject.full_name AS subject_name, subject.photo AS subject_photo,
               d.name AS audience_name,
               (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) AS like_count
        FROM posts p
        JOIN employees e ON e.id = p.author_id
        LEFT JOIN post_types pt ON pt.key = p.post_type
        LEFT JOIN employees subject ON subject.id = p.subject_employee_id
        LEFT JOIN departments d ON d.id = p.audience_department_id
        ORDER BY p.created_at DESC
    """
    if limit:
        sql += " LIMIT ?"
        posts = query_all(sql, (limit,))
    else:
        posts = query_all(sql)
    result = []
    current_id = session.get("current_employee_id", -1)
    viewer = query_one("SELECT * FROM employees WHERE id=? AND is_dismissed=0", (current_id,)) if current_id != -1 else None
    for post in posts:
        if post["audience_department_id"] and (not viewer or (viewer["department_id"] != post["audience_department_id"] and viewer["portal_role"] not in LEADERSHIP_ROLES)):
            continue
        reaction_rows = query_all("SELECT reaction,COUNT(*) AS total FROM post_reactions WHERE post_id=? GROUP BY reaction", (post["id"],))
        comments = query_all(
            """SELECT c.*, e.full_name AS author_name FROM comments c
               JOIN employees e ON e.id = c.author_id WHERE c.post_id = ? ORDER BY c.created_at""",
            (post["id"],),
        )
        result.append(
            {
                "post": post,
                "attachments": query_all("SELECT * FROM post_attachments WHERE post_id = ?", (post["id"],)),
                "comments": comments,
                "comment_attachments": {comment["id"]: query_all("SELECT * FROM comment_attachments WHERE comment_id=?", (comment["id"],)) for comment in comments},
                "liked": bool(query_one("SELECT 1 FROM likes WHERE post_id = ? AND employee_id = ?", (post["id"], current_id))),
                "reactions": {row["reaction"]: row["total"] for row in reaction_rows},
                "my_reaction": (query_one("SELECT reaction FROM post_reactions WHERE post_id=? AND employee_id=?", (post["id"], current_id)) or {"reaction": ""})["reaction"],
                "can_manage": bool(viewer and (post["author_id"] == viewer["id"] or employee_can("news", viewer["id"]))),
            }
        )
    return result


@app.route("/feed")
def feed():
    mode = request.args.get("filter", "all")
    posts = load_posts()
    current_id = session.get("current_employee_id")
    if mode == "important":
        posts = [item for item in posts if item["post"]["post_type"] == "Важное объявление"]
    elif mode == "mine":
        posts = [item for item in posts if current_id and item["post"]["author_id"] == current_id]
    return render_template("feed.html", posts=posts, selected_filter=mode)


@app.route("/feed/new", methods=["GET", "POST"])
def new_post():
    if not require_feature("news"):
        return redirect(url_for("feed"))
    employees = active_employees()
    departments = query_all("SELECT * FROM departments ORDER BY name")
    post_types = catalog_rows("post-types")
    if request.method == "POST":
        author_id = session.get("current_employee_id")
        post_type = request.form.get("post_type", "")
        audience_id = request.form.get("audience_department_id", type=int)
        text = request.form.get("text", "").strip()
        if not author_id or not query_one("SELECT 1 FROM employees WHERE id = ? AND is_dismissed = 0", (author_id,)) or not text or not valid_catalog_value("post-types", post_type) or (audience_id and not valid_catalog_value("departments", audience_id)):
            flash("Выберите автора, тип публикации и введите текст. Если справочник пуст, добавьте значение в Настройках.", "error")
        else:
            try:
                db = get_db()
                cursor = db.execute(
                    "INSERT INTO posts (author_id, post_type, audience_department_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
                    (author_id, post_type, audience_id, text, datetime.now().isoformat(timespec="minutes")),
                )
                post_id = cursor.lastrowid
                for file_storage in request.files.getlist("attachments"):
                    uploaded = save_upload(file_storage, DOCUMENT_EXTENSIONS)
                    if uploaded:
                        db.execute(
                            "INSERT INTO post_attachments (post_id, stored_name, original_name) VALUES (?, ?, ?)",
                            (post_id, uploaded[0], uploaded[1]),
                        )
                db.commit()
                flash("Публикация создана.", "success")
                return redirect(url_for("feed"))
            except (ValueError, sqlite3.IntegrityError) as error:
                get_db().rollback()
                flash(str(error) if isinstance(error, ValueError) else "Не удалось создать публикацию.", "error")
    return render_template("post_form.html", employees=employees, departments=departments, post_types=post_types, post=None)


def post_is_visible(post, employee_id):
    employee = query_one("SELECT * FROM employees WHERE id=? AND is_dismissed=0", (employee_id,)) if employee_id else None
    return bool(employee and (not post["audience_department_id"] or employee["department_id"] == post["audience_department_id"] or employee["portal_role"] in LEADERSHIP_ROLES))


@app.route("/feed/<int:post_id>/edit", methods=["GET", "POST"])
def edit_post(post_id):
    employee_id = require_current_employee()
    post = query_one("SELECT * FROM posts WHERE id=?", (post_id,))
    if not post:
        abort(404)
    if not employee_id or (post["author_id"] != employee_id and not employee_can("news", employee_id)):
        flash("Редактировать публикацию может автор или сотрудник с правом управления новостями.", "error")
        return redirect(url_for("feed"))
    departments = query_all("SELECT * FROM departments ORDER BY name")
    post_types = catalog_rows("post-types")
    if request.method == "POST":
        post_type = request.form.get("post_type", "")
        audience_id = request.form.get("audience_department_id", type=int)
        text = request.form.get("text", "").strip()
        if not text or not valid_catalog_value("post-types", post_type) or (audience_id and not valid_catalog_value("departments", audience_id)):
            flash("Проверьте тип, получателей и текст публикации.", "error")
        else:
            try:
                db = get_db()
                db.execute("UPDATE posts SET post_type=?,audience_department_id=?,text=? WHERE id=?", (post_type, audience_id, text, post_id))
                for file_storage in request.files.getlist("attachments"):
                    uploaded = save_upload(file_storage, DOCUMENT_EXTENSIONS)
                    if uploaded:
                        db.execute("INSERT INTO post_attachments (post_id,stored_name,original_name) VALUES (?,?,?)", (post_id, uploaded[0], uploaded[1]))
                db.commit()
                flash("Публикация обновлена.", "success")
                return redirect(url_for("feed"))
            except ValueError as error:
                get_db().rollback()
                flash(str(error), "error")
    return render_template("post_form.html", employees=active_employees(), departments=departments, post_types=post_types, post=post)


@app.post("/feed/<int:post_id>/delete")
def delete_post(post_id):
    employee_id = require_current_employee()
    post = query_one("SELECT * FROM posts WHERE id=?", (post_id,))
    if not post:
        abort(404)
    if not employee_id or (post["author_id"] != employee_id and not employee_can("news", employee_id)):
        flash("Удалить публикацию может автор или сотрудник с правом управления новостями.", "error")
    else:
        get_db().execute("DELETE FROM posts WHERE id=?", (post_id,))
        get_db().commit()
        flash("Публикация удалена.", "success")
    return redirect(url_for("feed"))


@app.post("/feed/<int:post_id>/like")
def toggle_like(post_id):
    employee_id = require_current_employee()
    post = query_one("SELECT * FROM posts WHERE id=?", (post_id,))
    if employee_id and post and post_is_visible(post, employee_id):
        db = get_db()
        existing = query_one("SELECT 1 FROM likes WHERE post_id = ? AND employee_id = ?", (post_id, employee_id))
        if existing:
            db.execute("DELETE FROM likes WHERE post_id = ? AND employee_id = ?", (post_id, employee_id))
        else:
            db.execute("INSERT OR IGNORE INTO likes VALUES (?, ?)", (post_id, employee_id))
        db.commit()
    return redirect(request.referrer or url_for("feed"))


@app.post("/feed/<int:post_id>/reaction")
def set_reaction(post_id):
    employee_id = require_current_employee()
    reaction = request.form.get("reaction", "")
    post = query_one("SELECT * FROM posts WHERE id=?", (post_id,))
    if employee_id and post and post_is_visible(post, employee_id) and reaction in {"heart", "support", "important", "thanks"}:
        current = query_one("SELECT reaction FROM post_reactions WHERE post_id=? AND employee_id=?", (post_id, employee_id))
        db = get_db()
        if current and current["reaction"] == reaction:
            db.execute("DELETE FROM post_reactions WHERE post_id=? AND employee_id=?", (post_id, employee_id))
        else:
            db.execute("INSERT OR REPLACE INTO post_reactions (post_id,employee_id,reaction) VALUES (?,?,?)", (post_id, employee_id, reaction))
        db.commit()
    return redirect(request.referrer or url_for("feed"))


@app.post("/feed/<int:post_id>/comment")
def add_comment(post_id):
    employee_id = require_current_employee()
    text = request.form.get("text", "").strip()
    attachment = request.files.get("attachment")
    post = query_one("SELECT * FROM posts WHERE id=?", (post_id,))
    if employee_id and post and post_is_visible(post, employee_id) and (text or (attachment and attachment.filename)):
        db = get_db()
        cursor = db.execute(
            "INSERT INTO comments (post_id, author_id, text, created_at) VALUES (?, ?, ?, ?)",
            (post_id, employee_id, text, datetime.now().isoformat(timespec="minutes")),
        )
        try:
            uploaded = save_upload(attachment, DOCUMENT_EXTENSIONS)
            if uploaded:
                db.execute("INSERT INTO comment_attachments (comment_id,stored_name,original_name) VALUES (?,?,?)", (cursor.lastrowid, uploaded[0], uploaded[1]))
            db.commit()
        except ValueError as error:
            db.rollback()
            flash(str(error), "error")
    elif employee_id:
        flash("Добавьте текст или файл к комментарию.", "error")
    return redirect(request.referrer or url_for("feed"))


def period_start(period):
    today = date.today()
    if period == "week":
        return today - timedelta(days=6)
    if period == "month":
        return today - timedelta(days=29)
    if period == "six_months":
        return today - timedelta(days=183)
    if period == "quarter":
        return today - timedelta(days=91)
    if period == "year":
        return today - timedelta(days=365)
    return date(2000, 1, 1)


def employee_efficiency(employee_id, start, end=None):
    employee = query_one("""SELECT e.*, d.name AS department_name, s.label AS status_label FROM employees e
        JOIN departments d ON d.id=e.department_id LEFT JOIN presence_options s ON s.key=e.presence_status WHERE e.id=?""", (employee_id,))
    task_sql = """SELECT t.* FROM tasks t JOIN task_assignees ta ON ta.task_id=t.id
        WHERE ta.employee_id=? AND t.created_date>=?"""
    task_params = [employee_id, start.isoformat()]
    if end:
        task_sql += " AND t.created_date<=?"
        task_params.append(end.isoformat())
    tasks = query_all(task_sql + " ORDER BY t.deadline", task_params)
    completed = [task for task in tasks if task["status"] == "Выполнено"]
    on_time = [task for task in completed if task["completed_at"] and task["completed_at"] <= task["deadline"]]
    period_end = end or date.today()
    overdue_cutoff = min(period_end, date.today()).isoformat() + "T23:59"
    overdue = [task for task in tasks if (task["status"] != "Выполнено" and task["deadline"] < overdue_cutoff) or (task["completed_at"] and task["completed_at"] > task["deadline"])]
    absences = query_all("SELECT * FROM employee_absences WHERE employee_id=? AND status_key IN ('vacation','sick') AND end_date>=? AND start_date<=?", (employee_id, start.isoformat(), period_end.isoformat()))
    absence_days = sum((min(period_end, date.fromisoformat(row["end_date"])) - max(start, date.fromisoformat(row["start_date"]))).days + 1 for row in absences if min(period_end, date.fromisoformat(row["end_date"])) >= max(start, date.fromisoformat(row["start_date"])))
    total = len(tasks)
    on_time_percent = round(len(on_time) * 100 / total) if total else 0
    overdue_percent = round(len(overdue) * 100 / total) if total else 0
    thresholds = {row["key"]: row["value"] for row in query_all("SELECT key,value FROM app_settings")}
    if absence_days >= int(thresholds.get("sick_days_threshold", 10)):
        recommendation = "Сотрудник часто отсутствует по болезни. Рекомендуется обратить внимание на распределение нагрузки."
    elif overdue_percent > int(thresholds.get("overdue_threshold", 20)):
        recommendation = "У сотрудника наблюдается большое количество просроченных задач. Рекомендуется провести беседу и уточнить причины."
    elif on_time_percent > int(thresholds.get("high_result_threshold", 90)):
        recommendation = "Сотрудник демонстрирует стабильно высокие результаты. Рекомендуется рассмотреть возможность повышения квалификации или включения в кадровый резерв."
    else:
        recommendation = "Показатели находятся в рабочем диапазоне. Рекомендуется продолжать плановое наблюдение за динамикой."
    weeks = []
    for index in range(3, -1, -1):
        week_end = period_end - timedelta(days=index * 7)
        week_start = week_end - timedelta(days=6)
        weeks.append({
            "label": f"{week_start.strftime('%d.%m')}–{week_end.strftime('%d.%m')}",
            "completed": sum(1 for task in completed if task["completed_at"] and week_start.isoformat() <= task["completed_at"][:10] <= week_end.isoformat()),
            "overdue": sum(1 for task in overdue if week_start.isoformat() <= (task["completed_at"] or task["deadline"])[:10] <= week_end.isoformat()),
        })
    roles = query_all("""SELECT ep.position_name, ep.rate, ep.project, COUNT(CASE WHEN t.status='Выполнено' THEN 1 END) AS completed
        FROM employee_positions ep LEFT JOIN task_positions tp ON tp.employee_position_id=ep.id
        LEFT JOIN tasks t ON t.id=tp.task_id AND t.created_date>=? AND t.created_date<=? WHERE ep.employee_id=?
        GROUP BY ep.id ORDER BY ep.is_primary DESC,ep.id""", (start.isoformat(), period_end.isoformat(), employee_id))
    return {"employee": employee, "tasks": tasks, "total": total, "completed": completed, "on_time": len(on_time), "overdue": len(overdue), "on_time_percent": on_time_percent, "overdue_percent": overdue_percent, "absence_days": absence_days, "recommendation": recommendation, "weeks": weeks, "roles": roles}


def analytics_data(period, start=None, end=None):
    start = start or period_start(period)
    departments = []
    for department in catalog_rows("departments"):
        if end:
            tasks = query_all("SELECT * FROM tasks WHERE department_id=? AND created_date>=? AND created_date<=?", (department["id"], start.isoformat(), end.isoformat()))
        else:
            tasks = query_all("SELECT * FROM tasks WHERE department_id=? AND created_date>=?", (department["id"], start.isoformat()))
        completed = sum(1 for task in tasks if task["status"] == "Выполнено")
        overdue_cutoff = min(end or date.today(), date.today()).isoformat() + "T23:59"
        overdue = sum(1 for task in tasks if task["status"] != "Выполнено" and task["deadline"] < overdue_cutoff)
        departments.append({"name": department["name"], "total": len(tasks), "completed": completed, "active": len(tasks) - completed, "overdue": overdue})
    departments.sort(key=lambda item: item["completed"], reverse=True)
    maximum = max((item["completed"] for item in departments), default=1) or 1
    for item in departments:
        item["height"] = max(6, round(item["completed"] * 100 / maximum))
    return departments, start


def analytics_bounds():
    settings = {row["key"]: row["value"] for row in query_all("SELECT key,value FROM app_settings")}
    period = request.args.get("period") or settings.get("analytics_period", "month")
    month = request.args.get("month", "")
    start_value = request.args.get("start", "")
    end_value = request.args.get("end", "")
    try:
        if month:
            start = date.fromisoformat(f"{month}-01")
            end = date(start.year + (1 if start.month == 12 else 0), 1 if start.month == 12 else start.month + 1, 1) - timedelta(days=1)
            return "month", start, end, month
        if start_value and end_value:
            start, end = date.fromisoformat(start_value), date.fromisoformat(end_value)
            if end < start:
                raise ValueError
            return "custom", start, end, ""
    except ValueError:
        flash("Проверьте выбранный период аналитики.", "error")
    return period, period_start(period), date.today(), ""


@app.get("/analytics")
def analytics():
    if not require_feature("analytics"):
        return redirect(url_for("profile"))
    period, start, end, selected_month = analytics_bounds()
    departments, start = analytics_data(period, start, end)
    employee_id = request.args.get("employee_id", type=int)
    profile = employee_efficiency(employee_id, start, end) if employee_id else None
    ranking = []
    for employee in active_employees():
        data = employee_efficiency(employee["id"], start, end)
        ranking.append({"employee": employee, "completed": len(data["completed"]), "overdue": data["overdue"], "percent": round(len(data["completed"]) * 100 / data["total"]) if data["total"] else 0, "tasks": data["tasks"]})
    ranking.sort(key=lambda item: item["completed"], reverse=True)
    reports = query_all("""SELECT wr.*, e.full_name, d.name AS department_name FROM weekly_reports wr
        JOIN employees e ON e.id=wr.employee_id JOIN departments d ON d.id=e.department_id ORDER BY week_start DESC,e.full_name""")
    calendar_month = date.fromisoformat(f"{selected_month}-01") if selected_month else start.replace(day=1)
    month_weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(calendar_month.year, calendar_month.month)
    previous_month = (calendar_month.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    next_month = date(calendar_month.year + (1 if calendar_month.month == 12 else 0), 1 if calendar_month.month == 12 else calendar_month.month + 1, 1).strftime("%Y-%m")
    return render_template("analytics.html", departments=departments, period=period, start=start, end=end, selected_month=selected_month, calendar_month=calendar_month, month_weeks=month_weeks, previous_month=previous_month, next_month=next_month, employees=active_employees(), profile=profile, ranking=ranking, reports=reports)


@app.get("/analytics/export")
def export_analytics():
    if not require_feature("analytics"):
        return redirect(url_for("profile"))
    period, start, end, _selected_month = analytics_bounds()
    departments, _start = analytics_data(period, start, end)
    if request.args.get("format") == "xlsx":
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Отделы"
        sheet.append(["Отдел", "Всего задач", "Выполнено", "В работе", "Просрочено"])
        for item in departments:
            sheet.append([item["name"], item["total"], item["completed"], item["active"], item["overdue"]])
        people_sheet = workbook.create_sheet("Сотрудники")
        people_sheet.append(["ФИО", "Отдел", "Основная должность", "Все должности и ставки", "Всего задач", "Выполнено", "В срок", "Просрочено", "Выполнение, %"])
        task_sheet = workbook.create_sheet("Задачи сотрудников")
        task_sheet.append(["ФИО", "№ задачи", "Название", "Отдел", "Статус", "Создана", "Дедлайн", "Завершена", "Причина просрочки"])
        for employee in active_employees():
            data = employee_efficiency(employee["id"], start, end)
            department = query_one("SELECT name FROM departments WHERE id=?", (employee["department_id"],))["name"]
            positions = query_all("SELECT position_name,rate,project FROM employee_positions WHERE employee_id=? ORDER BY is_primary DESC,id", (employee["id"],))
            positions_text = "; ".join(f"{item['position_name']} — {item['rate']} ставки" + (f" ({item['project']})" if item["project"] else "") for item in positions)
            people_sheet.append([employee["full_name"], department, employee["position"], positions_text, data["total"], len(data["completed"]), data["on_time"], data["overdue"], round(len(data["completed"]) * 100 / data["total"]) if data["total"] else 0])
            for task in data["tasks"]:
                task_department = query_one("SELECT name FROM departments WHERE id=?", (task["department_id"],))["name"]
                task_sheet.append([employee["full_name"], task["department_number"] or task["id"], task["title"], task_department, task["workflow_status"], task["created_date"], task["deadline"], task["completed_at"] or "", task["overdue_reason"]])
        for cell in sheet[1]:
            cell.font = cell.font.copy(bold=True)
        for current_sheet in (people_sheet, task_sheet):
            for cell in current_sheet[1]:
                cell.font = cell.font.copy(bold=True)
            current_sheet.freeze_panes = "A2"
        output = io.BytesIO()
        workbook.save(output)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=pioneer-analytics.xlsx"})
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Отдел", "Всего задач", "Выполнено", "В работе", "Просрочено"])
    for item in departments:
        writer.writerow([item["name"], item["total"], item["completed"], item["active"], item["overdue"]])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=pioneer-analytics.csv"})


@app.get("/analytics/weekly-export")
def export_weekly_report():
    if not require_feature("analytics"):
        return redirect(url_for("profile"))
    _period, start, end, _month = analytics_bounds()
    if request.args.get("format") == "docx":
        from docx import Document
        document = Document()
        document.add_heading("ПИОНЕР. Сводный отчёт", 0)
        document.add_paragraph(f"Период с {start.strftime('%d.%m.%Y')} по {end.strftime('%d.%m.%Y')}")
        table = document.add_table(rows=1, cols=5)
        table.style = "Table Grid"
        for cell, title in zip(table.rows[0].cells, ["ФИО", "Отдел", "Выполненные задачи", "Просроченные задачи и причины", "Выполнение"]):
            cell.text = title
        for employee in active_employees():
            data = employee_efficiency(employee["id"], start, end)
            completed_names = ", ".join(task["title"] for task in data["completed"]) or "Нет"
            overdue_names = ", ".join(f"{task['title']} ({task['overdue_reason'] or 'причина не указана'})" for task in data["tasks"] if (task["status"] != "Выполнено" and task["deadline"] < datetime.now().isoformat(timespec="minutes")) or (task["completed_at"] and task["completed_at"] > task["deadline"])) or "Нет"
            positions = query_all("SELECT position_name,rate,project FROM employee_positions WHERE employee_id=? ORDER BY is_primary DESC,id", (employee["id"],))
            document.add_heading(employee["full_name"], level=1)
            document.add_paragraph("Должности: " + "; ".join(f"{item['position_name']} — {item['rate']} ставки" + (f" ({item['project']})" if item["project"] else "") for item in positions))
            document.add_paragraph(f"Задач: {data['total']}; выполнено: {len(data['completed'])}; в срок: {data['on_time']}; просрочено: {data['overdue']}.")
            if data["tasks"]:
                employee_tasks = document.add_table(rows=1, cols=5)
                employee_tasks.style = "Table Grid"
                for cell, title in zip(employee_tasks.rows[0].cells, ["№", "Задача", "Статус", "Дедлайн", "Результат"]):
                    cell.text = title
                for task in data["tasks"]:
                    cells = employee_tasks.add_row().cells
                    values = [task["department_number"] or task["id"], task["title"], task["workflow_status"], ru_datetime(task["deadline"]), task["overdue_reason"] or ("Выполнено" if task["status"] == "Выполнено" else "В работе")]
                    for cell, value in zip(cells, values):
                        cell.text = str(value)
            row = table.add_row().cells
            values = [employee["full_name"], query_one("SELECT name FROM departments WHERE id=?", (employee["department_id"],))["name"], completed_names, overdue_names, f"{round(len(data['completed']) * 100 / data['total']) if data['total'] else 0}%"]
            for cell, value in zip(row, values):
                cell.text = str(value)
        output = io.BytesIO()
        document.save(output)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": "attachment; filename=pioneer-weekly-report.docx"})
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ФИО", "Отдел", "Выполненные задачи", "Просроченные задачи и причины", "Процент выполнения"])
    for employee in active_employees():
        data = employee_efficiency(employee["id"], start, end)
        completed_names = ", ".join(task["title"] for task in data["completed"])
        overdue_names = ", ".join(f"{task['title']} ({task['overdue_reason'] or 'причина не указана'})" for task in data["tasks"] if (task["status"] != "Выполнено" and task["deadline"] < datetime.now().isoformat(timespec="minutes")) or (task["completed_at"] and task["completed_at"] > task["deadline"]))
        writer.writerow([employee["full_name"], query_one("SELECT name FROM departments WHERE id=?", (employee["department_id"],))["name"], completed_names, overdue_names, f"{round(len(data['completed']) * 100 / data['total']) if data['total'] else 0}%"])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=pioneer-weekly-report.csv"})


@app.post("/profile/weekly-report")
def create_weekly_report():
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("profile"))
    week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    get_db().execute("INSERT OR REPLACE INTO weekly_reports (employee_id, week_start, created_at) VALUES (?, ?, ?)", (employee_id, week_start, datetime.now().isoformat(timespec="minutes")))
    get_db().commit()
    flash("Отчёт за неделю сформирован и доступен руководству.", "success")
    return redirect(url_for("profile"))


@app.get("/reports/<int:report_id>")
def weekly_report(report_id):
    report = query_one("""SELECT wr.*, e.full_name, e.photo, d.name AS department_name FROM weekly_reports wr
        JOIN employees e ON e.id=wr.employee_id JOIN departments d ON d.id=e.department_id WHERE wr.id=?""", (report_id,))
    if not report:
        abort(404)
    current_id = require_current_employee()
    if not current_id or (current_id != report["employee_id"] and not employee_can("analytics", current_id)):
        flash("Этот отчёт доступен сотруднику и руководству.", "error")
        return redirect(url_for("profile"))
    start = date.fromisoformat(report["week_start"])
    end = start + timedelta(days=6)
    tasks = query_all("""SELECT t.* FROM tasks t JOIN task_assignees ta ON ta.task_id=t.id WHERE ta.employee_id=?
        AND (t.created_date<=? AND (t.completed_at IS NULL OR t.completed_at>=?)) ORDER BY t.status,t.deadline""", (report["employee_id"], end.isoformat(), start.isoformat()))
    completed = [task for task in tasks if task["completed_at"] and start.isoformat() <= task["completed_at"][:10] <= end.isoformat()]
    active = [task for task in tasks if task["status"] != "Выполнено" and task["deadline"] >= datetime.now().isoformat(timespec="minutes")]
    overdue = [task for task in tasks if task["status"] != "Выполнено" and task["deadline"] < datetime.now().isoformat(timespec="minutes")]
    return render_template("weekly_report.html", report=report, start=start, end=end, completed=completed, active=active, overdue=overdue)


@app.route("/notifications")
def notifications():
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("profile"))
    rows = query_all(
        "SELECT * FROM notifications WHERE employee_id = ? ORDER BY created_at DESC, id DESC",
        (employee_id,),
    )
    return render_template("notifications.html", notifications=rows)


@app.get("/notifications/status")
def notification_status():
    employee_id = require_current_employee()
    if not employee_id:
        return jsonify({"unread": 0, "latest": 0}), 401
    ensure_workday_reminder(employee_id)
    row = query_one("SELECT COUNT(*) AS unread,COALESCE(MAX(id),0) AS latest FROM notifications WHERE employee_id=? AND is_read=0", (employee_id,))
    popup = query_one("""SELECT id,text,action_url FROM notifications WHERE employee_id=? AND is_read=0
        AND kind='workday-reminder' ORDER BY id DESC LIMIT 1""", (employee_id,))
    return jsonify({"unread": row["unread"], "latest": row["latest"], "popup": dict(popup) if popup else None})


@app.post("/notifications/read-all")
def read_all_notifications():
    employee_id = require_current_employee()
    if employee_id:
        get_db().execute("UPDATE notifications SET is_read = 1 WHERE employee_id = ?", (employee_id,))
        get_db().commit()
    return redirect(url_for("notifications" if employee_id else "profile"))


@app.get("/notifications/<int:notification_id>/open")
def open_notification(notification_id):
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("profile"))
    notification = query_one(
        "SELECT * FROM notifications WHERE id = ? AND employee_id = ?",
        (notification_id, employee_id),
    )
    if not notification:
        abort(404)
    get_db().execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (notification_id,))
    get_db().commit()
    if notification["action_url"]:
        return redirect(notification["action_url"])
    if notification["task_id"]:
        return redirect(url_for("edit_task", task_id=notification["task_id"]))
    return redirect(url_for("notifications"))


def room_access(room_id, employee_id):
    return query_one(
        """SELECT r.* FROM chat_rooms r WHERE r.id = ? AND
           (r.kind = 'general' OR EXISTS
           (SELECT 1 FROM chat_members m WHERE m.room_id = r.id AND m.employee_id = ?))
           AND (r.kind != 'private' OR NOT EXISTS
           (SELECT 1 FROM chat_members m JOIN employees e ON e.id = m.employee_id
            WHERE m.room_id = r.id AND e.is_dismissed = 1))""",
        (room_id, employee_id),
    )


@app.get("/messages")
@app.get("/messages/<int:room_id>")
def messages(room_id=None):
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("profile"))
    rooms = query_all(
        """SELECT r.* FROM chat_rooms r WHERE r.kind = 'general' OR
           (EXISTS (SELECT 1 FROM chat_members m WHERE m.room_id = r.id AND m.employee_id = ?)
            AND (r.kind != 'private' OR NOT EXISTS
            (SELECT 1 FROM chat_members m JOIN employees e ON e.id = m.employee_id
             WHERE m.room_id = r.id AND e.is_dismissed = 1)))
           ORDER BY CASE WHEN r.kind = 'general' THEN 0 ELSE 1 END, r.id DESC""",
        (employee_id,),
    )
    if room_id is None and rooms:
        room_id = rooms[0]["id"]
    room = room_access(room_id, employee_id) if room_id else None
    if room_id and not room:
        abort(404)
    room_names = {}
    for chat in rooms:
        if chat["kind"] == "private":
            other = query_one(
                "SELECT e.full_name FROM chat_members m JOIN employees e ON e.id = m.employee_id WHERE m.room_id = ? AND m.employee_id != ?",
                (chat["id"], employee_id),
            )
            room_names[chat["id"]] = other["full_name"] if other else chat["name"]
        else:
            room_names[chat["id"]] = chat["name"]
    chat_messages = query_all(
        """SELECT m.*, e.full_name, e.photo, e.is_dismissed,
                  (SELECT COUNT(*) FROM chat_reads cr WHERE cr.room_id=m.room_id AND cr.employee_id!=m.author_id AND cr.last_message_id>=m.id) AS read_count
           FROM chat_messages m
           JOIN employees e ON e.id = m.author_id WHERE m.room_id = ? ORDER BY m.id""",
        (room_id,),
    ) if room else []
    members = query_all(
        """SELECT e.* FROM chat_members m JOIN employees e ON e.id = m.employee_id
           WHERE m.room_id = ? AND e.is_dismissed = 0 ORDER BY e.full_name""",
        (room_id,),
    ) if room and room["kind"] != "general" else active_employees()
    if room:
        last_message_id = chat_messages[-1]["id"] if chat_messages else 0
        get_db().execute("INSERT OR REPLACE INTO chat_reads (room_id, employee_id, last_message_id) VALUES (?, ?, ?)", (room_id, employee_id, last_message_id))
        get_db().commit()
    return render_template("messages.html", rooms=rooms, room=room, room_names=room_names, chat_messages=chat_messages, members=members, people=active_employees())


@app.get("/messages/<int:room_id>/export")
def export_messages(room_id):
    employee_id = require_current_employee()
    room = room_access(room_id, employee_id) if employee_id else None
    if not room:
        abort(404)
    rows = query_all("""SELECT m.created_at,e.full_name,m.text,m.original_name FROM chat_messages m
        JOIN employees e ON e.id=m.author_id WHERE m.room_id=? ORDER BY m.id""", (room_id,))
    if request.args.get("format") == "docx":
        from docx import Document
        document = Document()
        document.add_heading(f"ПИОНЕР. Сообщения: {room['name']}", 0)
        for row in rows:
            document.add_paragraph(f"{ru_datetime(row['created_at'])} · {row['full_name']}", style="Heading 2")
            document.add_paragraph(row["text"] or f"Вложение: {row['original_name']}")
        output = io.BytesIO()
        document.save(output)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": f"attachment; filename=pioneer-messages-{room_id}.docx"})
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Сообщения"
    sheet.append(["Дата и время", "Автор", "Сообщение", "Вложение"])
    for row in rows:
        sheet.append([row["created_at"], row["full_name"], row["text"], row["original_name"] or ""])
    output = io.BytesIO()
    workbook.save(output)
    return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=pioneer-messages-{room_id}.xlsx"})


@app.post("/messages/create")
def create_chat():
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("profile"))
    kind = request.form.get("kind")
    participants = set(parse_id_list(request.form.getlist("participants"))) - {employee_id}
    name = request.form.get("name", "").strip()
    link = request.form.get("call_link", "").strip()
    if kind not in {"private", "group"} or len(participants) < (1 if kind == "private" else 2) or (kind == "private" and len(participants) != 1) or (kind == "group" and (not name or len(participants) > 4)) or any(not query_one("SELECT 1 FROM employees WHERE id = ? AND is_dismissed = 0", (item,)) for item in participants):
        flash("Для личного чата выберите одного коллегу; для группы — название и от двух до четырёх коллег (до пяти участников вместе с вами).", "error")
        return redirect(url_for("messages"))
    if link:
        parsed = urlsplit(link)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            flash("Ссылка для звонка должна начинаться с http:// или https://.", "error")
            return redirect(url_for("messages"))
    if kind == "private":
        other = next(iter(participants))
        existing = query_one(
            """SELECT r.id FROM chat_rooms r JOIN chat_members a ON a.room_id = r.id AND a.employee_id = ?
               JOIN chat_members b ON b.room_id = r.id AND b.employee_id = ?
               WHERE r.kind = 'private' AND (SELECT COUNT(*) FROM chat_members WHERE room_id = r.id) = 2""",
            (employee_id, other),
        )
        if existing:
            return redirect(url_for("messages", room_id=existing["id"]))
        name = query_one("SELECT full_name FROM employees WHERE id = ?", (other,))["full_name"]
    db = get_db()
    cursor = db.execute("INSERT INTO chat_rooms (name, kind, call_link) VALUES (?, ?, ?)", (name, kind, link))
    room_id = cursor.lastrowid
    db.executemany("INSERT INTO chat_members VALUES (?, ?)", [(room_id, person) for person in participants | {employee_id}])
    db.commit()
    return redirect(url_for("messages", room_id=room_id))


@app.post("/messages/<int:room_id>/send")
def send_chat_message(room_id):
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("profile"))
    if not room_access(room_id, employee_id):
        abort(404)
    text = request.form.get("text", "").strip()
    if not text and not request.files.get("attachment", None):
        flash("Напишите сообщение или прикрепите файл.", "error")
        return redirect(url_for("messages", room_id=room_id))
    try:
        uploaded = save_upload(request.files.get("attachment"), DOCUMENT_EXTENSIONS)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("messages", room_id=room_id))
    get_db().execute(
        "INSERT INTO chat_messages (room_id, author_id, text, stored_name, original_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (room_id, employee_id, text, uploaded[0] if uploaded else None, uploaded[1] if uploaded else None, datetime.now().isoformat(timespec="minutes")),
    )
    get_db().commit()
    return redirect(url_for("messages", room_id=room_id))


@app.post("/messages/<int:room_id>/call-link")
def update_chat_call_link(room_id):
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("profile"))
    if not room_access(room_id, employee_id):
        abort(404)
    link = request.form.get("call_link", "").strip()
    if link:
        parsed = urlsplit(link)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            flash("Ссылка для звонка должна начинаться с http:// или https://.", "error")
            return redirect(url_for("messages", room_id=room_id))
    get_db().execute("UPDATE chat_rooms SET call_link = ? WHERE id = ?", (link, room_id))
    get_db().commit()
    flash("Ссылка для звонка сохранена.", "success")
    return redirect(url_for("messages", room_id=room_id))


@app.route("/meetings")
@app.route("/calendar")
def meetings():
    now = datetime.now().isoformat(timespec="minutes")
    selected_room_id = request.args.get("room_id", type=int)
    rooms = catalog_rows("rooms")
    if not selected_room_id and rooms:
        selected_room_id = rooms[0]["id"]
    selected_date_value = request.args.get("selected_date", "")
    date_from_value = request.args.get("date_from", "")
    date_to_value = request.args.get("date_to", "")
    try:
        selected_date = date.fromisoformat(selected_date_value) if selected_date_value else date.today()
        range_start = date.fromisoformat(date_from_value) if date_from_value else selected_date - timedelta(days=selected_date.weekday())
        range_end = date.fromisoformat(date_to_value) if date_to_value else range_start + timedelta(days=6)
        if range_end < range_start or (range_end - range_start).days > 30:
            raise ValueError
    except ValueError:
        selected_date = date.today()
        range_start = selected_date - timedelta(days=selected_date.weekday())
        range_end = range_start + timedelta(days=6)
        flash("Период календаря должен составлять от 1 до 31 дня.", "error")
    days = [range_start + timedelta(days=index) for index in range((range_end - range_start).days + 1)]
    rows = query_all(
        """SELECT m.*,et.label AS event_type_label,et.icon AS event_type_icon,GROUP_CONCAT(e.full_name) AS participant_names
           FROM meetings m LEFT JOIN event_types et ON et.key=m.event_type
           LEFT JOIN meeting_participants mp ON mp.meeting_id = m.id LEFT JOIN employees e ON e.id = mp.employee_id
           WHERE substr(m.meeting_at,1,10)>=? AND substr(m.meeting_at,1,10)<=?
           GROUP BY m.id ORDER BY m.meeting_at""", (range_start.isoformat(), range_end.isoformat()))
    participants = {meeting["id"]: query_all("""SELECT e.* FROM meeting_participants mp JOIN employees e ON e.id=mp.employee_id
        WHERE mp.meeting_id=? ORDER BY e.full_name""", (meeting["id"],)) for meeting in rows}
    bookings = query_all("""SELECT rb.*,
        COALESCE((SELECT GROUP_CONCAT(d.name, ', ') FROM room_booking_departments rbd JOIN departments d ON d.id=rbd.department_id WHERE rbd.booking_id=rb.id), d0.name) AS department_names,
        COALESCE((SELECT GROUP_CONCAT(e.full_name, ', ') FROM room_booking_responsibles rbr JOIN employees e ON e.id=rbr.employee_id WHERE rbr.booking_id=rb.id), e0.full_name) AS responsible_names
        FROM room_bookings rb JOIN departments d0 ON d0.id=rb.department_id JOIN employees e0 ON e0.id=rb.responsible_employee_id
        WHERE rb.room_id=? AND rb.booking_date>=? AND rb.booking_date<=? ORDER BY rb.booking_date,rb.start_hour""", (selected_room_id, days[0].isoformat(), days[-1].isoformat())) if selected_room_id else []
    booking_map = {(row["booking_date"], hour): row for row in bookings for hour in range(row["start_hour"], row["end_hour"])}
    birthdays = query_all("SELECT * FROM employees WHERE is_dismissed=0 ORDER BY full_name")
    birthday_events = [(day, [employee for employee in birthdays if employee["birth_date"][5:] == day.isoformat()[5:]]) for day in days]
    selected_room = next((room for room in rooms if room["id"] == selected_room_id), None)
    interval_days = len(days)
    return render_template("meetings.html", meetings=rows, now=now, participants=participants, rooms=rooms, selected_room=selected_room, days=days, hours=range(9, 21), booking_map=booking_map, selected_date=selected_date, date_from=range_start, date_to=range_end, previous_date_from=range_start-timedelta(days=interval_days), previous_date_to=range_end-timedelta(days=interval_days), next_date_from=range_start+timedelta(days=interval_days), next_date_to=range_end+timedelta(days=interval_days), departments=catalog_rows("departments"), employees=active_employees(), birthday_events=birthday_events, can_edit_rooms=bool(selected_room and (employee_can("rooms") or selected_room["responsible_employee_id"] == session.get("current_employee_id"))))


@app.route("/meetings/new", methods=["GET", "POST"])
@app.route("/calendar/new", methods=["GET", "POST"])
def new_meeting():
    employees = active_employees()
    event_types = catalog_rows("event-types")
    organizer_id = require_current_employee()
    if not organizer_id:
        return redirect(url_for("employee_login"))
    if request.method == "POST":
        topic = request.form.get("topic", "").strip()
        meeting_at = request.form.get("meeting_at", "")
        link = request.form.get("link", "").strip()
        notes = request.form.get("notes", "").strip()
        event_type = request.form.get("event_type", "meeting")
        participants = list(dict.fromkeys(parse_id_list(request.form.getlist("participants"))))
        if not topic or not meeting_at or not participants or not valid_catalog_value("event-types", event_type):
            flash("Укажите тему, дату и хотя бы одного участника.", "error")
        else:
            try:
                datetime.fromisoformat(meeting_at)
                if link:
                    parsed = urlsplit(link)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        raise ValueError
                db = get_db()
                cursor = db.execute(
                    "INSERT INTO meetings (topic, meeting_at, link, notes, created_at, event_type, organizer_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (topic, meeting_at, link, notes, datetime.now().isoformat(timespec="minutes"), event_type, organizer_id),
                )
                meeting_id = cursor.lastrowid
                if any(not query_one("SELECT 1 FROM employees WHERE id=? AND is_dismissed=0", (item,)) for item in participants):
                    raise ValueError
                db.executemany("INSERT INTO meeting_participants VALUES (?, ?)", [(meeting_id, item) for item in participants])
                event_type_row = query_one("SELECT label FROM event_types WHERE key=?", (event_type,))
                event_label = event_type_row["label"] if event_type_row else "Событие"
                text = f"Вы приглашены: {event_label.lower()} «{topic}» назначена на {ru_datetime(meeting_at)}."
                notify_entity(db, participants, "meeting", meeting_id, "meeting-created", text, url_for("meetings"))
                db.commit()
                placeholders = ",".join("?" for _ in participants)
                for person in query_all(f"SELECT id,email FROM employees WHERE id IN ({placeholders}) AND email IS NOT NULL", participants):
                    send_email(person["email"], f"Новое событие в ПИОНЕР: {event_label}", text + (f"\nСсылка: {link}" if link else ""), person["id"])
                flash("Комната встречи создана.", "success")
                return redirect(url_for("meetings"))
            except (ValueError, sqlite3.IntegrityError):
                flash("Проверьте данные встречи.", "error")
    return render_template("meeting_form.html", employees=employees, event_types=event_types)


@app.post("/meetings/<int:meeting_id>/cancel")
def cancel_meeting(meeting_id):
    employee_id = require_current_employee()
    meeting = query_one("SELECT * FROM meetings WHERE id=?", (meeting_id,))
    if not meeting:
        abort(404)
    if not employee_id or (meeting["organizer_id"] != employee_id and not employee_can("rooms", employee_id)):
        flash("Отменить встречу может организатор или ответственный за календарь.", "error")
        return redirect(url_for("meetings"))
    db = get_db()
    db.execute("UPDATE meetings SET status='Отменена' WHERE id=?", (meeting_id,))
    participant_ids = [row["employee_id"] for row in query_all("SELECT employee_id FROM meeting_participants WHERE meeting_id=?", (meeting_id,))]
    text = f"Встреча «{meeting['topic']}» отменена."
    notify_entity(db, participant_ids, "meeting", meeting_id, "meeting-cancelled", text, url_for("meetings"))
    db.commit()
    for person in query_all("SELECT id,email FROM employees WHERE id IN (SELECT employee_id FROM meeting_participants WHERE meeting_id=?) AND email IS NOT NULL", (meeting_id,)):
        send_email(person["email"], "Встреча отменена", text, person["id"])
    flash("Встреча отменена, участники уведомлены.", "success")
    return redirect(url_for("meetings"))


@app.post("/meetings/<int:meeting_id>/recording")
def upload_meeting_recording(meeting_id):
    employee_id = require_current_employee()
    meeting = query_one("SELECT * FROM meetings WHERE id=?", (meeting_id,))
    if not meeting:
        abort(404)
    if not employee_id or (meeting["organizer_id"] != employee_id and not employee_can("rooms", employee_id)):
        flash("Загрузить запись может организатор или ответственный за календарь.", "error")
        return redirect(url_for("meetings"))
    try:
        uploaded = save_upload(request.files.get("recording"), RECORDING_EXTENSIONS)
        if not uploaded:
            raise ValueError("Выберите файл записи.")
        db = get_db()
        db.execute("UPDATE meetings SET recording_stored_name=?, recording_original_name=? WHERE id=?", (uploaded[0], uploaded[1], meeting_id))
        participant_ids = [row["employee_id"] for row in query_all("SELECT employee_id FROM meeting_participants WHERE meeting_id=?", (meeting_id,))]
        notify_entity(db, participant_ids, "meeting", meeting_id, "meeting-recording", f"Доступна запись встречи «{meeting['topic']}».", url_for("meetings"))
        db.commit()
        flash("Запись встречи загружена и доступна участникам.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("meetings"))


@app.post("/calendar/rooms/book")
def book_room():
    employee_id = require_current_employee()
    room_id = request.form.get("room_id", type=int)
    room = query_one("SELECT * FROM rooms WHERE id=?", (room_id,))
    if not employee_id or not room or not (employee_can("rooms", employee_id) or room["responsible_employee_id"] == employee_id):
        flash("Бронирование может изменять только назначенный ответственный за зал.", "error")
        return redirect(url_for("meetings", room_id=room_id))
    booking_date = request.form.get("booking_date", "")
    start_hour = request.form.get("start_hour", type=int)
    end_hour = request.form.get("end_hour", type=int)
    title = request.form.get("title", "").strip()
    department_ids = list(dict.fromkeys(parse_id_list(request.form.getlist("department_ids"))))
    responsible_ids = list(dict.fromkeys(parse_id_list(request.form.getlist("responsible_employee_ids"))))
    try:
        date.fromisoformat(booking_date)
        if not title or start_hour not in range(9, 21) or end_hour not in range(10, 22) or end_hour <= start_hour or not department_ids or not responsible_ids:
            raise ValueError
        if any(not query_one("SELECT 1 FROM departments WHERE id=?", (item,)) for item in department_ids) or any(not query_one("SELECT 1 FROM employees WHERE id=? AND is_dismissed=0", (item,)) for item in responsible_ids):
            raise ValueError
        overlap = query_one("SELECT 1 FROM room_bookings WHERE room_id=? AND booking_date=? AND start_hour < ? AND end_hour > ?", (room_id, booking_date, end_hour, start_hour))
        if overlap:
            flash("Выбранное время уже занято.", "error")
        else:
            db = get_db()
            cursor = db.execute("""INSERT INTO room_bookings (room_id,booking_date,start_hour,end_hour,title,department_id,responsible_employee_id,created_by,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (room_id, booking_date, start_hour, end_hour, title, department_ids[0], responsible_ids[0], employee_id, datetime.now().isoformat(timespec="minutes")))
            booking_id = cursor.lastrowid
            db.executemany("INSERT INTO room_booking_departments (booking_id,department_id) VALUES (?,?)", [(booking_id, item) for item in department_ids])
            db.executemany("INSERT INTO room_booking_responsibles (booking_id,employee_id,is_primary) VALUES (?,?,?)", [(booking_id, item, 1 if index == 0 else 0) for index, item in enumerate(responsible_ids)])
            notify_entity(db, responsible_ids, "room_booking", booking_id, "room-booking-created", f"Вы назначены ответственным за мероприятие «{title}» {ru_date(booking_date)} с {start_hour:02d}:00 до {end_hour:02d}:00.", url_for("meetings", room_id=room_id, selected_date=booking_date))
            db.commit()
            flash("Зал забронирован.", "success")
    except (ValueError, sqlite3.IntegrityError):
        flash("Проверьте дату, время и обязательные поля бронирования.", "error")
    return redirect(url_for("meetings", room_id=room_id))


@app.get("/calendar/rooms/export")
def export_room_usage():
    employee_id = require_current_employee()
    if not employee_id or not employee_can("rooms", employee_id):
        flash("Сводка помещений доступна ответственным за календарь.", "error")
        return redirect(url_for("meetings"))
    month = request.args.get("month", date.today().strftime("%Y-%m"))
    try:
        start = date.fromisoformat(f"{month}-01")
    except ValueError:
        flash("Выберите корректный месяц.", "error")
        return redirect(url_for("meetings"))
    end = date(start.year + (1 if start.month == 12 else 0), 1 if start.month == 12 else start.month + 1, 1)
    room_id = request.args.get("room_id", type=int)
    conditions = "rb.booking_date>=? AND rb.booking_date<?"
    params = [start.isoformat(), end.isoformat()]
    if room_id:
        conditions += " AND rb.room_id=?"
        params.append(room_id)
    rows = query_all(f"""SELECT rb.*,r.name AS room_name,
        (SELECT GROUP_CONCAT(d.name, ', ') FROM room_booking_departments x JOIN departments d ON d.id=x.department_id WHERE x.booking_id=rb.id) AS department_names,
        (SELECT GROUP_CONCAT(e.full_name, ', ') FROM room_booking_responsibles x JOIN employees e ON e.id=x.employee_id WHERE x.booking_id=rb.id) AS responsible_names
        FROM room_bookings rb JOIN rooms r ON r.id=rb.room_id WHERE {conditions} ORDER BY rb.booking_date,rb.start_hour,r.name""", params)
    if request.args.get("format") == "docx":
        from docx import Document
        document = Document()
        document.add_heading(f"ПИОНЕР. Использование залов за {month}", 0)
        table = document.add_table(rows=1, cols=6)
        table.style = "Table Grid"
        for cell, title in zip(table.rows[0].cells, ["Дата", "Время", "Помещение", "Мероприятие", "Отделы", "Ответственные"]):
            cell.text = title
        for row in rows:
            cells = table.add_row().cells
            values = [ru_date(row["booking_date"]), f"{row['start_hour']:02d}:00–{row['end_hour']:02d}:00", row["room_name"], row["title"], row["department_names"] or "", row["responsible_names"] or ""]
            for cell, value in zip(cells, values):
                cell.text = str(value)
        output = io.BytesIO()
        document.save(output)
        return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": f"attachment; filename=pioneer-rooms-{month}.docx"})
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Использование залов"
    sheet.append(["Дата", "Время", "Помещение", "Мероприятие", "Отделы", "Ответственные"])
    for row in rows:
        sheet.append([ru_date(row["booking_date"]), f"{row['start_hour']:02d}:00–{row['end_hour']:02d}:00", row["room_name"], row["title"], row["department_names"] or "", row["responsible_names"] or ""])
    for cell in sheet[1]:
        cell.font = cell.font.copy(bold=True)
    sheet.freeze_panes = "A2"
    widths = (13, 16, 24, 40, 35, 35)
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[chr(64 + index)].width = width
    output = io.BytesIO()
    workbook.save(output)
    return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename=pioneer-rooms-{month}.xlsx"})


@app.post("/calendar/bookings/<int:booking_id>/cancel")
def cancel_booking(booking_id):
    employee_id = require_current_employee()
    booking = query_one("SELECT rb.*,r.responsible_employee_id AS room_responsible FROM room_bookings rb JOIN rooms r ON r.id=rb.room_id WHERE rb.id=?", (booking_id,))
    if not booking:
        abort(404)
    if not employee_id or (booking["created_by"] != employee_id and booking["room_responsible"] != employee_id and not employee_can("rooms", employee_id)):
        flash("Отменить бронирование может автор или ответственный за помещение.", "error")
    else:
        responsible_ids = [row["employee_id"] for row in query_all("SELECT employee_id FROM room_booking_responsibles WHERE booking_id=?", (booking_id,))]
        db = get_db()
        notify_entity(db, responsible_ids, "room_booking", booking_id, "room-booking-cancelled", f"Бронирование «{booking['title']}» на {ru_date(booking['booking_date'])} отменено.", url_for("meetings", room_id=booking["room_id"], selected_date=booking["booking_date"]))
        db.execute("DELETE FROM room_bookings WHERE id=?", (booking_id,))
        db.commit()
        flash("Бронирование отменено.", "success")
    return redirect(url_for("meetings", room_id=booking["room_id"]))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=request.args.get("download") == "1")


@app.errorhandler(413)
def file_too_large(_error):
    flash("Файл слишком большой. Максимальный размер — 16 МБ.", "error")
    return redirect(request.referrer or url_for("dashboard"))


init_database()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
