import os
import stat
import sys


WINDOWS_FORBIDDEN_IN_FILENAMES = '<>:"/\\|?*'

WINDOWS_RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{n}" for n in range(1, 10)),
    *(f"LPT{n}" for n in range(1, 10)),
}


def posix_permissions_are_meaningful():
    return os.name == "posix"


def private_file_is_readable_by_others(path):
    if not posix_permissions_are_meaningful():
        return False
    return bool(stat.S_IMODE(os.stat(path).st_mode) & 0o077)


def make_file_private(path):
    if not posix_permissions_are_meaningful():
        return (f"'{path}' holds a secret. This platform has no POSIX file mode, so "
                f"nothing was changed: restrict it through the folder's security "
                f"settings, or keep it on an encrypted volume.")
    os.chmod(path, 0o600)
    return f"'{path}' tightened to 600."


def safe_filename(name, replacement="_"):
    cleaned = "".join(replacement if character in WINDOWS_FORBIDDEN_IN_FILENAMES
                      or ord(character) < 32 else character
                      for character in str(name))
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        return replacement
    stem = cleaned.split(".")[0].upper()
    if stem in WINDOWS_RESERVED_FILENAMES:
        cleaned = replacement + cleaned
    return cleaned


def console_can_print(text, stream=None):
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding, errors="strict")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def printable(text, stream=None):
    if text is None:
        return ""
    text = str(text)
    if console_can_print(text, stream):
        return text
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except LookupError:
        return text.encode("ascii", errors="replace").decode("ascii")


def use_unicode_console():
    changed = []
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if console_can_print("日本語", stream):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
            changed.append(name)
        except (ValueError, OSError):
            pass
    return changed


def remove_files(paths):
    removed, blocked = [], []
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
            removed.append(path)
        except PermissionError:
            blocked.append(path)
    return removed, blocked

def looks_like_a_repository(directory="."):
    return os.path.isdir(os.path.join(directory, ".git"))


def live_credential_in_a_repository(key_file, example_file, directory="."):
    if not looks_like_a_repository(directory):
        return False
    if not (os.path.exists(key_file) and os.path.exists(example_file)):
        return False
    try:
        with open(key_file, "r", encoding="utf-8") as handle:
            live = handle.read().strip()
        with open(example_file, "r", encoding="utf-8") as handle:
            placeholder = handle.read().strip()
    except OSError:
        return False
    return bool(live) and live != placeholder


def plural(count, word, many=None):
    return word if count == 1 else (many or word + "s")
