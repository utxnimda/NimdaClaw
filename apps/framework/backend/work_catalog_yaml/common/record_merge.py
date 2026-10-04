"""Three-way merging of explicitly confirmed changes, not blind record replacement."""
from copy import deepcopy

_MISSING = object()


def merge_confirmed_changes(before, desired, current, *, location="record"):
    """Preserve fields the user did not edit; conflicting edits require a new preview."""
    def same(left, right):
        return left is right if left is _MISSING or right is _MISSING else left == right

    def clone(value):
        return value if value is _MISSING else deepcopy(value)

    def merge(base, wanted, latest, path):
        if same(wanted, base):
            return clone(latest)
        if same(latest, base) or same(latest, wanted):
            return clone(wanted)
        if all(isinstance(value, dict) for value in (base, wanted, latest)):
            result = {}
            for key in sorted(base.keys() | wanted.keys() | latest.keys()):
                value = merge(base.get(key, _MISSING), wanted.get(key, _MISSING), latest.get(key, _MISSING), f"{path}.{key}")
                if value is not _MISSING:
                    result[key] = value
            return result
        if all(isinstance(value, list) for value in (base, wanted, latest)) and len(base) == len(wanted) == len(latest):
            return [merge(a, b, c, f"{path}[{index}]") for index, (a, b, c) in enumerate(zip(base, wanted, latest))]
        raise ValueError(f"多个已选目录对同一 DB 字段有不同修改，请统一后重新预览：{path}")

    return merge(before, desired, current, location)
