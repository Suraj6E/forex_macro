"""Click-to-sort table headers.

Sorting replaces most filtering: if a column can be ordered, a filter for it is
usually just a slower way of looking at the top of that order. Sorting happens
in SQL with an explicit whitelist of columns — the sort key never reaches the
ORM as raw input.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SortColumn:
    key: str
    label: str
    field: str
    numeric: bool = False
    default_desc: bool = False
    width: str = ""
    sortable: bool = True


def resolve(request, columns: list[SortColumn], default_key: str):
    """Pick the column and direction, falling back to the default on anything
    unrecognised — a hand-edited query string cannot inject an ordering."""
    lookup = {c.key: c for c in columns if c.sortable}
    key = request.GET.get("sort") or default_key
    column = lookup.get(key) or lookup[default_key]
    key = column.key

    direction = request.GET.get("dir")
    if direction not in ("asc", "desc"):
        direction = "desc" if column.default_desc else "asc"
    return column, key, direction


def order_by(queryset, column: SortColumn, direction: str, tiebreak: str = "pk"):
    prefix = "-" if direction == "desc" else ""
    # A stable tiebreak stops rows shuffling between pages when the sort key
    # repeats, which it does constantly here — hundreds of releases share a
    # timestamp.
    return queryset.order_by(f"{prefix}{column.field}", tiebreak)


def headers(request, columns: list[SortColumn], key: str, direction: str) -> list[dict]:
    out = []
    for column in columns:
        if not column.sortable:
            out.append(
                {"label": column.label, "sortable": False, "numeric": column.numeric,
                 "width": column.width, "url": "", "active": False, "dir": ""}
            )
            continue

        if column.key == key:
            next_dir = "desc" if direction == "asc" else "asc"
        else:
            next_dir = "desc" if column.default_desc else "asc"

        params = request.GET.copy()
        params["sort"] = column.key
        params["dir"] = next_dir
        params.pop("page", None)

        out.append(
            {
                "label": column.label,
                "sortable": True,
                "numeric": column.numeric,
                "width": column.width,
                "url": "?" + params.urlencode(),
                "active": column.key == key,
                "dir": direction if column.key == key else "",
            }
        )
    return out
