"""Uploads are bounded.

The upload route streamed the request body to disk with shutil.copyfileobj
and no limit, then handed the file to pandas to read whole — so a multi-GB
body filled the disk and then memory. copy_with_limit counts as it copies
and refuses past the cap, removing the partial file.
"""
import io

import pytest

from app.services.upload_safety import UploadTooLarge, copy_with_limit


def test_a_small_upload_is_copied_in_full(tmp_path):
    dest = tmp_path / "stmt.csv"
    body = b"Date,Description,Amount\n01/01/2026,Coffee,-4.50\n"

    written = copy_with_limit(io.BytesIO(body), dest, max_bytes=1024)

    assert written == len(body)
    assert dest.read_bytes() == body


def test_exactly_at_the_limit_is_allowed(tmp_path):
    dest = tmp_path / "exact.bin"
    body = b"x" * 1000

    assert copy_with_limit(io.BytesIO(body), dest, max_bytes=1000) == 1000
    assert dest.stat().st_size == 1000


def test_one_byte_over_is_refused(tmp_path):
    dest = tmp_path / "over.bin"

    with pytest.raises(UploadTooLarge) as excinfo:
        copy_with_limit(io.BytesIO(b"x" * 1001), dest, max_bytes=1000)

    assert excinfo.value.limit == 1000


def test_a_refused_upload_leaves_no_partial_file(tmp_path):
    dest = tmp_path / "partial.bin"

    with pytest.raises(UploadTooLarge):
        copy_with_limit(io.BytesIO(b"x" * 5000), dest, max_bytes=1000)

    assert not dest.exists()


def test_the_cap_is_enforced_while_streaming_not_after(tmp_path):
    """A body far larger than the cap must be stopped after the cap's worth
    of chunks, not read to the end first."""
    dest = tmp_path / "stream.bin"

    class CountingSource:
        def __init__(self, total):
            self.remaining = total
            self.reads = 0

        def read(self, n):
            self.reads += 1
            if self.remaining <= 0:
                return b""
            take = min(n, self.remaining)
            self.remaining -= take
            return b"x" * take

    src = CountingSource(total=100 * 1024 * 1024)      # pretend 100 MB

    with pytest.raises(UploadTooLarge):
        copy_with_limit(src, dest, max_bytes=2 * 1024 * 1024, chunk_size=1024 * 1024)

    # Two 1 MB chunks fit, the third tips it over — nowhere near 100 reads.
    assert src.reads <= 4


def test_the_route_uses_the_configured_cap():
    """The default is sized for statements, not arbitrary files."""
    from app.config import settings

    assert settings.max_upload_bytes == 25 * 1024 * 1024
