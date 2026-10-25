"""YAML from outside the platform (an app's ``appstore.yaml``, a pasted ``clouds.yaml``).

``yaml.safe_load`` does not execute anything, but it expands aliases without limit: a few hundred bytes of
"billion laughs" can use gigabytes of memory, and a deeply nested document can exhaust the stack. Neither
file has any use for aliases, so a document is refused if it is larger than ``max_bytes``, contains an
alias, or nests too deeply; only then is it loaded.
"""

from __future__ import annotations

from typing import Any

import yaml

MAX_YAML_BYTES = 256 * 1024
MAX_NESTING = 32


class UnsafeYaml(ValueError):
    """The document is refused before it is loaded."""


def load(text: str | bytes, max_bytes: int = MAX_YAML_BYTES) -> Any:
    """``yaml.safe_load`` for untrusted text. Raises :class:`UnsafeYaml` or ``yaml.YAMLError``."""
    raw = text if isinstance(text, bytes) else text.encode("utf-8", "replace")
    if len(raw) > max_bytes:
        raise UnsafeYaml(f"the YAML document is larger than {max_bytes // 1024} KiB")
    depth = 0
    try:
        for event in yaml.parse(raw, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise UnsafeYaml("the YAML document uses aliases, which are not allowed")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > MAX_NESTING:
                    raise UnsafeYaml("the YAML document is nested too deeply")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
    except RecursionError:
        raise UnsafeYaml("the YAML document is nested too deeply") from None
    return yaml.safe_load(raw)
