"""Regression tests for stored-XSS hardening of cabinet ticket media.

User-uploaded ticket attachments are served from the cabinet's own origin. An
HTML/SVG file served inline as text/html / image/svg+xml would execute JS in the
app origin and steal the JWT/refresh token from localStorage. These tests pin the
safe serving contract: only raster images render inline; everything else downloads
as an opaque blob, with nosniff + a locked-down CSP, and a sanitized filename.

The inline/attachment decision is made by sniffing the downloaded bytes' magic
signature, NOT by trusting the filename/extension. Telegram's `file.file_path`
is not a reliable source of truth (self-hosted/proxied Bot API servers, or a
path with no recognizable suffix at all, produced `application/octet-stream`
for real photos and broke rendering — see `test_photo_without_extension_*`
below for the regression this fixes). Extension-based detection also could not
guarantee an attacker-labelled `evil.jpg` containing real HTML was safe — the
magic-byte approach closes that gap too.
"""

from __future__ import annotations

import pytest

from app.cabinet.routes.media import (
    _BLOCKED_UPLOAD_CONTENT_TYPES,
    _BLOCKED_UPLOAD_EXTENSIONS,
    _content_response_params,
    _sanitize_download_filename,
    _sniff_safe_image_type,
)


# Real magic-byte prefixes for each safe raster type, padded with harmless
# filler bytes so they look like a (truncated) real file rather than a bare
# signature.
JPEG_BYTES = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01' + b'\x00' * 32
PNG_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
GIF87_BYTES = b'GIF87a' + b'\x00' * 32
GIF89_BYTES = b'GIF89a' + b'\x00' * 32
WEBP_BYTES = b'RIFF\x24\x00\x00\x00WEBPVP8 ' + b'\x00' * 32

HTML_BYTES = b'<!DOCTYPE html><html><body><script>alert(1)</script></body></html>'
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
XML_BYTES = b'<?xml version="1.0"?><root/>'
JS_BYTES = b'alert(document.cookie)'
PDF_BYTES = b'%PDF-1.4\n' + b'\x00' * 32
ZIP_BYTES = b'PK\x03\x04' + b'\x00' * 32


@pytest.mark.parametrize(
    'filename,content,expected_mime',
    [
        ('x.jpg', JPEG_BYTES, 'image/jpeg'),
        ('x.jpeg', JPEG_BYTES, 'image/jpeg'),
        ('x.png', PNG_BYTES, 'image/png'),
        ('x.gif', GIF87_BYTES, 'image/gif'),
        ('x.gif', GIF89_BYTES, 'image/gif'),
        ('x.webp', WEBP_BYTES, 'image/webp'),
    ],
)
def test_raster_images_served_inline_with_their_type(filename, content, expected_mime):
    media_type, headers = _content_response_params(filename, content)
    assert media_type == expected_mime
    assert headers['Content-Disposition'].startswith('inline;')


@pytest.mark.parametrize(
    'filename,content',
    [
        ('evil.html', HTML_BYTES),
        ('evil.htm', HTML_BYTES),
        ('evil.svg', SVG_BYTES),
        ('evil.xml', XML_BYTES),
        ('evil.js', JS_BYTES),
        ('doc.pdf', PDF_BYTES),
        ('archive.zip', ZIP_BYTES),
        ('noext', b''),
    ],
)
def test_non_raster_forced_to_download_as_octet_stream(filename, content):
    media_type, headers = _content_response_params(filename, content)
    # Never serve a renderable/scriptable content-type for these.
    assert media_type == 'application/octet-stream'
    assert headers['Content-Disposition'].startswith('attachment;')


def test_photo_without_extension_still_served_inline():
    """Root-cause regression: a Telegram `file_path` with no recognizable
    extension (e.g. a self-hosted/proxied Bot API path, or any file name
    lacking a suffix) must still render inline as an image when the bytes
    are actually a photo. This is the exact bug reported: real photos were
    showing the `<img alt>` fallback text instead of the picture."""
    media_type, headers = _content_response_params('file_123', JPEG_BYTES)
    assert media_type == 'image/jpeg'
    assert headers['Content-Disposition'].startswith('inline;')


def test_photo_with_no_filename_at_all_still_served_inline():
    media_type, headers = _content_response_params('', PNG_BYTES)
    assert media_type == 'image/png'
    assert headers['Content-Disposition'].startswith('inline;')


def test_extension_lying_about_html_content_is_not_trusted():
    """Even a file claiming a safe image extension must be forced to
    download if its actual bytes are not a real raster image — the
    filename/extension alone can never grant inline rendering."""
    media_type, headers = _content_response_params('evil.jpg', HTML_BYTES)
    assert media_type == 'application/octet-stream'
    assert headers['Content-Disposition'].startswith('attachment;')


def test_extension_lying_about_svg_content_is_not_trusted():
    media_type, headers = _content_response_params('safe-looking.png', SVG_BYTES)
    assert media_type == 'application/octet-stream'
    assert headers['Content-Disposition'].startswith('attachment;')


def test_sniff_safe_image_type_direct():
    assert _sniff_safe_image_type(JPEG_BYTES) == 'image/jpeg'
    assert _sniff_safe_image_type(PNG_BYTES) == 'image/png'
    assert _sniff_safe_image_type(GIF87_BYTES) == 'image/gif'
    assert _sniff_safe_image_type(GIF89_BYTES) == 'image/gif'
    assert _sniff_safe_image_type(WEBP_BYTES) == 'image/webp'
    assert _sniff_safe_image_type(HTML_BYTES) is None
    assert _sniff_safe_image_type(SVG_BYTES) is None
    assert _sniff_safe_image_type(b'') is None


def test_html_is_never_text_html():
    media_type, headers = _content_response_params('payload.html', HTML_BYTES)
    assert media_type != 'text/html'
    assert 'attachment' in headers['Content-Disposition']


def test_svg_is_never_image_svg_xml():
    media_type, _ = _content_response_params('payload.svg', SVG_BYTES)
    assert media_type != 'image/svg+xml'


def test_hardening_headers_always_present():
    cases = [
        ('photo.png', PNG_BYTES),
        ('evil.html', HTML_BYTES),
        ('doc.pdf', PDF_BYTES),
    ]
    for filename, content in cases:
        _media_type, headers = _content_response_params(filename, content)
        assert headers['X-Content-Type-Options'] == 'nosniff'
        assert 'sandbox' in headers['Content-Security-Policy']
        assert "default-src 'none'" in headers['Content-Security-Policy']
        assert headers['Cache-Control'] == 'private, no-store'


def test_filename_sanitized_against_header_injection():
    # CRLF / quotes / path separators must not survive into the header.
    dirty = 'a/b\\c"d\r\ne.html'
    cleaned = _sanitize_download_filename(dirty)
    # The security-relevant chars (CRLF / quote / path separators) must be gone…
    assert '\r' not in cleaned and '\n' not in cleaned
    assert '"' not in cleaned and '/' not in cleaned and '\\' not in cleaned
    # …leaving only the basename's plain chars (separators split, the rest kept).
    assert cleaned == 'cde.html'


def test_empty_filename_falls_back():
    assert _sanitize_download_filename('') == 'file'
    assert _sanitize_download_filename('///') == 'file'


def test_blocked_upload_lists_cover_active_content():
    assert 'text/html' in _BLOCKED_UPLOAD_CONTENT_TYPES
    assert 'image/svg+xml' in _BLOCKED_UPLOAD_CONTENT_TYPES
    assert '.html' in _BLOCKED_UPLOAD_EXTENSIONS
    assert '.svg' in _BLOCKED_UPLOAD_EXTENSIONS
    assert '.js' in _BLOCKED_UPLOAD_EXTENSIONS
