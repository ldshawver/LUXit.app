"""Canonical server-side phone normalization for contacts and communications."""
from __future__ import annotations

import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import PhoneNumberFormat


_EXTENSION_RE = re.compile(r"(?:ext\.?|extension|x|#)\s*([0-9]{1,10})\s*$", re.I)


@dataclass(frozen=True)
class PhoneNormalizationResult:
    original: str
    normalized: str | None
    extension: str | None
    is_valid: bool
    country: str
    error: str | None = None


def split_extension(value: str | None) -> tuple[str, str | None]:
    raw = (value or "").strip()
    match = _EXTENSION_RE.search(raw)
    if not match:
        return raw, None
    return raw[: match.start()].strip(), match.group(1)


def normalize_phone(value: str | None, default_country: str = "US") -> PhoneNormalizationResult:
    original = (value or "").strip()
    country = (default_country or "US").upper()
    base, ext = split_extension(original)
    if not base:
        return PhoneNormalizationResult(original, None, ext, False, country, "blank")
    try:
        parsed = phonenumbers.parse(base, None if base.startswith("+") else country)
        # Legacy imports sometimes omit the international ``+``. Only retry
        # clearly non-NANP long digit strings, and still require libphonenumber
        # to prove that the resulting number is possible and valid.
        digits = re.sub(r"\D", "", base)
        if not phonenumbers.is_valid_number(parsed) and not base.startswith("+") and len(digits) > 11:
            international_digits = digits[3:] if digits.startswith("001") else digits.removeprefix("00")
            parsed = phonenumbers.parse("+" + international_digits, None)

        if not phonenumbers.is_valid_number(parsed):
            return PhoneNormalizationResult(original, None, ext, False, country, "invalid")
        return PhoneNormalizationResult(original, phonenumbers.format_number(parsed, PhoneNumberFormat.E164), ext, True, country)
    except Exception as exc:
        return PhoneNormalizationResult(original, None, ext, False, country, str(exc))


def normalize_phone_e164(value: str | None, default_country: str = "US") -> str:
    result = normalize_phone(value, default_country)
    return result.normalized or ""


def format_phone_display(value: str | None, default_country: str = "US") -> str:
    result = normalize_phone(value, default_country)
    if not result.normalized:

        return (value or "").strip()
    parsed = phonenumbers.parse(result.normalized, None)
    rendered = phonenumbers.format_number(parsed, PhoneNumberFormat.NATIONAL if result.normalized.startswith("+1") else PhoneNumberFormat.INTERNATIONAL)
    return f"{rendered} x{result.extension}" if result.extension else rendered
