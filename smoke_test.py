"""Быстрая проверка основных сценариев на временной копии базы."""

import shutil
import sqlite3
import tempfile
import re
from io import BytesIO
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import app as portal


def row_count(database, table, condition="1 = 1"):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {condition}").fetchone()[0]


with tempfile.TemporaryDirectory() as temp_directory:
    # Проверяем обновление старой базы, в которой ещё нет колонки статуса.
    old_database = Path(temp_directory) / "old.db"
    with closing(sqlite3.connect(old_database)) as old_connection:
        legacy_schema = portal.SCHEMA.replace("    presence_status TEXT NOT NULL DEFAULT 'offline',\n", "")
        legacy_schema = legacy_schema.replace("    is_dismissed INTEGER NOT NULL DEFAULT 0,\n", "")
        legacy_schema = re.sub(r"CREATE TABLE IF NOT EXISTS (?:positions|presence_options|post_types|task_history|notifications|chat_rooms|chat_members|chat_messages) \(.*?\);\n", "", legacy_schema, flags=re.S)
        legacy_schema = re.sub(r"CREATE INDEX IF NOT EXISTS .*?;\n", "", legacy_schema)
        old_connection.executescript(legacy_schema)
        old_connection.execute("INSERT INTO departments (name) VALUES ('Тестовый отдел')")
        old_connection.execute(
            "INSERT INTO employees (full_name, department_id, position, birth_date) VALUES ('Старый сотрудник', 1, 'Специалист', '1990-01-01')"
        )
        old_connection.commit()
    original_database, original_backups = portal.DATABASE, portal.BACKUP_DIR
    portal.DATABASE = old_database
    portal.BACKUP_DIR = Path(temp_directory) / "backups"
    portal.init_database()
    portal.init_database()  # Повторный запуск не должен сбрасывать сохранённые данные.
    with closing(sqlite3.connect(old_database)) as connection:
        assert connection.execute("SELECT presence_status FROM employees WHERE full_name = 'Старый сотрудник'").fetchone()[0] == "offline"
        assert connection.execute("SELECT is_dismissed FROM employees WHERE full_name = 'Старый сотрудник'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM chat_rooms WHERE kind = 'general'").fetchone()[0] == 1
        assert connection.execute("SELECT name FROM positions WHERE name = 'Специалист'").fetchone()[0] == "Специалист"
        assert connection.execute("SELECT label FROM presence_options WHERE key = 'offline'").fetchone()[0] == "🔴 Не в сети"
        assert connection.execute("SELECT label FROM post_types WHERE key = 'Важное объявление'").fetchone()[0] == "Важное объявление"
    portal.DATABASE, portal.BACKUP_DIR = original_database, original_backups

    test_database = Path(temp_directory) / "test.db"
    shutil.copy2(portal.DATABASE, test_database)
    portal.DATABASE = test_database
    original_upload_dir = portal.UPLOAD_DIR
    portal.UPLOAD_DIR = Path(temp_directory)
    client = portal.app.test_client()
    with closing(sqlite3.connect(test_database)) as connection:
        initial_status = connection.execute("SELECT presence_status FROM employees WHERE id = 1").fetchone()[0]
        first_photo = connection.execute("SELECT photo FROM employees WHERE id = 1").fetchone()[0]
        second_name = connection.execute("SELECT full_name FROM employees WHERE id = 2").fetchone()[0]

    # Профиль доступен только после выбора сотрудника; фото и статус принадлежат ему.
    assert "Выберите свой профиль" in client.get("/profile").data.decode("utf-8")
    client.post("/current-employee", data={"employee_id": "2"})
    profile_page = client.get("/profile").data.decode("utf-8")
    assert f"<h2>{second_name}</h2>" in profile_page
    assert "Дата рождения" in profile_page and "Должность" in profile_page and "Отдел" in profile_page
    assert 'id="current-employee"' not in client.get("/").data.decode("utf-8")
    response = client.post(
        "/profile",
        data={"employee_id": "1", "presence_status": "online", "photo": (BytesIO(b"GIF89a"), "avatar.gif")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    with closing(sqlite3.connect(test_database)) as connection:
        uploaded_photo, uploaded_status = connection.execute("SELECT photo, presence_status FROM employees WHERE id = 2").fetchone()
        assert connection.execute("SELECT photo, presence_status FROM employees WHERE id = 1").fetchone() == (first_photo, initial_status)
    assert uploaded_photo.endswith(".gif") and uploaded_status == "online"
    assert client.get(f"/uploads/{uploaded_photo}").status_code == 200
    assert f'/uploads/{uploaded_photo}' in client.get("/tasks").data.decode("utf-8")
    assert f'/uploads/{uploaded_photo}' in client.get("/profile").data.decode("utf-8")
    assert client.post("/profile", data={"presence_status": "offline", "photo": (BytesIO(b"bad"), "bad.exe")}, content_type="multipart/form-data").status_code == 200
    assert client.post("/profile", data={"presence_status": "unknown"}).status_code == 200
    with closing(sqlite3.connect(test_database)) as connection:
        assert connection.execute("SELECT photo, presence_status FROM employees WHERE id = 2").fetchone() == (uploaded_photo, "online")

    assert portal.birthday_genitive("Анна Петрова") == "Анны Петровой"
    assert portal.birthday_genitive("Петрова Анна Сергеевна") == "Петровой Анны Сергеевны"
    assert portal.birthday_genitive("Иван Соколов") == "Ивана Соколова"

    # Три вида событий показываются в обзоре одновременно, пустой день тоже отображается.
    now = datetime.now().replace(second=0, microsecond=0)
    with closing(sqlite3.connect(test_database)) as connection:
        connection.execute("UPDATE employees SET birth_date = '1990-01-01'")
        connection.execute("UPDATE posts SET created_at = '2020-01-01T10:00'")
        connection.execute("DELETE FROM meetings")
        connection.commit()
    assert "Сегодня важных событий не запланировано".encode() in client.get("/").data
    with closing(sqlite3.connect(test_database)) as connection:
        connection.execute("UPDATE employees SET full_name = 'Анна Петрова', birth_date = ? WHERE id = 1", (date.today().replace(year=1990).isoformat(),))
        connection.execute(
            "INSERT INTO meetings (topic, meeting_at, link, notes, created_at) VALUES (?, ?, ?, ?, ?)",
            ("Планёрка сегодня", now.isoformat(timespec="minutes"), "https://telemost.yandex.ru/test", "Тестовый комментарий", now.isoformat(timespec="minutes")),
        )
        connection.execute(
            "INSERT INTO posts (author_id, post_type, text, created_at) VALUES (1, 'Важное объявление', ?, ?)",
            ("Срочное объявление", now.isoformat(timespec="minutes")),
        )
        connection.commit()
    overview = client.get("/").data.decode("utf-8")
    assert "Сегодня день рождения у Анны Петровой!" in overview
    assert "Планёрка сегодня" in overview and "Срочное объявление" in overview
    assert 'href="https://telemost.yandex.ru/test"' in overview and "Подключиться" in overview
    assert "Тестовый комментарий" in client.get("/meetings").data.decode("utf-8")

    # Все разделы должны открываться без серверной ошибки.
    routes = ["/", "/tasks", "/tasks/completed", "/employees", "/feed", "/meetings", "/profile", "/tasks/new", "/meetings/new"]
    assert all(client.get(route).status_code == 200 for route in routes)
    assert client.get("/settings").status_code == 302
    assert client.get("/employees/new").status_code == 302
    assert client.get("/feed/new").status_code == 302
    period_page = client.get("/calendar?date_from=2026-08-31&date_to=2026-09-06").data.decode("utf-8")
    assert 'name="date_from" value="2026-08-31"' in period_page and 'name="date_to" value="2026-09-06"' in period_page

    deadline = (datetime.now() + timedelta(days=2)).isoformat(timespec="minutes")
    with closing(sqlite3.connect(test_database)) as connection:
        approver_ids = [row[0] for row in connection.execute("SELECT id FROM employees WHERE is_dismissed=0 ORDER BY CASE portal_role WHEN 'director' THEN 0 WHEN 'deputy' THEN 1 ELSE 2 END,id LIMIT 2")]
        connection.execute("INSERT INTO employee_positions (employee_id,position_name,rate,project,is_primary) VALUES (2,'Тестовая роль',0.5,'Проект ПИОНЕР',0)")
        task_position_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.commit()
    response = client.post(
        "/tasks/new",
        data={
            "title": "Тестовая задача",
            "description": "Проверка",
            "department_id": "1",
            "deadline": deadline,
            "assignees": "2",
            "observers": "1",
            "approvers": [str(item) for item in approver_ids],
            "priority": "Обычная",
            "position_2": str(task_position_id),
        },
    )
    assert response.status_code == 302

    with closing(sqlite3.connect(test_database)) as connection:
        task_id = connection.execute("SELECT MAX(id) FROM tasks").fetchone()[0]
        assert connection.execute("SELECT employee_position_id FROM task_positions WHERE task_id=? AND employee_id=2", (task_id,)).fetchone()[0] == task_position_id
    task_card = client.get(f"/tasks/{task_id}/edit").data.decode("utf-8")
    assert "Суть задачи (ТЗ)" in task_card and "История участников" not in task_card

    # Прошедший срок допустимо сохранить при редактировании задачи.
    past_deadline = (datetime.now() - timedelta(days=1)).isoformat(timespec="minutes")
    task_form = {"title": "Тестовая задача", "description": "Проверка", "department_id": "1", "deadline": past_deadline, "assignees": "2", "observers": "1", "approvers": [str(item) for item in approver_ids], "priority": "Обычная"}
    assert client.post(f"/tasks/{task_id}/edit", data=task_form).status_code == 302
    assert row_count(test_database, "tasks", f"id={task_id} AND deadline='{past_deadline}'") == 1
    task_form["deadline"] = deadline
    assert client.post(f"/tasks/{task_id}/edit", data=task_form).status_code == 302

    # Согласующий не может вернуть задачу до отправки на проверку.
    client.post("/current-employee", data={"employee_id": str(approver_ids[0])})
    client.post(f"/tasks/{task_id}/revision", data={"comment": "Рано", "new_deadline": deadline})
    assert row_count(test_database, "tasks", f"id = {task_id} AND workflow_status = 'Новая'") == 1

    # Посторонний сотрудник не может закрыть задачу.
    client.post("/current-employee", data={"employee_id": "3"})
    client.post(f"/tasks/{task_id}/complete")
    assert row_count(test_database, "tasks", f"id = {task_id} AND status = 'В работе'") == 1

    client.post("/current-employee", data={"employee_id": "2"})
    client.post(f"/tasks/{task_id}/start")
    client.post(f"/tasks/{task_id}/complete")
    assert row_count(test_database, "tasks", f"id = {task_id} AND status = 'В работе' AND workflow_status = 'На проверке'") == 1
    revision_deadline = (datetime.now() + timedelta(days=3)).isoformat(timespec="minutes")
    client.post("/current-employee", data={"employee_id": str(approver_ids[0])})
    assert "Закрыть задачу" in client.get(f"/tasks/{task_id}/edit").data.decode("utf-8")
    client.post(f"/tasks/{task_id}/revision", data={"comment": "Добавьте результат", "new_deadline": revision_deadline})
    assert row_count(test_database, "tasks", f"id = {task_id} AND workflow_status = 'На доработке' AND deadline = '{revision_deadline}'") == 1
    client.post("/current-employee", data={"employee_id": "2"})
    client.post(f"/tasks/{task_id}/complete")
    for approver_id in approver_ids:
        client.post("/current-employee", data={"employee_id": str(approver_id)})
        client.post(f"/tasks/{task_id}/approve")
    assert row_count(test_database, "tasks", f"id = {task_id} AND status = 'Выполнено'") == 1

    # Любой назначенный согласующий может закрыть задачу, когда она отправлена на проверку.
    client.post("/tasks/new", data={"title": "Закрытие согласующим", "department_id": "1", "deadline": deadline, "assignees": "2", "observers": "1", "approvers": [str(item) for item in approver_ids], "priority": "Обычная"})
    with closing(sqlite3.connect(test_database)) as connection:
        close_task_id = connection.execute("SELECT MAX(id) FROM tasks").fetchone()[0]
    client.post("/current-employee", data={"employee_id": "2"})
    client.post(f"/tasks/{close_task_id}/start")
    client.post(f"/tasks/{close_task_id}/complete")
    client.post("/current-employee", data={"employee_id": str(approver_ids[0])})
    assert client.post(f"/tasks/{close_task_id}/close").status_code == 302
    assert row_count(test_database, "tasks", f"id={close_task_id} AND status='Выполнено' AND workflow_status='Выполнено'") == 1

    # Ролевое представление: исполнитель видит только рабочую часть, согласующий — историю и управление сроком.
    client.post("/current-employee", data={"employee_id": "1"})
    role_deadline = (datetime.now() + timedelta(days=4)).isoformat(timespec="minutes")
    assert client.post("/tasks/new", data={"title": "Ролевая задача", "description": "Подготовить видеорезультат", "department_id": "2", "deadline": role_deadline, "assignees": "3", "observers": "5", "approvers": [str(item) for item in approver_ids], "priority": "Важная"}).status_code == 302
    with closing(sqlite3.connect(test_database)) as connection:
        role_task_id = connection.execute("SELECT MAX(id) FROM tasks").fetchone()[0]
    client.post("/current-employee", data={"employee_id": "3"})
    executor_card = client.get(f"/tasks/{role_task_id}/edit").data.decode("utf-8")
    assert "Суть задачи (ТЗ)" in executor_card and "История участников" not in executor_card
    assert "Принять задачу" not in executor_card and "Изменить дедлайн" not in executor_card
    client.post(f"/tasks/{role_task_id}/start")
    result_response = client.post(f"/tasks/{role_task_id}/complete", data={"result_comment": "Работа выполнена, видео приложено", "attachment": (BytesIO(b"video"), "result.mp4")}, content_type="multipart/form-data")
    assert result_response.status_code == 302
    assert row_count(test_database, "tasks", f"id={role_task_id} AND workflow_status='На проверке'") == 1
    assert row_count(test_database, "task_comments", f"task_id={role_task_id} AND author_id=3") == 1
    assert row_count(test_database, "task_files", f"task_id={role_task_id} AND author_id=3 AND original_name='result.mp4'") == 1
    review_card = client.get(f"/tasks/{role_task_id}/edit").data.decode("utf-8")
    assert "Результат находится на согласовании" in review_card and "История участников" not in review_card
    client.post("/current-employee", data={"employee_id": "5"})
    observer_card = client.get(f"/tasks/{role_task_id}/edit").data.decode("utf-8")
    assert "Результат находится на согласовании" in observer_card and "История участников" not in observer_card
    client.post("/employees/5/status", data={"presence_status": "sick", "absence_start": date.today().isoformat(), "absence_end": (date.today() + timedelta(days=1)).isoformat()})
    assert row_count(test_database, "task_substitutions", f"task_id={role_task_id} AND original_employee_id=5 AND role_type='observer' AND is_active=1") == 1
    assert row_count(test_database, "notifications", f"employee_id=3 AND task_id={role_task_id} AND kind LIKE 'substitution-%'") >= 1
    client.post("/current-employee", data={"employee_id": "3"})
    assert "Произошла замена участника" in client.get(f"/tasks/{role_task_id}/edit").data.decode("utf-8")
    client.post("/current-employee", data={"employee_id": "5"})
    client.post("/employees/5/status", data={"presence_status": "online"})
    client.post("/current-employee", data={"employee_id": "1"})
    approver_card = client.get(f"/tasks/{role_task_id}/edit").data.decode("utf-8")
    assert "История участников" in approver_card and "Изменить дедлайн" in approver_card and "Ручная замена участника" in approver_card and "result.mp4" in approver_card
    # Будущий отпуск не меняет роли заранее; согласователь может выполнить обоснованную ручную замену.
    client.post("/current-employee", data={"employee_id": "2"})
    future_start = (date.today() + timedelta(days=5)).isoformat()
    future_end = (date.today() + timedelta(days=7)).isoformat()
    client.post("/employees/2/status", data={"presence_status": "vacation", "absence_start": future_start, "absence_end": future_end})
    assert row_count(test_database, "employees", "id=2 AND presence_status!='vacation'") == 1
    assert row_count(test_database, "task_substitutions", f"task_id={role_task_id} AND original_employee_id=2") == 0
    client.post("/current-employee", data={"employee_id": "1"})
    assert client.post(f"/tasks/{role_task_id}/replace-participant", data={"role_type": "observer", "original_employee_id": "5", "replacement_employee_id": "2", "reason": "Наблюдатель недоступен"}).status_code == 302
    assert row_count(test_database, "task_observers", f"task_id={role_task_id} AND employee_id=2") == 1
    assert row_count(test_database, "task_substitutions", f"task_id={role_task_id} AND role_type='observer' AND source='manual' AND reason='Наблюдатель недоступен'") == 1
    assert row_count(test_database, "notifications", f"task_id={role_task_id} AND kind LIKE 'manual-substitution-%'") >= 1
    changed_deadline = (datetime.now() + timedelta(days=5)).isoformat(timespec="minutes")
    client.post(f"/tasks/{role_task_id}/deadline", data={"deadline": changed_deadline, "reason": "Дополнительная проверка"})
    assert row_count(test_database, "tasks", f"id={role_task_id} AND deadline='{changed_deadline}'") == 1
    revision_deadline = (datetime.now() + timedelta(days=6)).isoformat(timespec="minutes")
    client.post(f"/tasks/{role_task_id}/revision", data={"comment": "Исправьте титры", "new_deadline": revision_deadline})
    client.post("/current-employee", data={"employee_id": "3"})
    revision_card = client.get(f"/tasks/{role_task_id}/edit").data.decode("utf-8")
    assert "Задача возвращена на доработку" in revision_card and "Исправьте титры" in revision_card
    client.post(f"/tasks/{role_task_id}/complete", data={"result_comment": "Титры исправлены"})
    client.post("/current-employee", data={"employee_id": "1"})
    client.post(f"/tasks/{role_task_id}/close")
    assert row_count(test_database, "tasks", f"id={role_task_id} AND status='Выполнено'") == 1
    assert row_count(test_database, "notifications", f"employee_id=3 AND task_id={role_task_id} AND text LIKE '%перенесена в выполненные%'") >= 1

    client.post("/current-employee", data={"employee_id": str(approver_ids[0])})

    # Справочники защищают используемые данные и обновляют формы без перезапуска.
    client.post("/settings/departments/add", data={"name": "Отдел проектных инициатив"})
    with closing(sqlite3.connect(test_database)) as connection:
        department_id = connection.execute("SELECT id FROM departments WHERE name = 'Отдел проектных инициатив'").fetchone()[0]
        used_department = connection.execute("SELECT department_id FROM employees WHERE id = 1").fetchone()[0]
    assert "Отдел проектных инициатив" in client.get("/tasks/new").data.decode("utf-8")
    client.post(f"/settings/departments/{department_id}/rename", data={"name": "Проектные инициативы"})
    assert "Проектные инициативы" in client.get("/employees/new").data.decode("utf-8")
    assert client.post(f"/settings/departments/{used_department}/delete").status_code == 302
    assert row_count(test_database, "departments", f"id = {used_department}") == 1
    assert client.post(f"/settings/departments/{department_id}/delete").status_code == 302
    assert row_count(test_database, "departments", f"id = {department_id}") == 0

    client.post("/settings/positions/add", data={"name": "Специалист"})
    with closing(sqlite3.connect(test_database)) as connection:
        position_id = connection.execute("SELECT id FROM positions WHERE name = 'Специалист'").fetchone()[0]
    client.post(f"/settings/positions/{position_id}/rename", data={"name": "Главный специалист"})
    assert "Главный специалист" in client.get("/employees/new").data.decode("utf-8")

    # Все девять статусов выбираются, а чужой профиль и неверный код не изменяются.
    client.post("/current-employee", data={"employee_id": "2"})
    for status, label in portal.PRESENCE_STATUSES:
        status_data = {"presence_status": status}
        if status in portal.ABSENCE_KEYS:
            status_data.update(absence_start=date.today().isoformat(), absence_end=(date.today() + timedelta(days=2)).isoformat())
        response = client.post("/employees/2/status", data=status_data)
        assert response.status_code == 302
        with closing(sqlite3.connect(test_database)) as connection:
            assert connection.execute("SELECT presence_status FROM employees WHERE id = 2").fetchone()[0] == status
        assert label.encode("utf-8") in client.get("/employees").data
    client.post("/employees/1/status", data={"presence_status": "online"})
    client.post("/employees/2/status", data={"presence_status": "unknown"})
    with closing(sqlite3.connect(test_database)) as connection:
        assert connection.execute("SELECT presence_status FROM employees WHERE id = 1").fetchone()[0] == initial_status
        assert connection.execute("SELECT presence_status FROM employees WHERE id = 2").fetchone()[0] == "meeting"

    missing_period = client.post("/profile", data={"presence_status": "vacation"}).data.decode("utf-8")
    assert "Вы не указали период. Заполните, пожалуйста" in missing_period
    assert row_count(test_database, "employees", "id=2 AND presence_status='meeting'") == 1
    assert client.post("/profile", data={"presence_status": "vacation", "absence_start": date.today().isoformat(), "absence_end": (date.today() + timedelta(days=2)).isoformat()}).status_code == 302
    assert row_count(test_database, "employees", "id=2 AND presence_status='vacation'") == 1
    client.post("/profile", data={"presence_status": "online"})

    client.post("/current-employee", data={"employee_id": str(approver_ids[0])})
    client.post(
        "/employees/new",
        data={"full_name": "Тестовый Сотрудник", "department_id": "2", "position": "Главный специалист", "birth_date": "1990-01-01"},
    )
    assert row_count(test_database, "employees", "full_name = 'Тестовый Сотрудник'") == 1
    client.post("/current-employee", data={"employee_id": str(approver_ids[0])})
    client.post(f"/settings/positions/{position_id}/rename", data={"name": "Старший специалист"})
    assert row_count(test_database, "employees", "full_name = 'Тестовый Сотрудник' AND position = 'Старший специалист'") == 1
    assert client.post(f"/settings/positions/{position_id}/delete").status_code == 302
    assert row_count(test_database, "positions", f"id = {position_id}") == 1

    client.post(
        "/feed/new",
        data={"author_id": "1", "post_type": "Важное объявление", "text": "Тестовая публикация"},
    )
    with closing(sqlite3.connect(test_database)) as connection:
        post_id = connection.execute("SELECT MAX(id) FROM posts").fetchone()[0]
    client.post(f"/feed/{post_id}/like")
    client.post(f"/feed/{post_id}/comment", data={"text": "Тестовый комментарий"})
    assert row_count(test_database, "likes", f"post_id = {post_id}") == 1
    assert row_count(test_database, "comments", f"post_id = {post_id}") == 1

    # Типу сохраняем код: переименование не убирает оформление и событие дня.
    client.post("/settings/post-types/Важное объявление/rename", data={"name": "Срочно для команды"})
    assert "Срочно для команды" in client.get("/feed").data.decode("utf-8")
    assert "Тестовая публикация" in client.get("/").data.decode("utf-8")
    assert client.post("/settings/post-types/Важное объявление/delete").status_code == 302
    assert row_count(test_database, "post_types", "key = 'Важное объявление'") == 1

    client.post("/settings/post-types/add", data={"name": "Итоги недели"})
    with closing(sqlite3.connect(test_database)) as connection:
        custom_type = connection.execute("SELECT key FROM post_types WHERE label = 'Итоги недели'").fetchone()[0]
    assert "Итоги недели" in client.get("/feed/new").data.decode("utf-8")
    client.post("/feed/new", data={"author_id": "1", "post_type": custom_type, "text": "Отчёт"})
    assert row_count(test_database, "posts", f"post_type = '{custom_type}'") == 1
    assert client.post(f"/settings/post-types/{custom_type}/delete").status_code == 302
    assert row_count(test_database, "post_types", f"key = '{custom_type}'") == 1

    client.post("/settings/statuses/add", data={"name": "🎯 В проекте", "color": "#123abc"})
    with closing(sqlite3.connect(test_database)) as connection:
        custom_status = connection.execute("SELECT key FROM presence_options WHERE label = '🎯 В проекте'").fetchone()[0]
    client.post("/current-employee", data={"employee_id": "2"})
    client.post("/employees/2/status", data={"presence_status": custom_status})
    assert "🎯 В проекте" in client.get("/employees").data.decode("utf-8")
    client.post("/current-employee", data={"employee_id": str(approver_ids[0])})
    assert client.post(f"/settings/statuses/{custom_status}/delete").status_code == 302
    assert row_count(test_database, "presence_options", f"key = '{custom_status}'") == 1
    client.post(f"/settings/statuses/{custom_status}/rename", data={"name": "🎯 На проекте", "color": "#567abc"})
    assert "🎯 На проекте" in client.get("/employees").data.decode("utf-8")
    with closing(sqlite3.connect(test_database)) as connection:
        assert connection.execute("SELECT color FROM presence_options WHERE key = ?", (custom_status,)).fetchone()[0] == "#567abc"

    client.post("/settings/positions/add", data={"name": "СТАРШИЙ СПЕЦИАЛИСТ"})
    assert row_count(test_database, "positions", "name = 'СТАРШИЙ СПЕЦИАЛИСТ'") == 0

    client.post(
        "/meetings/new",
        data={"topic": "Тестовая встреча", "meeting_at": deadline, "event_type": "vks", "participants": ["1", "2"], "notes": "Проверка"},
    )
    assert row_count(test_database, "meetings", "topic = 'Тестовая встреча'") == 1
    with closing(sqlite3.connect(test_database)) as connection:
        room_id = connection.execute("SELECT id FROM rooms ORDER BY id LIMIT 1").fetchone()[0]
    booking_date = (date.today() + timedelta(days=90)).isoformat()
    booking = {"room_id": str(room_id), "booking_date": booking_date, "start_hour": "10", "end_hour": "12", "title": "Совещание", "department_ids": ["1", "2"], "responsible_employee_ids": ["1", "2"]}
    client.post("/calendar/rooms/book", data=booking)
    with closing(sqlite3.connect(test_database)) as connection:
        booking_id = connection.execute("SELECT MAX(id) FROM room_bookings WHERE room_id=?", (room_id,)).fetchone()[0]
    assert row_count(test_database, "room_booking_departments", f"booking_id={booking_id}") == 2
    assert row_count(test_database, "room_booking_responsibles", f"booking_id={booking_id}") == 2
    assert row_count(test_database, "notifications", f"entity_type='room_booking' AND entity_id={booking_id} AND kind='room-booking-created'") == 2
    booking.update(start_hour="11", end_hour="13", title="Пересечение")
    client.post("/calendar/rooms/book", data=booking)
    assert row_count(test_database, "room_bookings", f"room_id={room_id} AND booking_date='{booking_date}'") == 1

    # Цвета: приоритет завершённой задачи выше просрочки, далее срок меньше суток.
    assert portal.task_visual_state({"status": "Выполнено", "deadline": "2000-01-01T00:00"})[0] == "completed"
    assert portal.task_visual_state({"status": "В работе", "deadline": "2000-01-01T00:00"})[0] == "overdue"
    assert portal.task_visual_state({"status": "В работе", "deadline": (datetime.now() + timedelta(hours=4)).isoformat()})[0] == "soon"
    assert portal.task_visual_state({"status": "В работе", "deadline": (datetime.now() + timedelta(days=3)).isoformat()})[0] == "active"
    assert portal.full_years("2000-01-01") == date.today().year - 2000
    assert 'action="/tasks/' not in client.get("/tasks").data.decode("utf-8")
    with closing(sqlite3.connect(test_database)) as connection:
        new_employee_id = connection.execute("SELECT id FROM employees WHERE full_name = 'Тестовый Сотрудник'").fetchone()[0]

    overdue_deadline = (datetime.now() - timedelta(hours=2)).isoformat(timespec="minutes")
    assert client.post("/tasks/new", data={"title": "Проверка уведомлений", "department_id": "1", "deadline": overdue_deadline, "assignees": ["2", "3"], "observers": ["1"], "approvers": [str(item) for item in approver_ids], "priority": "Срочная"}).status_code == 302
    with closing(sqlite3.connect(test_database)) as connection:
        overdue_task_id = connection.execute("SELECT MAX(id) FROM tasks").fetchone()[0]
    assert row_count(test_database, "task_history", f"task_id = {overdue_task_id} AND event = 'initial'") == 1
    assert row_count(test_database, "notifications", f"task_id = {overdue_task_id} AND kind = 'assigned'") == 2
    assert row_count(test_database, "notifications", f"task_id = {overdue_task_id} AND kind = 'observing'") == 1
    client.post("/current-employee", data={"employee_id": "2"})
    proposed_deadline = (datetime.now() + timedelta(days=4)).isoformat(timespec="minutes")
    client.post(f"/tasks/{overdue_task_id}/extension-request", data={"comment": "Нужно дождаться ответа", "proposed_deadline": proposed_deadline})
    assert row_count(test_database, "task_history", f"task_id={overdue_task_id} AND event='extension-request'") == 1
    assert row_count(test_database, "notifications", f"task_id={overdue_task_id} AND kind LIKE 'extension-request-%'") >= 2
    client.get("/notifications")
    client.get("/notifications")
    assert row_count(test_database, "notifications", f"task_id = {overdue_task_id} AND kind = 'overdue'") == 3
    notification_page = client.get("/notifications").data.decode("utf-8")
    assert "Просрочена задача" in notification_page and "Отметить все как прочитанные" in notification_page
    with closing(sqlite3.connect(test_database)) as connection:
        own_notice = connection.execute("SELECT id FROM notifications WHERE task_id = ? AND employee_id = 2 AND kind = 'assigned'", (overdue_task_id,)).fetchone()[0]
    assert client.get(f"/notifications/{own_notice}/open").status_code == 302
    assert row_count(test_database, "notifications", f"id = {own_notice} AND is_read = 1") == 1
    assert client.post("/notifications/read-all").status_code == 302
    assert row_count(test_database, "notifications", "employee_id = 2 AND is_read = 0") == 0
    assert client.get("/notifications/999999/open").status_code == 404

    assert "initial:" not in client.get(f"/tasks/{overdue_task_id}/edit").data.decode("utf-8")
    client.post("/current-employee", data={"employee_id": str(approver_ids[0])})
    assert client.post(f"/tasks/{overdue_task_id}/reassign", data={"assignees": [str(new_employee_id), "3"], "reason": "Поменялись обязанности"}).status_code == 302
    assert row_count(test_database, "task_history", f"task_id = {overdue_task_id} AND event = 'reassign'") == 1
    assert "Поменялись обязанности" in client.get(f"/tasks/{overdue_task_id}/edit").data.decode("utf-8")

    # Отсутствующего исполнителя временно заменяют, не удаляя второго исполнителя задачи.
    client.post("/current-employee", data={"employee_id": "3"})
    client.post("/employees/3/status", data={
        "presence_status": "vacation",
        "absence_start": date.today().isoformat(),
        "absence_end": (date.today() + timedelta(days=2)).isoformat(),
        "substitute_id": str(new_employee_id),
    })
    assert row_count(test_database, "task_substitutions", f"task_id = {overdue_task_id} AND original_employee_id = 3 AND is_active = 1") == 1
    assert row_count(test_database, "task_assignees", f"task_id = {overdue_task_id} AND employee_id = 3") == 0
    assert row_count(test_database, "task_assignees", f"task_id = {overdue_task_id} AND employee_id = {new_employee_id}") == 1
    client.post("/employees/3/status", data={"presence_status": "online"})
    assert row_count(test_database, "task_substitutions", f"task_id = {overdue_task_id} AND original_employee_id = 3 AND is_active = 0") == 1
    assert row_count(test_database, "task_assignees", f"task_id = {overdue_task_id} AND employee_id = 3") == 1
    assert row_count(test_database, "task_assignees", f"task_id = {overdue_task_id} AND employee_id = {new_employee_id}") == 1

    client.post("/current-employee", data={"employee_id": str(approver_ids[0])})
    client.post(f"/tasks/{overdue_task_id}/complete")
    assert row_count(test_database, "tasks", f"id = {overdue_task_id} AND status = 'В работе'") == 1
    client.post("/current-employee", data={"employee_id": "3"})
    client.post(f"/tasks/{overdue_task_id}/start")
    assert client.post(f"/tasks/{overdue_task_id}/complete", data={"overdue_reason": "Ждали ответ коллеги"}).status_code == 302
    for approver_id in approver_ids:
        client.post("/current-employee", data={"employee_id": str(approver_id)})
        client.post(f"/tasks/{overdue_task_id}/approve")
    assert row_count(test_database, "tasks", f"id = {overdue_task_id} AND status = 'Выполнено'") == 1
    assert row_count(test_database, "notifications", f"task_id = {overdue_task_id} AND kind LIKE 'completed-%'") >= 1
    assert "task-completed" in client.get("/tasks/completed").data.decode("utf-8")

    # Чаты: общий, личный, групповой, вложение и ссылка для звонка.
    client.post("/current-employee", data={"employee_id": "2"})
    with closing(sqlite3.connect(test_database)) as connection:
        general_id = connection.execute("SELECT id FROM chat_rooms WHERE kind = 'general'").fetchone()[0]
    assert client.post(f"/messages/{general_id}/send", data={"text": "Привет всем!", "attachment": (BytesIO(b"file"), "report.txt")}, content_type="multipart/form-data").status_code == 302
    assert "Привет всем!" in client.get("/messages").data.decode("utf-8")
    assert client.post(f"/messages/{general_id}/call-link", data={"call_link": "https://telemost.yandex.ru/test"}).status_code == 302
    assert "Позвонить" in client.get("/messages").data.decode("utf-8")
    assert client.post("/messages/create", data={"kind": "private", "participants": str(new_employee_id)}).status_code == 302
    with closing(sqlite3.connect(test_database)) as connection:
        private_id = connection.execute("SELECT MAX(id) FROM chat_rooms WHERE kind = 'private'").fetchone()[0]
    assert client.post("/messages/create", data={"kind": "group", "name": "Тестовая группа", "participants": ["1", "3"]}).status_code == 302
    assert "Тестовая группа" in client.get("/messages").data.decode("utf-8")
    client.post("/current-employee", data={"employee_id": "1"})
    assert client.get(f"/messages/{private_id}").status_code == 404
    assert client.post(f"/messages/{private_id}/send", data={"text": "Не своё"}).status_code == 404

    # Увольнение не удаляет задачи, скрывает личный чат и публикует новость с комментариями.
    client.post(f"/employees/{new_employee_id}/edit", data={"full_name": "Тестовый Сотрудник", "department_id": "2", "position": "Старший специалист", "birth_date": "1990-01-01", "substitute_id": "2", "is_department_head": "1"})
    assert row_count(test_database, "employees", f"id={new_employee_id} AND portal_role='head' AND substitute_id=2") == 1
    assert row_count(test_database, "employees", "department_id=2 AND portal_role='head' AND is_dismissed=0") == 1
    employee_card = client.get(f"/employees/{new_employee_id}").data.decode("utf-8")
    assert "Сотрудник на замену" in employee_card and "Дата рождения" in employee_card and "лет" in employee_card
    assert client.post(f"/employees/{new_employee_id}/dismiss").status_code == 302
    assert row_count(test_database, "employees", f"id = {new_employee_id} AND is_dismissed = 1") == 1
    assert "Тестовый Сотрудник (уволен)" in client.get("/tasks/completed").data.decode("utf-8")
    assert "Тестовый Сотрудник покинул нашу команду. Желаем успехов на новом месте!" in client.get("/feed").data.decode("utf-8")
    client.post("/current-employee", data={"employee_id": "2"})
    assert client.get(f"/messages/{private_id}").status_code == 404
    with closing(sqlite3.connect(test_database)) as connection:
        dismissal_post_id = connection.execute("SELECT MAX(id) FROM posts").fetchone()[0]
    assert client.post(f"/feed/{dismissal_post_id}/comment", data={"text": "Удачи!"}).status_code == 302
    assert "Удачи!" in client.get("/feed").data.decode("utf-8")

    # Персональный доступ: email уникален, код хранится как хеш, вход требует код и просит подтвердить статус.
    client.post("/site-logout")
    client.post("/current-employee", data={"employee_id": "1"})
    with closing(sqlite3.connect(test_database)) as connection:
        connection.execute("UPDATE employees SET email='person@example.test' WHERE id=2")
        connection.commit()
    assert client.post("/employees/2/access-code").status_code == 302
    with closing(sqlite3.connect(test_database)) as connection:
        code, code_hash = connection.execute("SELECT access_code_display,access_code_hash FROM employees WHERE id=2").fetchone()
    assert code and code not in code_hash
    personal_client = portal.app.test_client()
    assert personal_client.post("/login", data={"email": "person@example.test", "code": "WRONG"}).status_code == 200
    assert personal_client.post("/login", data={"email": "person@example.test", "code": code}).status_code == 302
    login_page = personal_client.get("/").data.decode("utf-8")
    assert "Проверьте свой статус присутствия" in login_page and "Включите таймер рабочего дня" in login_page
    assert row_count(test_database, "notifications", "employee_id=2 AND kind LIKE 'login-reminder-%'") == 1
    personal_client.post("/profile/confirm-status", data={"presence_status": "online"})
    assert "Проверьте свой статус присутствия" not in personal_client.get("/").data.decode("utf-8")
    assert row_count(test_database, "workday_sessions", "employee_id=2 AND ended_at IS NULL") == 1
    with closing(sqlite3.connect(test_database)) as connection:
        connection.execute("""INSERT INTO notifications (employee_id,kind,text,created_at,entity_type,entity_id,action_url)
            VALUES (2,'workday-reminder','Скоро конец рабочего дня. Не забудьте выключить таймер и сменить статус.',?,'workday',999,'/profile')""", (datetime.now().isoformat(timespec="minutes"),))
        connection.commit()
    popup_status = personal_client.get("/notifications/status").get_json()
    assert popup_status["popup"]["text"] == "Скоро конец рабочего дня. Не забудьте выключить таймер и сменить статус."
    timer_page = personal_client.get("/profile").data.decode("utf-8")
    assert "Выключить таймер" in timer_page and "data-workday-timer" in timer_page and "data-started-at" in timer_page
    personal_client.post("/workday/stop")
    assert row_count(test_database, "workday_sessions", "employee_id=2 AND ended_at IS NOT NULL") == 1
    personal_client.post("/current-employee", data={"employee_id": "1"})
    assert second_name in personal_client.get("/profile").data.decode("utf-8")

    # Реакции и вложения комментариев сохраняются независимо от старого лайка.
    with closing(sqlite3.connect(test_database)) as connection:
        post_id = connection.execute("SELECT MIN(id) FROM posts").fetchone()[0]
    personal_client.post(f"/feed/{post_id}/reaction", data={"reaction": "thanks"})
    personal_client.post(f"/feed/{post_id}/comment", data={"text": "С файлом", "attachment": (BytesIO(b"note"), "note.txt")}, content_type="multipart/form-data")
    assert row_count(test_database, "post_reactions", f"post_id={post_id} AND employee_id=2 AND reaction='thanks'") == 1
    assert row_count(test_database, "comment_attachments") >= 1

    # Встреча уведомляет каждого выбранного участника, включая Ивана Соколова и Марину Цветаеву.
    meeting_at = (datetime.now() + timedelta(days=1)).isoformat(timespec="minutes")
    personal_client.post("/meetings/new", data={"topic": "Встреча нового формата", "meeting_at": meeting_at, "event_type": "meeting", "participants": ["2", "4"]})
    with closing(sqlite3.connect(test_database)) as connection:
        meeting_id = connection.execute("SELECT MAX(id) FROM meetings").fetchone()[0]
        assert connection.execute("SELECT organizer_id FROM meetings WHERE id=?", (meeting_id,)).fetchone()[0] == 2
        notified = {row[0] for row in connection.execute("SELECT employee_id FROM notifications WHERE entity_type='meeting' AND entity_id=? AND kind='meeting-created'", (meeting_id,))}
        assert {2, 4}.issubset(notified)
    personal_client.post(f"/meetings/{meeting_id}/recording", data={"recording": (BytesIO(b"record"), "recording.mp3")}, content_type="multipart/form-data")
    personal_client.post(f"/meetings/{meeting_id}/cancel")
    assert row_count(test_database, "meetings", f"id={meeting_id} AND status='Отменена' AND recording_stored_name IS NOT NULL") == 1
    assert row_count(test_database, "notifications", f"entity_type='meeting' AND entity_id={meeting_id}") >= 2

    # Архив требует передачи всех активных обязанностей и сохраняет историю сотрудника.
    client.post("/site-logout")
    client.post("/current-employee", data={"employee_id": "1"})
    transfer_deadline = (datetime.now() + timedelta(days=3)).isoformat(timespec="minutes")
    client.post("/tasks/new", data={"title": "Передача при увольнении", "department_id": "1", "deadline": transfer_deadline, "assignees": "3", "observers": "1", "approvers": [str(item) for item in approver_ids], "priority": "Обычная"})
    assert client.post("/employees/3/dismiss", data={"replacement_id": "2", "dismissal_reason": "Тест переноса"}).status_code == 302
    assert row_count(test_database, "employees", "id=3 AND is_dismissed=1") == 1
    assert row_count(test_database, "task_assignees", "employee_id=3 AND task_id IN (SELECT id FROM tasks WHERE status='В работе')") == 0
    assert row_count(test_database, "task_substitutions", "original_employee_id=3 AND source='dismissal' AND reason='Тест переноса'") >= 1
    assert "Тест переноса" in client.get("/employees/archive").data.decode("utf-8")

    # Руководство получает настоящие документы Excel и Word.
    selected_month = date.today().strftime("%Y-%m")
    analytics_page = client.get(f"/analytics?month={selected_month}")
    assert analytics_page.status_code == 200 and "Дашборд сотрудников" in analytics_page.data.decode("utf-8") and "Выбрать период в календаре" in analytics_page.data.decode("utf-8")
    excel = client.get(f"/analytics/export?month={selected_month}&format=xlsx")
    word = client.get(f"/analytics/weekly-export?month={selected_month}&format=docx")
    employee_excel = client.get("/employees/export")
    employee_word = client.get("/employees/export?format=docx")
    archive_excel = client.get("/employees/archive/export")
    archive_word = client.get("/employees/archive/export?format=docx")
    room_excel = client.get(f"/calendar/rooms/export?month={selected_month}")
    room_word = client.get(f"/calendar/rooms/export?month={selected_month}&format=docx")
    task_excel = client.get("/tasks/export?format=xlsx")
    task_word = client.get("/tasks/export?format=docx")
    message_excel = client.get(f"/messages/{general_id}/export")
    message_word = client.get(f"/messages/{general_id}/export?format=docx")
    settings_excel = client.get("/settings/export")
    settings_word = client.get("/settings/export?format=docx")
    assert excel.status_code == 200 and excel.data[:2] == b"PK"
    assert word.status_code == 200 and word.data[:2] == b"PK"
    assert employee_excel.status_code == 200 and employee_excel.data[:2] == b"PK"
    assert employee_word.status_code == 200 and employee_word.data[:2] == b"PK"
    assert archive_excel.status_code == 200 and archive_excel.data[:2] == b"PK"
    assert archive_word.status_code == 200 and archive_word.data[:2] == b"PK"
    assert room_excel.status_code == 200 and room_excel.data[:2] == b"PK"
    assert room_word.status_code == 200 and room_word.data[:2] == b"PK"
    assert task_excel.status_code == 200 and task_excel.data[:2] == b"PK"
    assert task_word.status_code == 200 and task_word.data[:2] == b"PK"
    assert message_excel.status_code == 200 and message_excel.data[:2] == b"PK"
    assert message_word.status_code == 200 and message_word.data[:2] == b"PK"
    assert settings_excel.status_code == 200 and settings_excel.data[:2] == b"PK"
    assert settings_word.status_code == 200 and settings_word.data[:2] == b"PK"

    # Без выбранного профиля списки и обзор не раскрывают названия задач.
    anonymous_client = portal.app.test_client()
    assert "Ролевая задача" not in anonymous_client.get("/tasks").data.decode("utf-8")
    assert "Ролевая задача" not in anonymous_client.get("/").data.decode("utf-8")

    # Пустые справочники предлагают добавить значение; перезапуск их не заполняет снова.
    empty_database = Path(temp_directory) / "empty.db"
    with closing(sqlite3.connect(empty_database)) as connection:
        connection.execute("CREATE TABLE departments (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)")
        connection.commit()
    portal.DATABASE = empty_database
    portal.BACKUP_DIR = Path(temp_directory) / "empty-backups"
    portal.init_database()
    empty_client = portal.app.test_client()
    assert row_count(empty_database, "departments") == 0
    assert "Создайте первое значение" in empty_client.get("/employees/new").data.decode("utf-8")
    for post_type in ("Обычный", "Важное объявление", "Новый сотрудник"):
        assert empty_client.post(f"/settings/post-types/{post_type}/delete").status_code == 302
    for status, _label in portal.PRESENCE_STATUSES:
        assert empty_client.post(f"/settings/statuses/{status}/delete").status_code == 302
    assert "Создайте первое значение" in empty_client.get("/feed/new").data.decode("utf-8")
    empty_client.post("/settings/departments/add", data={"name": "Новый отдел"})
    empty_client.post("/settings/positions/add", data={"name": "Новая должность"})
    empty_client.post("/settings/statuses/add", data={"name": "В сети", "color": "#23865f"})
    with closing(sqlite3.connect(empty_database)) as connection:
        department_id = connection.execute("SELECT id FROM departments WHERE name = 'Новый отдел'").fetchone()[0]
        new_status = connection.execute("SELECT key FROM presence_options WHERE label = 'В сети'").fetchone()[0]
    empty_client.post("/employees/new", data={"full_name": "Первый Сотрудник", "department_id": str(department_id), "position": "Новая должность", "birth_date": "1990-01-01"})
    with closing(sqlite3.connect(empty_database)) as connection:
        assert connection.execute("SELECT presence_status FROM employees WHERE full_name = 'Первый Сотрудник'").fetchone()[0] == new_status
    portal.init_database()
    assert row_count(empty_database, "post_types") == 0
    assert row_count(empty_database, "presence_options") == 1
    portal.DATABASE, portal.BACKUP_DIR = original_database, original_backups
    portal.UPLOAD_DIR = original_upload_dir

print("Проверка завершена: все основные сценарии работают.")
