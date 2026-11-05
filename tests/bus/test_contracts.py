"""contracts/ <-> eds.bus.topics consistency: topic names, schema files, $id/$ref wiring."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from eds.bus import topics as T
from eds.core.paths import contracts_dir

jsonschema = pytest.importorskip("jsonschema")
from eds.bus import validate as V  # noqa: E402  (needs jsonschema)

CONSTANT_NAMES = (
    "RX_STATUS", "RX_SPECTRUM", "RX_DETECTIONS", "RX_SWEEP", "CAL_RESULT", "DF_BEARING",
    "MEAS_RESULT", "CTL_RX", "CTL_ACK",
)
EXPECTED = {
    "RX_STATUS": "rx.status", "RX_SPECTRUM": "rx.spectrum", "RX_DETECTIONS": "rx.detections",
    "RX_SWEEP": "rx.sweep", "CAL_RESULT": "cal.result", "DF_BEARING": "df.bearing",
    "MEAS_RESULT": "meas.result", "CTL_RX": "ctl.rx", "CTL_ACK": "ctl.ack",
}


def _schema_files() -> dict[str, dict]:
    d = contracts_dir() / "schemas"
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.schema.json"))}


def test_constant_values():
    for name, value in EXPECTED.items():
        assert getattr(T, name) == value


def test_constants_equal_registry_both_directions():
    reg = T.registry()
    constants = {getattr(T, n) for n in CONSTANT_NAMES}
    assert set(reg) == constants, f"yalnızca kayıtta: {set(reg) - constants}, yalnızca sabitlerde: {constants - set(reg)}"
    assert set(T.ALL_TOPICS) == set(reg)
    assert len(T.ALL_TOPICS) == len(set(T.ALL_TOPICS)) == len(reg)
    assert T.ALL_TOPICS == tuple(getattr(T, n) for n in CONSTANT_NAMES)


def test_registry_entries_complete():
    contract = T.load_contract()
    assert contract["envelope"] == T.ENVELOPE_SCHEMA
    for topic, entry in T.registry().items():
        assert entry["schema"] == f"{topic}.schema.json"
        for key in ("producer", "rate", "description"):
            assert isinstance(entry.get(key), str) and entry[key]


def test_registry_returns_copies():
    reg = T.registry()
    reg[T.RX_STATUS]["schema"] = "tampered.json"
    reg.pop(T.CTL_ACK)
    again = T.registry()
    assert again[T.RX_STATUS]["schema"] == "rx.status.schema.json"
    assert T.CTL_ACK in again


def test_schema_path_and_unknown_topic():
    for topic in T.ALL_TOPICS:
        p = T.schema_path(topic)
        assert p.is_file(), p
        assert p.parent == T.schemas_dir()
    assert T.envelope_schema_path().is_file()
    with pytest.raises(T.UnknownTopic):
        T.schema_path("rx.nope")
    assert not T.is_known("rx.nope") and T.is_known(T.RX_SWEEP)


@pytest.mark.parametrize("name", sorted(_schema_files()))
def test_every_schema_file_is_valid_draft_2020_12(name):
    doc = _schema_files()[name]
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["$id"] == T.SCHEMA_BASE_URI + name
    jsonschema.Draft202012Validator.check_schema(doc)
    assert isinstance(doc.get("title"), str) and doc["title"]
    assert isinstance(doc.get("description"), str) and doc["description"]


@pytest.mark.parametrize("topic", T.ALL_TOPICS)
def test_topic_schema_structure(topic):
    doc = json.loads(T.schema_path(topic).read_text(encoding="utf-8"))
    assert doc["allOf"] == [{"$ref": "envelope.schema.json"}]
    assert doc["properties"]["topic"] == {"const": topic}
    data = doc["properties"]["data"]
    assert data["type"] == "object"
    assert data["additionalProperties"] is False
    assert set(data["required"]) <= set(data["properties"])


def test_no_orphan_schema_files():
    files = set(_schema_files())
    referenced = {e["schema"] for e in T.registry().values()} | {T.ENVELOPE_SCHEMA, T.COMMON_SCHEMA}
    assert files == referenced


def test_envelope_schema_documents_bigint():
    doc = _schema_files()["envelope.schema.json"]
    assert "BigInt" in doc["description"] and "2**53" in doc["description"]
    assert doc["additionalProperties"] is False
    assert doc["required"] == ["topic", "v", "src", "seq", "t_ns", "data"]


def test_detection_def_is_shared():
    common = _schema_files()["common.schema.json"]
    det = common["$defs"]["detection"]
    assert set(det["required"]) == {
        "f_center_hz", "f_peak_hz", "f_lo_hz", "f_hi_hz", "bw_hz", "peak_db", "band_power_db",
        "snr_db", "noise_db", "n_bins",
    }
    for topic in (T.RX_DETECTIONS, T.RX_SWEEP):
        doc = json.loads(T.schema_path(topic).read_text(encoding="utf-8"))
        items = doc["properties"]["data"]["properties"]["detections"]["items"]
        assert items == {"$ref": "common.schema.json#/$defs/detection"}


def _refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref":
                yield v
            else:
                yield from _refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _refs(v)


def test_all_refs_are_local_and_resolve():
    files = _schema_files()
    reg = V.build_registry(files)
    for name, doc in files.items():
        resolver = reg.resolver(base_uri=T.schema_uri(name))
        for ref in _refs(doc):
            assert "://" not in ref, f"{name}: ağ referansı {ref}"
            resolved = resolver.lookup(ref)
            assert resolved.contents is not None


def test_unknown_ref_is_not_fetched(monkeypatch):
    """A $ref missing from the local registry fails; nothing is downloaded."""
    import urllib.request

    def _no_network(*a, **k):  # pragma: no cover - would only run on a regression
        raise AssertionError("şema çözümü ağa çıkmaya çalıştı")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    reg = V.build_registry(_schema_files())
    from referencing.exceptions import Unresolvable

    with pytest.raises(Unresolvable):
        reg.resolver(base_uri=T.schema_uri("rx.status.schema.json")).lookup("missing.schema.json")


def test_check_schemas_clean():
    assert V.check_schemas() == []


def test_contracts_dir_override(tmp_path, monkeypatch):
    """EDS_CONTRACTS_DIR relocates the registry; a broken copy is reported, not ignored."""
    dst = tmp_path / "contracts"
    shutil.copytree(contracts_dir(), dst)
    monkeypatch.setenv("EDS_CONTRACTS_DIR", str(dst))
    V.clear_cache()
    try:
        assert set(T.registry()) == set(T.ALL_TOPICS)
        assert T.schema_path(T.RX_STATUS) == dst / "schemas" / "rx.status.schema.json"
        assert V.check_schemas() == []
        # break one schema: const no longer matches the topic, $id mismatch is fatal
        p = dst / "schemas" / "ctl.ack.schema.json"
        original = p.read_text(encoding="utf-8")
        doc = json.loads(original)
        doc["properties"]["topic"]["const"] = "ctl.nack"
        p.write_text(json.dumps(doc), encoding="utf-8")
        problems = V.check_schemas()
        assert any("ctl.ack" in s and "const" in s for s in problems)
        doc["$id"] = "https://example.invalid/other.json"
        p.write_text(json.dumps(doc), encoding="utf-8")
        assert any("$id" in s for s in V.check_schemas())
        # a topic registered without a schema file
        reg_doc = json.loads((dst / "topics.json").read_text(encoding="utf-8"))
        reg_doc["topics"]["rx.extra"] = {"schema": "rx.extra.schema.json"}
        (dst / "topics.json").write_text(json.dumps(reg_doc), encoding="utf-8")
        assert "rx.extra" in T.registry()
        p.write_text(original, encoding="utf-8")
        problems = V.check_schemas()
        assert any("rx.extra" in s for s in problems) and not any("ctl.ack" in s for s in problems)
    finally:
        monkeypatch.delenv("EDS_CONTRACTS_DIR")
        V.clear_cache()
    assert "rx.extra" not in T.registry()


def test_malformed_registry_is_an_error(tmp_path, monkeypatch):
    d = tmp_path / "c"
    d.mkdir()
    (d / "topics.json").write_text('{"topics": {"rx.status": {}}}', encoding="utf-8")
    monkeypatch.setenv("EDS_CONTRACTS_DIR", str(d))
    from eds.bus.envelope import BusError

    with pytest.raises(BusError, match="schema"):
        T.registry()
    (d / "topics.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(BusError):
        T.registry()
    (d / "topics.json").unlink()
    with pytest.raises(BusError):
        T.registry()


def test_repo_contracts_location():
    assert (contracts_dir() / "topics.json").is_file()
    assert Path(T.contract_path()) == contracts_dir() / "topics.json"
