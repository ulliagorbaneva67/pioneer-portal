import os
import csv
import io
import shutil
import sqlite3
import uuid
from datetime import date, datetime, timedelta
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
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "pioneer.db"
UPLOAD_DIR = BASE_DIR / "uploads"
BACKUP_DIR = BASE_DIR / "backups"

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
DOCUMENT_EXTENSIONS = IMAGE_EXTENSIONS | {"pdf", "doc", "docx", "xls", "xlsx", "txt", "zip"}

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
)


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

CREATE TABLE IF NOT EXISTS likes (
    post_id INTEGER NOT NULL,
    employee_id INTEGER NOT NULL,
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
    created_at TEXT NOT NULL
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
    task_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(employee_id, task_id, kind),
    FOREIGN KEY (employee_id) REFERENCES employees(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
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
    if employee["portal_role"] in LEADERSHIP_ROLES:
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
    return {row["id"] for row in rows}


def history_event(db, task_id, event, new_names, reason="", previous_names=""):
    db.execute(
        "INSERT INTO task_history (task_id, event, previous_names, new_names, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, event, previous_names, new_names, reason, datetime.now().isoformat(timespec="minutes")),
    )


def find_replacement(employee, role_type, excluded=()):
    excluded = set(excluded) | {employee["id"]}
    if employee["substitute_id"]:
        chosen = query_one(
            "SELECT * FROM employees WHERE id = ? AND is_dismissed = 0 AND presence_status NOT IN ('vacation','sick','trip','training')",
            (employee["substitute_id"],),
        )
        if chosen and chosen["id"] not in excluded:
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
            continue
        if role_type == "approver":
            db.execute("UPDATE task_approvers SET current_employee_id = ?, decision = 'Ожидает', comment = '', decided_at = NULL WHERE task_id = ? AND current_employee_id = ?", (replacement["id"], task_id, employee_id))
            action = "Проверку задач временно осуществляет"
        else:
            db.execute(f"DELETE FROM {table} WHERE task_id = ? AND employee_id = ?", (task_id, employee_id))
            db.execute(f"INSERT OR IGNORE INTO {table} (task_id, employee_id) VALUES (?, ?)", (task_id, replacement["id"]))
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
            raise ValueError("Для статуса отсутствия укажите даты начала и окончания.")
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if end < start:
            raise ValueError("Дата окончания отсутствия не может быть раньше даты начала.")
        db.execute("UPDATE employees SET presence_status = ?, absence_start = ?, absence_end = ? WHERE id = ?", (status, start_date, end_date, employee_id))
        db.execute("INSERT INTO employee_absences (employee_id, status_key, start_date, end_date, created_at) VALUES (?, ?, ?, ?, ?)", (employee_id, status, start_date, end_date, datetime.now().isoformat(timespec="minutes")))
        apply_absence_substitutions(db, employee_id, status, start_date, end_date)
    else:
        if employee and employee["absence_start"]:
            restore_employee_substitutions(db, employee_id)
            db.execute("UPDATE employee_absences SET returned_at = ? WHERE employee_id = ? AND returned_at IS NULL", (datetime.now().isoformat(timespec="minutes"), employee_id))
        db.execute("UPDATE employees SET presence_status = ?, absence_start = NULL, absence_end = NULL WHERE id = ?", (status, employee_id))


def notify(db, employee_ids, task_id, kind, text):
    """Уникальная пара адресат/задача/событие не допускает повторных оповещений."""
    now = datetime.now().isoformat(timespec="minutes")
    for employee_id in set(employee_ids):
        db.execute(
            """INSERT OR IGNORE INTO notifications (employee_id, task_id, kind, text, created_at)
               SELECT id, ?, ?, ?, ? FROM employees WHERE id = ? AND is_dismissed = 0""",
            (task_id, kind, text, now, employee_id),
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
        return "review", "На проверке"
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
    add_column("posts", "subject_employee_id INTEGER")
    add_column("meetings", "event_type TEXT NOT NULL DEFAULT 'meeting'")
    add_column("meetings", "room_id INTEGER")
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
    if "rooms" not in existing_tables:
        connection.executemany("INSERT OR IGNORE INTO rooms (name) VALUES (?)", [("Актовый зал",), ("Конференц-зал",)])
    connection.executemany(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
        [("portal_name", "ПИОНЕР. Внутренний портал"), ("work_start", "09:00"), ("work_end", "18:00"),
         ("notification_sound", "1"), ("analytics_period", "month"), ("high_result_threshold", "90"),
         ("overdue_threshold", "20"), ("sick_days_threshold", "10"), ("work_weekends", ""),
         ("primary_color", "#146c4b"), ("portal_logo", "")],
    )
    connection.execute(
        """INSERT INTO employee_positions (employee_id, position_name, rate, is_primary)
           SELECT e.id, e.position, 1.0, 1 FROM employees e
           WHERE NOT EXISTS (SELECT 1 FROM employee_positions ep WHERE ep.employee_id = e.id AND ep.is_primary = 1)"""
    )
    connection.execute(
        "INSERT OR IGNORE INTO task_departments (task_id, department_id, is_primary) SELECT id, department_id, 1 FROM tasks"
    )
    # Роли выводятся из должности один раз; затем их можно уточнить в настройках.
    for employee in connection.execute("SELECT id, position, portal_role FROM employees"):
        if employee[2] != "employee":
            continue
        position = employee[1].casefold()
        role = "director" if "директор" in position and "зам" not in position else "deputy" if "директор" in position and "зам" in position else "head" if any(word in position for word in ("началь", "завед", "руковод")) else "employee"
        connection.execute("UPDATE employees SET portal_role = ? WHERE id = ?", (role, employee[0]))
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
        expired = query_all("SELECT id FROM employees WHERE absence_end IS NOT NULL AND absence_end < ?", (date.today().isoformat(),))
        db = get_db()
        for employee in expired:
            update_employee_presence(db, employee["id"], "online")
        if expired:
            db.commit()
        # Просрочку создаём при открытии любой страницы, без фонового сервера.
        overdue = query_all("SELECT id, title FROM tasks WHERE status = 'В работе' AND deadline < ?", (datetime.now().isoformat(timespec="minutes"),))
        db = get_db()
        for task in overdue:
            recipients = [person["id"] for role in ("assignees", "observers") for person in task_people(task["id"], role)]
            notify(db, recipients, task["id"], "overdue", f"Просрочена задача: {task['title']}")
        db.commit()
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
        "portal_logo": app_options.get("portal_logo", ""), "primary_color": app_options.get("primary_color", "#146c4b"),
        "can_analytics": employee_can("analytics") if current_employee else False,
        "can_settings": employee_can("settings") if current_employee else False,
        "can_employees_admin": employee_can("employees_admin") if current_employee else False,
        "can_rates": employee_can("rates") if current_employee else False,
        "notification_sound": app_options.get("notification_sound", "1") == "1",
    }


@app.template_filter("ru_datetime")
def ru_datetime(value):
    if not value:
        return "—"
    return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")


@app.template_filter("ru_date")
def ru_date(value):
    if not value:
        return "—"
    return date.fromisoformat(value).strftime("%d.%m.%Y")


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
    active = task_rows("В работе")
    stats = {
        "active": len(active),
        "overdue": sum(1 for _row, state, _label in active if state == "overdue"),
        "completed": query_one("SELECT COUNT(*) AS count FROM tasks WHERE status = 'Выполнено'")["count"],
    }
    posts = load_posts(limit=3)
    meetings = query_all(
        "SELECT * FROM meetings WHERE meeting_at >= ? ORDER BY meeting_at LIMIT 4",
        (datetime.now().isoformat(timespec="minutes"),),
    )
    today_meetings = query_all(
        "SELECT * FROM meetings WHERE meeting_at >= ? AND meeting_at < ? ORDER BY meeting_at",
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
    current_id = session.get("current_employee_id")
    my_today_tasks = [item for item in active if current_id and any(person["id"] == current_id for person in task_people(item[0]["id"], "assignees")) and item[0]["deadline"][:10] <= today.isoformat()]
    return render_template(
        "dashboard.html", birthdays=birthdays, stats=stats, tasks=active[:5], posts=posts,
        meetings=meetings, today_meetings=today_meetings, important_posts=important_posts, task_avatars=task_avatars,
        my_today_tasks=my_today_tasks,
    )


@app.post("/current-employee")
def set_current_employee():
    employee_id = request.form.get("employee_id", type=int)
    if employee_id and query_one("SELECT id FROM employees WHERE id = ? AND is_dismissed = 0", (employee_id,)):
        session["current_employee_id"] = employee_id
    else:
        session.pop("current_employee_id", None)
    return redirect(request.referrer or url_for("dashboard"))


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
    return render_template("profile.html", employee=employee, statuses=statuses, my_tasks=my_tasks)


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
    rows = task_rows("В работе")
    current_id = session.get("current_employee_id")
    if current_id:
        rows = [item for item in rows if task_is_visible(item[0]["id"], current_id)]
    people = {task["id"]: (task_people(task["id"], "assignees"), task_people(task["id"], "observers")) for task, _state, _label in rows}
    return render_template("tasks.html", tasks=rows, people=people, title="Задачи в работе", completed=False)


@app.route("/tasks/completed")
def completed_tasks():
    rows = task_rows("Выполнено")
    current_id = session.get("current_employee_id")
    if current_id:
        rows = [item for item in rows if task_is_visible(item[0]["id"], current_id)]
    people = {task["id"]: (task_people(task["id"], "assignees"), task_people(task["id"], "observers")) for task, _state, _label in rows}
    return render_template("tasks.html", tasks=rows, people=people, title="Выполненные задачи", completed=True)


def task_form_data():
    departments = query_all("SELECT * FROM departments ORDER BY name")
    employees = active_employees()
    return departments, employees, default_approvers()


def department_heads(department_ids):
    return [
        employee["id"] for employee in active_employees()
        if employee["department_id"] in set(department_ids) and employee["portal_role"] == "head"
    ]


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
    if task_id:
        old = query_one("SELECT deadline FROM tasks WHERE id = ?", (task_id,))
        if old and datetime.fromisoformat(old["deadline"]) < datetime.now() and old["deadline"] != data["deadline"] and not data["overdue_reason"]:
            return "При продлении просроченной задачи обязательно укажите причину."
    return None


def save_task_relations(db, task_id, data, replace=False):
    if replace:
        for table in ("task_assignees", "task_observers", "task_departments", "task_approvers", "task_positions"):
            db.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
    db.executemany("INSERT OR IGNORE INTO task_assignees VALUES (?, ?)", [(task_id, item) for item in data["assignees"]])
    db.executemany("INSERT OR IGNORE INTO task_observers VALUES (?, ?)", [(task_id, item) for item in data["observers"]])
    db.execute("INSERT OR IGNORE INTO task_departments VALUES (?, ?, 1)", (task_id, data["department_id"]))
    db.executemany("INSERT OR IGNORE INTO task_departments VALUES (?, ?, 0)", [(task_id, item) for item in data["helper_departments"] if item != data["department_id"]])
    db.executemany(
        "INSERT INTO task_approvers (task_id, original_employee_id, current_employee_id) VALUES (?, ?, ?)",
        [(task_id, item, item) for item in data["approvers"]],
    )
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
                cursor = db.execute(
                    """INSERT INTO tasks
                       (title, description, department_id, created_date, deadline, creator_id, workflow_status, priority, critical_time, overdue_reason)
                       VALUES (?, ?, ?, ?, ?, ?, 'Новая', ?, ?, ?)""",
                    (data["title"], data["description"], data["department_id"], date.today().isoformat(), data["deadline"],
                     session.get("current_employee_id"), data["priority"], data["critical_time"] or None, data["overdue_reason"]),
                )
                task_id = cursor.lastrowid
                save_task_relations(db, task_id, data)
                history_event(db, task_id, "initial", people_names(task_people(task_id, "assignees")), "Задача создана")
                notify_assignments(db, task_id, data["title"], data["assignees"], data["observers"])
                db.commit()
                flash("Задача создана.", "success")
                return redirect(url_for("tasks"))
            except (ValueError, sqlite3.IntegrityError):
                flash("Проверьте корректность введённых данных.", "error")
    selected_approvers = parse_id_list(request.form.getlist("approvers")) if request.method == "POST" else [e["id"] for e in default_selected_approvers]
    positions_by_employee = {employee["id"]: query_all("SELECT * FROM employee_positions WHERE employee_id = ? ORDER BY is_primary DESC, id", (employee["id"],)) for employee in employees}
    return render_template(
        "task_form.html", task=None, departments=departments, employees=employees,
        selected_assignees=parse_id_list(request.form.getlist("assignees")), selected_observers=parse_id_list(request.form.getlist("observers")),
        selected_approvers=selected_approvers, helper_departments=parse_id_list(request.form.getlist("helper_departments")),
        positions_by_employee=positions_by_employee, source_message=source_message,
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
                db.execute(
                    """UPDATE tasks SET title = ?, description = ?, department_id = ?, deadline = ?, priority = ?,
                       critical_time = ?, overdue_reason = ? WHERE id = ?""",
                    (data["title"], data["description"], data["department_id"], data["deadline"], data["priority"],
                     data["critical_time"] or None, data["overdue_reason"], task_id),
                )
                save_task_relations(db, task_id, data, replace=True)
                if set(previous_assignees) != set(selected_assignees):
                    history_event(db, task_id, "reassign", people_names(task_people(task_id, "assignees")), "Изменено при редактировании задачи", previous_names)
                notify(db, set(selected_assignees) - set(previous_assignees), task_id, f"assigned-edit-{uuid.uuid4().hex}", f"Вам назначена задача: {data['title']}")
                notify(db, set(selected_observers) - set(previous_observers), task_id, f"observing-edit-{uuid.uuid4().hex}", f"Вас добавили в наблюдатели задачи: {data['title']}")
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
    return render_template(
        "task_form.html", task=task, departments=departments, employees=employees,
        selected_assignees=selected_assignees, selected_observers=selected_observers, selected_approvers=selected_approvers,
        helper_departments=helper_departments, history=history, current_assignees=people_names(task_people(task_id, "assignees")),
        active_assignees=active_employees(), approver_rows=task_approver_rows(task_id), comments=comments, files=files,
        substitutions=substitutions, positions_by_employee=positions_by_employee, source_message="",
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
        if datetime.fromisoformat(task["deadline"]) < datetime.now() and not (reason or task["overdue_reason"]):
            flash("Для просроченной задачи укажите причину перед отправкой на проверку.", "error")
            return redirect(url_for("edit_task", task_id=task_id))
        approvers = task_approver_rows(task_id)
        if len(approvers) != 2:
            flash("В задаче должны быть указаны два согласующих.", "error")
            return redirect(url_for("edit_task", task_id=task_id))
        db = get_db()
        db.execute("UPDATE tasks SET workflow_status = 'На проверке', overdue_reason = ? WHERE id = ?", (reason or task["overdue_reason"], task_id))
        db.execute("UPDATE task_approvers SET decision = 'Ожидает', comment = '', decided_at = NULL WHERE task_id = ?", (task_id,))
        notify(db, [row["current_employee_id"] for row in approvers], task_id, f"review-{uuid.uuid4().hex}", f"Задача «{task['title']}» ожидает вашего решения")
        notify(db, [person["id"] for person in task_people(task_id, "observers")], task_id, f"submitted-{uuid.uuid4().hex}", f"Исполнитель отправил задачу «{task['title']}» на проверку")
        history_event(db, task_id, "review", "На проверке", "Исполнитель отправил результат")
        db.commit()
        flash("Задача отправлена двум согласующим на проверку.", "success")
    return redirect(url_for("edit_task", task_id=task_id))


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
        notify(db, recipients, task_id, f"completed-{uuid.uuid4().hex}", f"Задача «{task['title']}» согласована и закрыта")
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
        history_event(db, task_id, "comment", text, query_one("SELECT full_name FROM employees WHERE id = ?", (employee_id,))["full_name"])
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
        uploaded = save_upload(request.files.get("attachment"), DOCUMENT_EXTENSIONS)
        if not uploaded:
            raise ValueError("Выберите файл для загрузки.")
        version = query_one("SELECT COALESCE(MAX(version), 0) + 1 AS value FROM task_files WHERE task_id = ? AND original_name = ?", (task_id, uploaded[1]))["value"]
        db = get_db()
        db.execute("INSERT INTO task_files (task_id, author_id, stored_name, original_name, version, created_at) VALUES (?, ?, ?, ?, ?, ?)", (task_id, employee_id, uploaded[0], uploaded[1], version, datetime.now().isoformat(timespec="minutes")))
        history_event(db, task_id, "file", f"{uploaded[1]} (версия {version})", query_one("SELECT full_name FROM employees WHERE id = ?", (employee_id,))["full_name"])
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
    return render_template("employees.html", employees=rows, grouped_employees=grouped, presence_statuses=catalog_rows("statuses"), departments=catalog_rows("departments"))


@app.get("/employees/<int:employee_id>")
def employee_card(employee_id):
    employee = query_one("""SELECT e.*, d.name AS department_name, s.label AS status_label, s.color AS status_color
        FROM employees e JOIN departments d ON d.id=e.department_id LEFT JOIN presence_options s ON s.key=e.presence_status
        WHERE e.id=?""", (employee_id,))
    if not employee:
        abort(404)
    positions = query_all("SELECT * FROM employee_positions WHERE employee_id=? ORDER BY is_primary DESC,id", (employee_id,))
    return render_template("employee_card.html", employee=employee, positions=positions)


@app.post("/employees/<int:employee_id>/dismiss")
def dismiss_employee(employee_id):
    if not require_feature("employees_admin"):
        return redirect(url_for("employees"))
    employee = query_one("SELECT * FROM employees WHERE id = ? AND is_dismissed = 0", (employee_id,))
    if not employee:
        abort(404)
    db = get_db()
    db.execute("UPDATE employees SET is_dismissed = 1 WHERE id = ?", (employee_id,))
    db.execute(
        "INSERT INTO posts (author_id, post_type, text, created_at) VALUES (?, 'Обычный', ?, ?)",
        (employee_id, f"{employee['full_name']} покинул нашу команду. Желаем успехов на новом месте!", datetime.now().isoformat(timespec="minutes")),
    )
    db.commit()
    if session.get("current_employee_id") == employee_id:
        session.pop("current_employee_id", None)
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
    substitutes = active_employees()
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        department_id = request.form.get("department_id", type=int)
        position = request.form.get("position", "").strip()
        birth_date = request.form.get("birth_date", "")
        if not full_name or not valid_catalog_value("departments", department_id) or not valid_catalog_value("positions", position, "name") or not birth_date:
            flash("Заполните все обязательные поля.", "error")
        else:
            try:
                date.fromisoformat(birth_date)
                if not employee and not statuses_available:
                    flash("Добавьте первый статус присутствия в Настройках.", "error")
                    return render_template("employee_form.html", employee=employee, departments=departments, positions=positions, statuses_available=False, additional_positions=additional_positions, substitutes=substitutes, can_rates=employee_can("rates"))
                photo = employee["photo"] if employee else None
                uploaded = save_upload(request.files.get("photo"), IMAGE_EXTENSIONS)
                if uploaded:
                    photo = uploaded[0]
                db = get_db()
                if employee:
                    db.execute(
                        "UPDATE employees SET full_name = ?, department_id = ?, position = ?, birth_date = ?, photo = ? WHERE id = ?",
                        (full_name, department_id, position, birth_date, photo, employee_id),
                    )
                else:
                    available_statuses = catalog_rows("statuses")
                    db.execute(
                        "INSERT INTO employees (full_name, department_id, position, birth_date, photo, presence_status) VALUES (?, ?, ?, ?, ?, ?)",
                        (full_name, department_id, position, birth_date, photo, available_statuses[0]["key"] if not valid_catalog_value("statuses", "offline") else "offline"),
                    )
                    employee_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                    if first_employee:
                        db.execute("UPDATE employees SET portal_role='admin' WHERE id=?", (employee_id,))
                        session["current_employee_id"] = employee_id
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
                db.execute("UPDATE employees SET substitute_id = ?, free_slots = ? WHERE id = ?", (request.form.get("substitute_id", type=int), request.form.get("free_slots", "").strip(), employee_id))
                db.commit()
                flash("Данные сотрудника сохранены.", "success")
                return redirect(url_for("employees"))
            except (ValueError, sqlite3.IntegrityError) as error:
                flash(str(error) if isinstance(error, ValueError) else "Не удалось сохранить сотрудника.", "error")
    return render_template("employee_form.html", employee=employee, departments=departments, positions=positions, statuses_available=statuses_available, additional_positions=additional_positions, substitutes=substitutes, can_rates=employee_can("rates"))


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
    for post in posts:
        result.append(
            {
                "post": post,
                "attachments": query_all("SELECT * FROM post_attachments WHERE post_id = ?", (post["id"],)),
                "comments": query_all(
                    """SELECT c.*, e.full_name AS author_name FROM comments c
                       JOIN employees e ON e.id = c.author_id WHERE c.post_id = ? ORDER BY c.created_at""",
                    (post["id"],),
                ),
                "liked": bool(query_one("SELECT 1 FROM likes WHERE post_id = ? AND employee_id = ?", (post["id"], current_id))),
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
        author_id = request.form.get("author_id", type=int)
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
    return render_template("post_form.html", employees=employees, departments=departments, post_types=post_types)


@app.post("/feed/<int:post_id>/like")
def toggle_like(post_id):
    employee_id = require_current_employee()
    if employee_id:
        db = get_db()
        existing = query_one("SELECT 1 FROM likes WHERE post_id = ? AND employee_id = ?", (post_id, employee_id))
        if existing:
            db.execute("DELETE FROM likes WHERE post_id = ? AND employee_id = ?", (post_id, employee_id))
        else:
            db.execute("INSERT OR IGNORE INTO likes VALUES (?, ?)", (post_id, employee_id))
        db.commit()
    return redirect(request.referrer or url_for("feed"))


@app.post("/feed/<int:post_id>/comment")
def add_comment(post_id):
    employee_id = require_current_employee()
    text = request.form.get("text", "").strip()
    if employee_id and text and query_one("SELECT id FROM posts WHERE id = ?", (post_id,)):
        get_db().execute(
            "INSERT INTO comments (post_id, author_id, text, created_at) VALUES (?, ?, ?, ?)",
            (post_id, employee_id, text, datetime.now().isoformat(timespec="minutes")),
        )
        get_db().commit()
    elif employee_id:
        flash("Комментарий не может быть пустым.", "error")
    return redirect(request.referrer or url_for("feed"))


def period_start(period):
    today = date.today()
    if period == "week":
        return today - timedelta(days=6)
    if period == "month":
        return today - timedelta(days=29)
    if period == "six_months":
        return today - timedelta(days=183)
    return date(2000, 1, 1)


def employee_efficiency(employee_id, start):
    employee = query_one("""SELECT e.*, d.name AS department_name, s.label AS status_label FROM employees e
        JOIN departments d ON d.id=e.department_id LEFT JOIN presence_options s ON s.key=e.presence_status WHERE e.id=?""", (employee_id,))
    tasks = query_all("""SELECT t.* FROM tasks t JOIN task_assignees ta ON ta.task_id=t.id
        WHERE ta.employee_id=? AND t.created_date>=? ORDER BY t.deadline""", (employee_id, start.isoformat()))
    completed = [task for task in tasks if task["status"] == "Выполнено"]
    on_time = [task for task in completed if task["completed_at"] and task["completed_at"] <= task["deadline"]]
    overdue = [task for task in tasks if (task["status"] != "Выполнено" and task["deadline"] < datetime.now().isoformat(timespec="minutes")) or (task["completed_at"] and task["completed_at"] > task["deadline"])]
    absences = query_all("SELECT * FROM employee_absences WHERE employee_id=? AND status_key IN ('vacation','sick') AND end_date>=?", (employee_id, start.isoformat()))
    absence_days = sum((min(date.today(), date.fromisoformat(row["end_date"])) - max(start, date.fromisoformat(row["start_date"]))).days + 1 for row in absences if min(date.today(), date.fromisoformat(row["end_date"])) >= max(start, date.fromisoformat(row["start_date"])))
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
        week_end = date.today() - timedelta(days=index * 7)
        week_start = week_end - timedelta(days=6)
        weeks.append({
            "label": f"{week_start.strftime('%d.%m')}–{week_end.strftime('%d.%m')}",
            "completed": sum(1 for task in completed if task["completed_at"] and week_start.isoformat() <= task["completed_at"][:10] <= week_end.isoformat()),
            "overdue": sum(1 for task in overdue if week_start.isoformat() <= (task["completed_at"] or task["deadline"])[:10] <= week_end.isoformat()),
        })
    roles = query_all("""SELECT ep.position_name, ep.rate, ep.project, COUNT(CASE WHEN t.status='Выполнено' THEN 1 END) AS completed
        FROM employee_positions ep LEFT JOIN task_positions tp ON tp.employee_position_id=ep.id
        LEFT JOIN tasks t ON t.id=tp.task_id AND t.created_date>=? WHERE ep.employee_id=?
        GROUP BY ep.id ORDER BY ep.is_primary DESC,ep.id""", (start.isoformat(), employee_id))
    return {"employee": employee, "tasks": tasks, "total": total, "completed": completed, "on_time": len(on_time), "overdue": len(overdue), "on_time_percent": on_time_percent, "overdue_percent": overdue_percent, "absence_days": absence_days, "recommendation": recommendation, "weeks": weeks, "roles": roles}


def analytics_data(period):
    start = period_start(period)
    departments = []
    for department in catalog_rows("departments"):
        tasks = query_all("SELECT * FROM tasks WHERE department_id=? AND created_date>=?", (department["id"], start.isoformat()))
        completed = sum(1 for task in tasks if task["status"] == "Выполнено")
        overdue = sum(1 for task in tasks if task["status"] != "Выполнено" and task["deadline"] < datetime.now().isoformat(timespec="minutes"))
        departments.append({"name": department["name"], "total": len(tasks), "completed": completed, "active": len(tasks) - completed, "overdue": overdue})
    departments.sort(key=lambda item: item["completed"], reverse=True)
    maximum = max((item["completed"] for item in departments), default=1) or 1
    for item in departments:
        item["height"] = max(6, round(item["completed"] * 100 / maximum))
    return departments, start


@app.get("/analytics")
def analytics():
    if not require_feature("analytics"):
        return redirect(url_for("profile"))
    period = request.args.get("period", "month")
    departments, start = analytics_data(period)
    employee_id = request.args.get("employee_id", type=int)
    profile = employee_efficiency(employee_id, period_start("six_months")) if employee_id else None
    ranking = []
    week_start = period_start("week")
    for employee in active_employees():
        data = employee_efficiency(employee["id"], week_start)
        ranking.append({"employee": employee, "completed": len(data["completed"]), "overdue": data["overdue"], "percent": round(len(data["completed"]) * 100 / data["total"]) if data["total"] else 0, "tasks": data["tasks"]})
    ranking.sort(key=lambda item: item["completed"], reverse=True)
    reports = query_all("""SELECT wr.*, e.full_name, d.name AS department_name FROM weekly_reports wr
        JOIN employees e ON e.id=wr.employee_id JOIN departments d ON d.id=e.department_id ORDER BY week_start DESC,e.full_name""")
    return render_template("analytics.html", departments=departments, period=period, start=start, employees=active_employees(), profile=profile, ranking=ranking, reports=reports)


@app.get("/analytics/export")
def export_analytics():
    if not require_feature("analytics"):
        return redirect(url_for("profile"))
    period = request.args.get("period", "month")
    departments, _start = analytics_data(period)
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
    start = period_start("week")
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ФИО", "Отдел", "Выполненные задачи", "Просроченные задачи и причины", "Процент выполнения"])
    for employee in active_employees():
        data = employee_efficiency(employee["id"], start)
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
    return redirect(url_for("edit_task", task_id=notification["task_id"]))


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


@app.post("/messages/create")
def create_chat():
    employee_id = require_current_employee()
    if not employee_id:
        return redirect(url_for("profile"))
    kind = request.form.get("kind")
    participants = set(parse_id_list(request.form.getlist("participants"))) - {employee_id}
    name = request.form.get("name", "").strip()
    link = request.form.get("call_link", "").strip()
    if kind not in {"private", "group"} or len(participants) < (1 if kind == "private" else 2) or (kind == "private" and len(participants) != 1) or (kind == "group" and not name) or any(not query_one("SELECT 1 FROM employees WHERE id = ? AND is_dismissed = 0", (item,)) for item in participants):
        flash("Для личного чата выберите одного коллегу; для группы — название и хотя бы двух коллег.", "error")
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
    rows = query_all(
        """SELECT m.*, GROUP_CONCAT(e.full_name) AS participant_names
           FROM meetings m
           LEFT JOIN meeting_participants mp ON mp.meeting_id = m.id
           LEFT JOIN employees e ON e.id = mp.employee_id
           GROUP BY m.id ORDER BY m.meeting_at DESC"""
    )
    now = datetime.now().isoformat(timespec="minutes")
    participants = {
        meeting["id"]: query_all(
            """SELECT e.* FROM meeting_participants mp JOIN employees e ON e.id = mp.employee_id
               WHERE mp.meeting_id = ? ORDER BY e.full_name""",
            (meeting["id"],),
        ) for meeting in rows
    }
    selected_room_id = request.args.get("room_id", type=int)
    rooms = catalog_rows("rooms")
    if not selected_room_id and rooms:
        selected_room_id = rooms[0]["id"]
    week_offset = request.args.get("week", 0, type=int)
    week_start = date.today() - timedelta(days=date.today().weekday()) + timedelta(days=week_offset * 7)
    days = [week_start + timedelta(days=index) for index in range(7)]
    bookings = query_all("""SELECT rb.*, d.name AS department_name, e.full_name AS responsible_name FROM room_bookings rb
        JOIN departments d ON d.id=rb.department_id JOIN employees e ON e.id=rb.responsible_employee_id
        WHERE rb.room_id=? AND rb.booking_date>=? AND rb.booking_date<=? ORDER BY rb.booking_date,rb.start_hour""", (selected_room_id, days[0].isoformat(), days[-1].isoformat())) if selected_room_id else []
    booking_map = {(row["booking_date"], hour): row for row in bookings for hour in range(row["start_hour"], row["end_hour"])}
    birthdays = query_all("SELECT * FROM employees WHERE is_dismissed=0 ORDER BY full_name")
    birthday_events = [(day, [employee for employee in birthdays if employee["birth_date"][5:] == day.isoformat()[5:]]) for day in days]
    selected_room = next((room for room in rooms if room["id"] == selected_room_id), None)
    return render_template("meetings.html", meetings=rows, now=now, participants=participants, rooms=rooms, selected_room=selected_room, days=days, hours=range(9, 21), booking_map=booking_map, week_offset=week_offset, departments=catalog_rows("departments"), employees=active_employees(), birthday_events=birthday_events, can_edit_rooms=bool(selected_room and (employee_can("rooms") or selected_room["responsible_employee_id"] == session.get("current_employee_id"))))


@app.route("/meetings/new", methods=["GET", "POST"])
@app.route("/calendar/new", methods=["GET", "POST"])
def new_meeting():
    employees = active_employees()
    event_types = catalog_rows("event-types")
    if request.method == "POST":
        topic = request.form.get("topic", "").strip()
        meeting_at = request.form.get("meeting_at", "")
        link = request.form.get("link", "").strip()
        notes = request.form.get("notes", "").strip()
        event_type = request.form.get("event_type", "meeting")
        participants = parse_id_list(request.form.getlist("participants"))
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
                    "INSERT INTO meetings (topic, meeting_at, link, notes, created_at, event_type) VALUES (?, ?, ?, ?, ?, ?)",
                    (topic, meeting_at, link, notes, datetime.now().isoformat(timespec="minutes"), event_type),
                )
                meeting_id = cursor.lastrowid
                db.executemany("INSERT INTO meeting_participants VALUES (?, ?)", [(meeting_id, item) for item in participants])
                db.commit()
                flash("Комната встречи создана.", "success")
                return redirect(url_for("meetings"))
            except (ValueError, sqlite3.IntegrityError):
                flash("Проверьте данные встречи.", "error")
    return render_template("meeting_form.html", employees=employees, event_types=event_types)


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
    department_id = request.form.get("department_id", type=int)
    responsible_id = request.form.get("responsible_employee_id", type=int)
    try:
        date.fromisoformat(booking_date)
        if not title or start_hour not in range(9, 21) or end_hour not in range(10, 22) or end_hour <= start_hour:
            raise ValueError
        overlap = query_one("SELECT 1 FROM room_bookings WHERE room_id=? AND booking_date=? AND start_hour < ? AND end_hour > ?", (room_id, booking_date, end_hour, start_hour))
        if overlap:
            flash("Выбранное время уже занято.", "error")
        else:
            get_db().execute("""INSERT INTO room_bookings (room_id,booking_date,start_hour,end_hour,title,department_id,responsible_employee_id,created_by,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (room_id, booking_date, start_hour, end_hour, title, department_id, responsible_id, employee_id, datetime.now().isoformat(timespec="minutes")))
            get_db().commit()
            flash("Зал забронирован.", "success")
    except (ValueError, sqlite3.IntegrityError):
        flash("Проверьте дату, время и обязательные поля бронирования.", "error")
    return redirect(url_for("meetings", room_id=room_id))


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
