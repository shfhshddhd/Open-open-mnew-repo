"""Compatibility shim for plugins that import the source obfuscation helper."""


def thanos_protect(value: str) -> str:
    return value