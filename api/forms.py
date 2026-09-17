"""Reading an HTML form body, without python-multipart.

Lifted out of `api/ui/routes.py` when the sign-in page needed the same parsing. The reason it
exists at all is unchanged, and is the important part:

`await request.form()` cannot be used. Starlette asserts that python-multipart is importable
*before* it looks at the content type at all, so a plain urlencoded body -- which is all an HTML
form sends -- raises AssertionError when the library is absent. That is what made every Save
return 500 and no schedule ever get written.

The library stays absent deliberately. Parsing the body directly keeps multipart *upload* parsing
structurally unreachable in an app whose whole guarantee is that it only reads, and CLAUDE.md s1
forbids adding the dependency in any case. `tests/test_operations_controls.py` asserts it stays
out of requirements.txt.
"""
from urllib.parse import parse_qs

from fastapi import Request

MAX_FORM_BYTES = 64 * 1024
"""A form on these pages is a handful of short fields. Anything larger is not one of ours, and is
dropped rather than parsed -- an unbounded `parse_qs` over an arbitrary body is a way to spend all
the memory in the process on one request."""


async def form_values(request: Request) -> dict:
    """The posted fields, one value per name (the last, if a name repeats)."""
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        return {}
    if int(request.headers.get("content-length") or 0) > MAX_FORM_BYTES:
        return {}
    parsed = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def form_list(body: bytes, key: str) -> list:
    """Every value posted under one name.

    `form_values` keeps the last value per key, which is right for a text field and wrong for a
    list of tick boxes -- the thread offer posts one `also` per message, and keeping only the last
    would set aside one of thirteen while the page said thirteen.
    """
    if len(body) > MAX_FORM_BYTES:
        return []
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=False)
    return [value for value in parsed.get(key, []) if value]
