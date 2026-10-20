"""Request-local reuse of read validation; never a cross-request data cache.

Callers hold the event authority lock. POST actions do not enter this scope.
The cache owns its keys so Python object IDs cannot be reused within a read.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_current = ContextVar('audited_read_scope', default=None)


@contextmanager
def read_scope():
    if _current.get() is not None:
        yield
        return
    token = _current.set({})
    try:
        yield
    finally:
        _current.reset(token)


def reuse(namespace, key, calculate):
    cache = _current.get()
    if cache is None:
        return calculate()
    identity = (namespace, key)
    if identity not in cache:
        cache[identity] = calculate()
    return cache[identity]


def validated_read(function):
    """Reuse a pure history validator within the caller's locked read snapshot."""
    @wraps(function)
    def wrapped(workspace, *args, **kwargs):
        key=(workspace,id(workspace._events),workspace.version,args,tuple(sorted(kwargs.items())))
        return reuse(function,key,lambda:function(workspace,*args,**kwargs))
    return wrapped
