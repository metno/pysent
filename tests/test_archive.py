from types import SimpleNamespace

from pysent.archive import (
    DEFAULT_ARCHIVE_ROOT,
    extract_download_url,
    map_archive_download_to_local,
    resolve_archive_root,
)

_URL = "https://nbstac.met.no/nbsArchive/S2A/2022/03/19/S2A_MSIL1C_x.zip"


def _record(*refs):
    return SimpleNamespace(references=list(refs))


def test_extract_download_url_picks_the_download_scheme():
    record = _record(
        {"scheme": "OGC:WMS", "url": "https://example/wms"},
        {"scheme": "WWW:DOWNLOAD-1.0-http--download", "url": _URL},
    )
    assert extract_download_url(record) == _URL


def test_extract_download_url_accepts_protocol_key():
    assert extract_download_url(_record({"protocol": "WWW:DOWNLOAD-1.0-http--download", "url": _URL})) == _URL


def test_extract_download_url_returns_none_without_references():
    assert extract_download_url(SimpleNamespace()) is None
    assert extract_download_url(_record({"scheme": "OGC:WMS", "url": "https://example/wms"})) is None


def test_map_archive_download_to_local_rewrites_onto_the_archive_root():
    assert map_archive_download_to_local(_URL, "/mnt/nbs") == "/mnt/nbs/S2A/2022/03/19/S2A_MSIL1C_x.zip"


def test_map_archive_download_to_local_without_the_marker():
    assert map_archive_download_to_local("https://example/other/product.zip", "/mnt/nbs") is None
    assert map_archive_download_to_local(None, "/mnt/nbs") is None


def test_map_archive_download_to_local_uses_env_root(monkeypatch):
    monkeypatch.setenv("NBS_ARCHIVE_ROOT", "/lustre/nbsArchive")
    assert map_archive_download_to_local(_URL) == "/lustre/nbsArchive/S2A/2022/03/19/S2A_MSIL1C_x.zip"


def test_resolve_archive_root_precedence(monkeypatch):
    monkeypatch.delenv("NBS_ARCHIVE_ROOT", raising=False)
    assert resolve_archive_root() == DEFAULT_ARCHIVE_ROOT
    monkeypatch.setenv("NBS_ARCHIVE_ROOT", "/from/env")
    assert resolve_archive_root() == "/from/env"
    assert resolve_archive_root("/explicit") == "/explicit"
