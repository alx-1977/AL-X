"""Mechanical, idempotent summaries for the goal store's result boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

STRING_LIMIT = 2_000
LIST_LIMIT = 50
RESULT_LIMIT = 8_000


def serialized_size(value: Any) -> int:
    # Count ASCII escaping and default whitespace, including metadata.
    return len(json.dumps(value, ensure_ascii=True))


def bound_result(value: Any, limit: int = RESULT_LIMIT) -> Any:
    """Bound one values/failure envelope without mutating the live result.

    Long strings are kept whole when the result fits the budget with them;
    long lists are always cut. Otherwise ordinary keys and scalars survive
    unless the aggregate budget requires omitting a container tail. Summary heads are small strings; list markers
    occupy the final slot, so repeated serialization is stable.
    """
    def normalize(item: Any, cut_strings: bool = True) -> Any:
        if cut_strings and isinstance(item, str) and len(item) > STRING_LIMIT:
            return {"head": item[:STRING_LIMIT], "original_length": len(item),
                    "sha256": hashlib.sha256(item.encode("utf-8")).hexdigest()}
        if isinstance(item, Mapping):
            return {key: normalize(child, cut_strings) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            if len(item) > LIST_LIMIT:
                return [normalize(child, cut_strings) for child in item[:LIST_LIMIT - 1]] + [
                    {"_omitted_items": len(item) - LIST_LIMIT + 1}]
            return [normalize(child, cut_strings) for child in item]
        return item

    missing = object()

    def fit(item: Any, budget: int) -> Any:
        if serialized_size(item) <= budget:
            return item
        # Preserve the original string's identity when shortening its head.
        if (isinstance(item, dict) and set(item) == {"head", "original_length", "sha256"}
                and isinstance(item["head"], str)):
            summary = dict(item, head="")
            if serialized_size(summary) > budget:
                return missing
            low, high = 0, len(item["head"])
            while low < high:
                middle = (low + high + 1) // 2
                summary["head"] = item["head"][:middle]
                if serialized_size(summary) <= budget:
                    low = middle
                else:
                    high = middle - 1
            return dict(item, head=item["head"][:low])
        if isinstance(item, dict):
            marker = "_omitted_items"
            while marker in item:
                marker = "_" + marker
            result: dict[str, Any] = {}
            entries = list(item.items())
            share = max(0, (budget - serialized_size(dict.fromkeys(item))) // max(1, len(item))) + 4
            for index, (key, child) in enumerate(entries):
                trial = dict(result)
                trial[key] = None
                trial[marker] = len(entries) - index - 1
                allowance = min(share, budget - serialized_size(trial) + 4)
                bounded = fit(child, allowance) if allowance >= 2 else missing
                if bounded is missing:
                    break
                result[key] = bounded
            omitted = len(entries) - len(result)
            if omitted:
                result[marker] = omitted
            return result if serialized_size(result) <= budget else missing
        if isinstance(item, list):
            result = []
            children, total = item, len(item)
            if (item and isinstance(item[-1], dict)
                    and set(item[-1]) == {"_omitted_items"}
                    and isinstance(item[-1]["_omitted_items"], int)):
                children = item[:-1]
                total = len(children) + item[-1]["_omitted_items"]
            for index, child in enumerate(children):
                trial = result + [None, {"_omitted_items": total - index - 1}]
                allowance = budget - serialized_size(trial) + 4
                bounded = fit(child, allowance) if allowance >= 2 else missing
                if bounded is missing:
                    break
                result.append(bounded)
            if len(result) < total:
                result.append({"_omitted_items": total - len(result)})
            return result if serialized_size(result) <= budget else missing
        return missing

    # Long strings are cut only when the result does not fit whole: cutting
    # one inside a result that is small overall only made her read the rest
    # a second time (review of 2026-11-05).
    whole = normalize(value, cut_strings=False)
    try:
        if serialized_size(whole) <= limit:
            return whole
    except TypeError:
        pass  # not plain JSON: bounded below exactly as before
    result = fit(normalize(value), limit)
    if result is missing:
        raise ValueError("result summary budget cannot hold omission metadata")
    return result
