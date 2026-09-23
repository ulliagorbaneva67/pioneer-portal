# Размещение полного портала в интернете

GitHub Pages не запускает Flask. Для полного портала нужен Python-хостинг и
постоянное место для SQLite и загруженных файлов.

## Рекомендуемый вариант: PythonAnywhere

1. Создайте аккаунт на <https://www.pythonanywhere.com/registration/register/beginner/>.
2. На вкладке **Consoles** откройте **Bash** и выполните:

   ```bash
   git clone https://github.com/ulliagorbaneva67/pioneer-portal.git
   cd pioneer-portal
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   mkdir -p ~/pioneer-data/uploads ~/pioneer-data/backups
   ```

3. На вкладке **Web** нажмите **Add a new web app**, выберите **Manual
   configuration** и доступную версию Python 3.
4. В поле **Virtualenv** укажите `/home/ВАШ_ЛОГИН/pioneer-portal/.venv`.
5. Откройте WSGI configuration file и замените его содержимое:

   ```python
   import os
   import sys

   project = "/home/ВАШ_ЛОГИН/pioneer-portal"
   if project not in sys.path:
       sys.path.insert(0, project)

   os.environ["PIONEER_DATA_DIR"] = "/home/ВАШ_ЛОГИН/pioneer-data"
   os.environ["PIONEER_SECRET_KEY"] = "ДЛИННАЯ-СЛУЧАЙНАЯ-СТРОКА"
   os.environ["PIONEER_ACCESS_PASSWORD"] = "НАДЁЖНЫЙ-ПАРОЛЬ-ПОРТАЛА"
   os.environ["PIONEER_HTTPS"] = "1"

   from app import app as application
   ```

6. Если нужны существующие данные, загрузите созданный локально файл
   `pioneer-data.zip` в `/home/ВАШ_ЛОГИН/`, затем выполните в Bash-консоли:

   ```bash
   unzip -o ~/pioneer-data.zip -d ~/pioneer-data
   ```

   Не помещайте этот архив, базу или загрузки в публичный GitHub.
7. На вкладке **Web** нажмите **Reload**.
8. Сайт откроется по адресу `https://ВАШ_ЛОГИН.pythonanywhere.com`.

## Обновление кода

После отправки изменений в GitHub откройте Bash-консоль PythonAnywhere:

```bash
cd ~/pioneer-portal
git pull
```

Затем нажмите **Reload** на вкладке **Web**. База и загрузки находятся отдельно
в `~/pioneer-data`, поэтому обновление кода их не затрагивает.

## Безопасность

- Никому не передавайте `PIONEER_SECRET_KEY`.
- Общий пароль защищает портал от посторонних, но внутри портала сохраняется
  локальная модель выбора профиля сотрудника.
- Для настоящей эксплуатации большим коллективом следует добавить отдельные
  пароли пользователей и восстановление доступа.
- Регулярно скачивайте резервные копии `pioneer.db` и папки `uploads`.
