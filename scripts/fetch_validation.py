#!/usr/bin/env python3
"""Validate fetched HTML content before saving — detect paywall pages, Cloudflare challenges, incomplete content."""

import re


# Markers indicating the page has full article content
FULL_TEXT_MARKERS = {
    "oup": [
        "article-body", "article-full-text", "ContentTab",
        'div class="section"', 'section class="abstract"',
    ],
    "wiley": [
        "article-section__content", "article-section--abstract",
        "article__body", "article-section__full",
    ],
    "generic": [
        "article-body", "full-text", "abstract", "introduction",
        "methods", "results", "discussion",
    ],
}

# Markers indicating the page is behind a paywall
PAYWALL_MARKERS = [
    "sign in through your institution",
    "purchase access",
    "subscribe to",
    "buy this article",
    "rental options",
    "get access",
    "institutional login",
    "access denied",
    "you do not currently have access",
    "this content is available",
]

# Markers indicating Cloudflare challenge
CLOUDFLARE_MARKERS = [
    "cf-challenge",
    "cf-please-wait",
    "challenge-platform",
    "cf-turnstile",
    "just a moment",
    "checking your browser",
    "ray id",
]

MIN_ARTICLE_LENGTH = 10_000  # Minimum chars for a full article HTML


def validate_html(content: str, publisher: str = "generic") -> tuple[bool, list[str]]:
    """Validate fetched HTML content.

    Returns:
        (is_valid, issues) — is_valid is True if content appears to be a full article.
        issues is a list of problems found (empty if valid).
    """
    issues = []
    content_lower = content.lower()

    # Check 1: Minimum length
    if len(content) < MIN_ARTICLE_LENGTH:
        issues.append(f"Content too short ({len(content):,} chars, minimum {MIN_ARTICLE_LENGTH:,})")

    # Check 2: Cloudflare challenge
    cf_found = [m for m in CLOUDFLARE_MARKERS if m in content_lower]
    if cf_found:
        issues.append(f"Cloudflare challenge detected (markers: {', '.join(cf_found)})")

    # Check 3: Paywall indicators
    paywall_found = [m for m in PAYWALL_MARKERS if m in content_lower]
    if paywall_found:
        # Only flag as paywall if we DON'T also have full-text markers
        # (some pages show both "sign in" options AND the article content)
        has_full_text = _check_full_text_markers(content_lower, publisher)
        if not has_full_text:
            issues.append(f"Paywall detected (markers: {', '.join(paywall_found[:3])})")

    # Check 4: Full-text markers
    if not _check_full_text_markers(content_lower, publisher):
        issues.append(f"No full-text markers found for publisher '{publisher}'")

    # Check 5: Looks like an error page
    error_patterns = [
        r"<title>\s*404\b", r"<title>\s*error\b", r"<title>\s*page not found\b",
        r"<title>\s*access denied\b", r"<title>\s*forbidden\b",
    ]
    for pattern in error_patterns:
        if re.search(pattern, content_lower):
            issues.append(f"Error page detected (pattern: {pattern})")
            break

    is_valid = len(issues) == 0
    return is_valid, issues


def _check_full_text_markers(content_lower: str, publisher: str) -> bool:
    """Check if content contains full-text markers for the given publisher."""
    markers = FULL_TEXT_MARKERS.get(publisher, FULL_TEXT_MARKERS["generic"])
    return any(marker.lower() in content_lower for marker in markers)


def detect_access_status(content: str, publisher: str = "generic") -> str:
    """Detect the access status of fetched content.

    Returns one of:
        "full_text" — article content is present
        "paywall" — behind a paywall, need authentication
        "cloudflare" — blocked by Cloudflare challenge
        "error" — error page (404, etc.)
        "unknown" — can't determine status
    """
    content_lower = content.lower()

    # Cloudflare takes priority
    if any(m in content_lower for m in CLOUDFLARE_MARKERS):
        return "cloudflare"

    # Error pages
    if re.search(r"<title>\s*(404|error|page not found|access denied|forbidden)\b", content_lower):
        return "error"

    # Check for full text
    has_full_text = _check_full_text_markers(content_lower, publisher)
    has_paywall = any(m in content_lower for m in PAYWALL_MARKERS)

    if has_full_text and len(content) >= MIN_ARTICLE_LENGTH:
        return "full_text"
    elif has_paywall:
        return "paywall"
    elif len(content) < 1000:
        return "error"

    return "unknown"
