# Borrowed from https://stackoverflow.com/questions/6618513/python-logging-with-context

import contextvars
import logging
from typing import List

log_context_data = contextvars.ContextVar('log_context_data', default=dict())

class ContextFilter(logging.Filter):
    """
    This is a filter which injects contextual information from `contextvars.ContextVar` (log_context_data) into the log.
    """
    def __init__(self, attributes: List[str]):
        super().__init__()
        self.attributes = attributes

    def filter(self, record):
        context_dict = log_context_data.get()
        for a in self.attributes:
            setattr(record, a, context_dict.get(a, 'None'))
        return True


class LoggerContext(object):
    def __init__(self, logger, context: dict = None):
        self.logger = logger
        self.context: dict = context
        self.token = None

    def __enter__(self):
        context_dict = log_context_data.get()
        for key, val in self.context.items():
            context_dict[key] = val
        self.token = log_context_data.set(context_dict)
        return self

    def __exit__(self, et, ev, tb):
        log_context_data.reset(self.token)
        self.token = None
