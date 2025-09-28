"""Исключения, используемые инструментами LocalWinAgent."""

class EverythingNotInstalledError(Exception):
    """Сообщает об отсутствии обязательных внешних зависимостей."""


class FileOperationError(Exception):
    """Обёртка для ошибок при работе с файлами."""


class NotAllowedPathError(Exception):
    """Путь не входит в список разрешённых директорий."""
