"""Slug normalization — single source of truth for folder-name slugs.

Rules:
  - lowercase
  - strip non-ASCII (é → e, ñ → n, etc. via unicodedata)
  - any non-alphanumeric run → single hyphen
  - no leading/trailing hyphens

Kept in sync with the TypeScript version in
supabase/functions/_shared/slug.ts (same rules).
"""
from __future__ import annotations

import re
import unicodedata


def slugify(value: str) -> str:
    """Normalize a free-text folder name to a safe filesystem slug.

    >>> slugify("Fase 2 – Solicitudes de Exhibición")
    'fase-2-solicitudes-de-exhibicion'
    >>> slugify("  Claim Charts!!  ")
    'claim-charts'
    >>> slugify("Año 2024")
    'ano-2024'
    """
    if not value:
        return ""
    # Decompose accents (é → e + combining mark) then drop combining chars
    nfkd = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    # Lowercase + replace any non-alphanumeric run with a hyphen
    ascii_only = ascii_only.lower()
    ascii_only = re.sub(r"[^a-z0-9]+", "-", ascii_only)
    return ascii_only.strip("-")


if __name__ == "__main__":
    import doctest
    doctest.testmod(verbose=True)
