from app.monitoring.prometheus import increment_counter as _inc


def increment_counter(name: str, tags: dict | None = None):
    _inc(name, tags=tags)
