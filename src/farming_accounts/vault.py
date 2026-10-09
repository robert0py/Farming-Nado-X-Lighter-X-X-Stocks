"""User-scoped encrypted storage. No plaintext credentials, including temporary files."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import sys
import threading


class VaultError(Exception):
    pass


def sync_directory(path: Path) -> None:
    if sys.platform != 'win32':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def state_directory() -> Path:
    override = os.environ.get('HEDGE_STATE_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'HedgeBot' / 'state'
    return Path.home() / '.local' / 'share' / 'hedge-bot'


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data: bytes, *, decrypt=False) -> bytes:
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; intentionally NOT CRYPTPROTECT_LOCAL_MACHINE.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise VaultError('Windows не смог открыть защищённое хранилище текущего пользователя.')
    free = ctypes.windll.kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free(result.data)


class Vault:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if sys.platform != 'win32':
            self.directory.chmod(0o700)
        self.path = self.directory / 'vault.bin'
        self.lock = threading.RLock()

    def _key(self) -> bytes:
        path = Path(os.environ.get('HEDGE_MASTER_KEY_FILE', str(self.directory / 'master.key')))
        if path.is_symlink():
            raise VaultError('Файл ключа хранилища не должен быть символической ссылкой.')
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(path.parent)
        if path.stat().st_mode & 0o077:
            raise VaultError('Ограничьте права файла master.key до 0600 или 0400.')
        key = path.read_bytes()
        if len(key) != 32:
            raise VaultError('Некорректный ключ хранилища.')
        return key

    def load(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return {}
            try:
                encrypted = self.path.read_bytes()
                if sys.platform == 'win32':
                    if not encrypted.startswith(b'DP1'):
                        raise ValueError()
                    clear = dpapi(encrypted[3:], decrypt=True)
                else:
                    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                    if not encrypted.startswith(b'AG1'):
                        raise ValueError()
                    clear = AESGCM(self._key()).decrypt(encrypted[3:15], encrypted[15:], b'hedge-bot-v1')
                value = json.loads(clear)
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except VaultError:
                raise
            except Exception:
                raise VaultError('Не удалось расшифровать хранилище. Данные не заменены.') from None

    def save(self, value: dict) -> None:
        with self.lock:
            clear = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
            if sys.platform == 'win32':
                encrypted = b'DP1' + dpapi(clear)
            else:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                nonce = secrets.token_bytes(12)
                encrypted = b'AG1' + nonce + AESGCM(self._key()).encrypt(nonce, clear, b'hedge-bot-v1')
            temporary = self.directory / ('vault-' + secrets.token_hex(8) + '.tmp')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(encrypted)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                sync_directory(self.directory)
            finally:
                temporary.unlink(missing_ok=True)

    def update(self, function) -> dict:
        with self.lock:
            value = self.load()
            function(value)
            self.save(value)
            return value

    def control_token(self) -> str:
        with self.lock:
            value = self.load()
            if 'control_token' not in value:
                value['control_token'] = secrets.token_urlsafe(32)
                self.save(value)
            return value['control_token']
