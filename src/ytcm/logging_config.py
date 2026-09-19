import logging

from ytcm import sanitize as sanitize


class Scrubbed(logging.Filter):

    def filter(self, record):
        try:
            if record.args:
                record.msg = record.getMessage()
                record.args = ()
            record.msg = sanitize.scrub(record.msg)
            if record.exc_info:
                record.exc_text = sanitize.scrub(
                    record.exc_text or logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
        except Exception:
            record.msg = "[a log line was dropped]"
            record.args = ()
            record.exc_info = None
        return True


class WithoutTraceback(logging.Formatter):

    def format(self, record):
        traceback, record.exc_text = record.exc_text, None
        try:
            return super().format(record)
        finally:
            record.exc_text = traceback


_scrubber = Scrubbed()
_add_handler = logging.Logger.addHandler


def _scrubbed_add_handler(self, handler):

    if not any(isinstance(f, Scrubbed) for f in handler.filters):
        handler.addFilter(_scrubber)
    _add_handler(self, handler)


logging.Logger.addHandler = _scrubbed_add_handler

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
for _handler in logger.handlers:
    _scrubbed_add_handler(logger, _handler)
