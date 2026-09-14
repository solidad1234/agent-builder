from contextvars import ContextVar


_request_context = ContextVar("agent_builder_request_context", default={})


def set_request_context(**values):
    _request_context.set(values)


def clear_request_context():
    _request_context.set({})


def get_request_context():
    return _request_context.get()
