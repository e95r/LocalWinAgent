# LocalWinAgent

LocalWinAgent — локальный ИИ-ассистент для Windows 10/11 с веб-интерфейсом и CLI. Проект использует Ollama для генерации ответов, FastAPI для серверной части и набор встроенных инструментов для работы с файлами, приложениями и вебом.

## Возможности

- Общение через веб-чат по адресу [http://127.0.0.1:8765/chat](http://127.0.0.1:8765/chat).
- CLI с историей команд, автодополнением и подтверждением опасных действий.
- Управление приложениями (Блокнот, VS Code, Word, Excel, Chrome).
- Поиск файлов через Everything (`es.exe`).
- Автоматизация браузера через Playwright (поиск и открытие страниц).
- Контроль доступа к файловой системе с белым списком путей и подтверждениями.

## Предварительные требования

1. **Windows 10/11**.
2. **Python 3.11**: [скачать с python.org](https://www.python.org/downloads/windows/). При установке отметьте «Add Python to PATH».
3. **Git**: [скачать](https://git-scm.com/download/win).
4. **Ollama**:
   - Установите [Ollama для Windows](https://ollama.com/download).
   - После установки в PowerShell выполните:
     ```powershell
     ollama pull llama3.1:8b
     ollama pull qwen2:7b
     ```
5. **Everything** от Voidtools:
   - Скачайте и установите [Everything](https://www.voidtools.com/downloads/).
   - В настройках Everything включите «Путь к es.exe» или добавьте каталог установки в `PATH`.
6. **Playwright**: будет установлен вместе с зависимостями Python (см. ниже).

## Установка проекта

```powershell
# Клонирование репозитория
git clone https://example.com/LocalWinAgent.git
cd LocalWinAgent

# Создание виртуального окружения
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Установка зависимостей
pip install -r requirements.txt

# Установка браузеров Playwright
playwright install
```

> Если команда `playwright` недоступна, запустите `python -m playwright install`.

## Быстрый запуск через ярлык

1. Выполните в каталоге проекта:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\create_shortcut.ps1
   ```
2. На рабочем столе появится ярлык **LocalWinAgent**. Двойной клик запустит сервер и откроет чат в браузере (используется `scripts\start_agent.ps1`).

## Ручной запуск

### Запуск сервера и веб-чата
```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn main:app --host 127.0.0.1 --port 8765
# Откройте http://127.0.0.1:8765/chat в браузере
```

### Запуск CLI
```powershell
.\.venv\Scripts\Activate.ps1
python agent_cli.py
```

Команды CLI:
- `открой текстовый редактор`
- `найди файл отчёт по продажам 2023`
- `закрой excel`
- `найди страницу fastapi background tasks и открой`
- `:auto on` / `:auto off` — включить/выключить автоподтверждение
- `:model qwen2:7b` — переключение модели Ollama

## Структура проекта

```
LocalWinAgent/
├── agent_cli.py
├── config/
│   ├── apps.yml
│   ├── paths.yml
│   └── web.yml
├── frontend/
│   └── chat/
│       └── index.html
├── intent_router.py
├── logs/
├── main.py
├── requirements.txt
├── scripts/
│   ├── create_shortcut.ps1
│   └── start_agent.ps1
├── tests/
│   ├── test_configs.py
│   ├── test_fuzzy.py
│   └── test_router.py
└── tools/
    ├── __init__.py
    ├── apps.py
    ├── files.py
    ├── search.py
    └── web.py
```

## Журналирование

- Консольные сообщения форматируются через `rich`.
- Файл логов сохраняется в `logs/agent.log`.

## Примечания по безопасности

- Все операции с файлами вне белого списка из `config/paths.yml` требуют явного подтверждения.
- Массовые операции (удаление каталогов) выполняются только после подтверждения или при включенном автоподтверждении.

## Тестирование

Проект содержит тесты на pytest:
```powershell
.\.venv\Scripts\Activate.ps1
pytest
```

## Часто используемые команды

- «Открой текстовый редактор» — запустит Блокнот.
- «Найди файл отчёт по продажам 2023» — выполнит поиск через Everything.
- «Закрой Excel» — завершит процесс Excel.
- «Найди страницу fastapi background tasks и открой» — откроет поисковую выдачу в браузере.

## Как говорит пользователь

LocalWinAgent распознаёт намерения по контексту фразы, а не по точным ключевым словам. Можно говорить естественно:

- «Хочу посмотреть последний скрин» — ассистент запустит локальный поиск изображений и откроет найденный файл.
- «Нужна документация fastapi» — будет найдено несколько ссылок, первая откроется в браузере.
- «Запусти что-нибудь, чтобы посчитать» — распознается как запрос на запуск калькулятора.
- «Посмотрим вчерашний отчёт» — агент найдёт ближайшие документы и предложит выбрать нужный.
- «Давай откроем блог проекта» — инициируется веб-поиск и открытие подходящего сайта.

Команды вроде «первую ссылку», «открой его», «сбрось контекст» работают по последним результатам в пределах 15 минут.

## Примеры команд

**Диалоговый сценарий поиска и открытия файла**

```
Пользователь: «найди файл скриншот»
Агент: «Нашёл (выберите номер):
        1) C:\Users\...\Desktop\screen1.png
        2) C:\Users\...\Pictures\screen2.png
        3) D:\Shots\screen3.png»
Пользователь: «открой 2»
Агент: «Открыл: C:\Users\...\Pictures\screen2.png (ok=True)»
```

Команда «сбрось контекст» очищает последние результаты.

## Диалоговый контекст

- После каждого поиска файлов ассистент запоминает список результатов в рамках сессии.
- Повторные команды «открой 2», «покажи первый», «открой его» используют последние найденные пути.
- Буфер результатов очищается автоматически через 15 минут бездействия или при выполнении нового поиска.
- Для ручного сброса скажите «сбрось контекст» или «очисти память».

## Code-as-Actions

Начиная с версии Code-as-Actions все действия агент выполняет через небольшой Python-скрипт, который формируется на лету и исполняется в защищённой песочнице. Скрипты используют API модулей `tools.files`, `tools.apps`, `tools.search`, `tools.web` и всегда возвращают подробный отчёт (`stdout`, `stderr`, структурированные данные).

### Как это работает

1. Пользовательская команда анализируется интент-детектором (`IntentInferencer`).
2. Агент формирует объект `TaskRequest` и минимальный скрипт с функцией `run(params)`.
3. Код проверяется, запускается в песочнице (`core/sandbox.py`), а результат упаковывается в `TaskResult`.
4. Ответ пользователю содержит итоговое сообщение, а также служебные данные (пути, URL, подтверждения).

### Примеры мини-скриптов

Создание файла с текстом:

```python
from tools.files import FileManager

def run(params):
    manager = FileManager(params["whitelist"])
    info = manager.create_file(
        params["path"],
        content=params.get("content", ""),
        confirmed=params.get("confirmed", False),
    )
    return {"ok": bool(info.get("ok")), "stdout": f"Создан файл: {info['path']} (exists={info.get('exists')})", "data": {"file": info}}
```

Локальный поиск и открытие первого результата:

```python
from tools.files import open_path
from tools.search import search_local

def run(params):
    results = search_local(params["query"], whitelist=params["whitelist"], max_results=10)
    if not results:
        return {"ok": False, "stdout": "Ничего не найдено", "data": {"results": []}}
    opened = open_path(results[0])
    return {"ok": bool(opened.get("ok", False)), "stdout": opened.get("reply", ""), "data": {"results": results}}
```

Песочница блокирует опасные операции (`os.system`, `eval`, произвольные импорты), ограничивает время выполнения и объём вывода. Благодаря этому код как действие остаётся безопасным, а ответы — максимально прозрачными.

## Поддержка

В случае вопросов создавайте issue в репозитории или пишите в команду сопровождения.
