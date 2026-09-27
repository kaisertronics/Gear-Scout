"""
Base scraper class and shared utilities.
"""
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Listing:
    source_name: str
    title: str
    url: str
    price: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    posted_at: Optional[datetime] = None
    listing_id: Optional[str] = None  # unique ID within source
    location: Optional[str] = None  # "City, ST" or a town name, for local-pickup sources

    def __post_init__(self):
        if not self.listing_id:
            # Fallback: hash the URL
            self.listing_id = hashlib.md5(self.url.encode()).hexdigest()[:16]

    @property
    def global_id(self) -> str:
        """Globally unique ID across all sources."""
        return f"{self.source_name}::{self.listing_id}"


@dataclass
class ScrapeResult:
    source_name: str
    source_url: str
    success: bool
    listings: list[Listing] = field(default_factory=list)
    error: Optional[str] = None
    fix_hint: Optional[str] = None  # human-readable fix instructions
    duration_seconds: float = 0.0
    # True only for the specific "site sits behind bot detection, use the
    # manual link instead" case (Guitar Center/Sweetwater/eBay) — this is
    # expected, permanent, and has a working manual link, so the UI shouldn't
    # scare people with red "FAILED" for it the way it does for an actual bug
    # (a real layout change breaking a selector, etc).
    blocked: bool = False


_keyword_pattern_cache: dict[str, re.Pattern] = {}


def _keyword_pattern(kw: str) -> Optional[re.Pattern]:
    kw_clean = kw.strip().lower()
    if not kw_clean:
        return None
    pattern = _keyword_pattern_cache.get(kw_clean)
    if pattern is None:
        # Allow an optional trailing "s" so plurals of the keyword's last word
        # still match (e.g. "studio monitor" -> "studio monitors").
        pattern = re.compile(r'\b' + re.escape(kw_clean) + r's?\b')
        _keyword_pattern_cache[kw_clean] = pattern
    return pattern


_combined_cache: dict[tuple, Optional[re.Pattern]] = {}


_last_combined: tuple = (None, -1, None)


def _combined_pattern(keywords: list[str]) -> Optional[re.Pattern]:
    # Callers pass the same list object for every listing in a run; building
    # the cache key from 1,000+ terms per call was the actual bottleneck.
    global _last_combined
    ref, length, pattern = _last_combined
    if keywords is ref and len(keywords) == length:
        return pattern
    key = tuple(str(k) for k in keywords)
    if key not in _combined_cache:
        parts = sorted({re.escape(k.strip().lower()) for k in key if k.strip()}, key=len, reverse=True)
        if len(_combined_cache) > 32:
            _combined_cache.clear()
        _combined_cache[key] = re.compile(r"\b(?:" + "|".join(parts) + r")s?\b") if parts else None
    _last_combined = (keywords, len(keywords), _combined_cache[key])
    return _combined_cache[key]


def keyword_match(text: str, keywords: list[str]) -> bool:
    """Return True if any keyword is found in text as a whole word/phrase
    (case-insensitive). Word-boundary matching keeps short keywords like "mic"
    or "rme" from matching fragments inside unrelated words (e.g. "Samick",
    "Performer") the way plain substring matching would."""
    text_lower = text.lower()
    if len(keywords) > 1:
        # One combined pattern instead of a loop over every keyword — with
        # 1,000+ search terms the loop made pages like Settings take seconds.
        # Same rule per keyword (whole word/phrase, optional plural "s").
        combined = _combined_pattern(keywords)
        return bool(combined and combined.search(text_lower))
    for kw in keywords:
        pattern = _keyword_pattern(str(kw))
        if pattern and pattern.search(text_lower):
            return True

    # A single term means a live search, where the site's own search already
    # did the real matching and normalizes spacing/hyphens and model suffixes
    # ("KM 184", "KM-184" and "KM184NI" all count as "km184" on Reverb). Only
    # model-number-style terms (letters + digits) get this looser match —
    # plain words like "eq" or "mic" keep strict word matching.
    if len(keywords) == 1:
        # Multi-word searches ("sony c38") need every word, in any order —
        # like the sites' own search boxes — not the exact phrase, so
        # "Sony C-38B" and "C38B mic by Sony" both match.
        words = [w for w in re.split(r'\s+', keywords[0].strip()) if re.search(r'[a-z0-9]', w, re.I)]
        if words and all(_word_matches(w, text_lower) for w in words):
            return True
    return False


def _word_matches(word: str, text_lower: str) -> bool:
    model = _model_pattern(word)
    if model:
        return bool(model.search(text_lower))
    digits = word.strip().lower()
    if digits.isdigit() and len(digits) >= 3:
        # Bare model numbers also take letter suffixes: 512 -> 512c,
        # 1176 -> 1176LN — but not a longer number (1073 != 10730).
        return bool(re.search(r'(?<![a-z0-9])' + digits + r'[a-z]*(?![a-z0-9])', text_lower))
    pattern = _keyword_pattern(word)
    return bool(pattern and pattern.search(text_lower))


_model_pattern_cache: dict[str, Optional[re.Pattern]] = {}


def _model_pattern(term: str) -> Optional[re.Pattern]:
    """For a model-number search (letters + digits, e.g. "c38", "u87",
    "la2a"): also match that model's lettered variants and spacing, so "c38"
    finds "C38A", "C38B", "C-38B" and "C 38 A" — but not a different model
    number like "C380". Returns None for anything that isn't model-style."""
    compact = re.sub(r'[^a-z0-9]', '', term.lower())
    if compact in _model_pattern_cache:
        return _model_pattern_cache[compact]
    pattern = None
    if re.search(r'[a-z]', compact) and re.search(r'\d', compact):
        runs = re.findall(r'[a-z]+|\d+', compact)
        body = r'[\s\-_./]?'.join(re.escape(r) for r in runs)
        pattern = re.compile(r'(?<![a-z0-9])' + body + r'[a-z]*(?![a-z0-9])')
    _model_pattern_cache[compact] = pattern
    return pattern


def clean_price(raw: str) -> Optional[str]:
    """Normalize price strings."""
    if not raw:
        return None
    raw = raw.strip()
    if not any(c.isdigit() for c in raw):
        return None
    return raw


def truncate(text: str, length: int = 200) -> str:
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:length] + "..." if len(text) > length else text
