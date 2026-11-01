"""Sanitizer and structural scanner behaviour, on fabricated real-looking documents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from shelog import sanitize
from shelog.sanitize import PSEUDO_ID_RE, SanitizeError, apply, build_mapping, sanitize_docs, scan

# Fabricated values that merely look like real ones. They live in the test module, never in
# tests/fixtures, and none of them belongs to a real device.
GEN3_ID = "9c2f4d71e0b5"
GEN1_ID = "3e5a71"
GEN1_MAC = "ecfabc3e5a71"
BLE_ADDR = "d8:3b:da:11:22:33"
BSSID = "f0:9f:c2:aa:bb:cc"
IPV4 = "10.9.8.7"
IPV6 = "fe80::9e2f:4dff:fe71:e0b5"
SSID = "TestNet-Home"

ORIGINALS = [
    GEN3_ID,
    GEN1_ID,
    GEN1_MAC,
    "3E5A71",
    "9C:2F:4D:71:E0:B5",
    "EC:FA:BC:3E:5A:71",
    BLE_ADDR,
    BSSID,
    IPV4,
    IPV6,
    SSID,
]


def real_looking_doc() -> dict[str, Any]:
    return {
        "isok": True,
        "data": {
            "devices_status": {
                GEN3_ID: {
                    "id": GEN3_ID,
                    "temperature:0": {"id": 0, "tC": 21.5},
                    "wifi": {"sta_ip": IPV4, "ssid": SSID, "bssid": BSSID},
                    "ble": {"addr": BLE_ADDR.upper() + ",p"},
                    "link": {"sta_ip6": IPV6},
                    "mac": GEN3_ID.upper(),
                },
                GEN1_ID: {
                    "tmp": {"tC": 20.0, "is_valid": True},
                    "getinfo": {
                        "fw_info": {"device": f"shellyht-{GEN1_ID.upper()}"},
                        "mac": GEN1_MAC.upper(),
                    },
                    "wifi_sta": {"ssid": SSID, "ip": IPV4},
                },
            }
        },
    }


@pytest.fixture
def sanitized_text() -> str:
    return sanitize_docs([real_looking_doc()])[0]


def test_originals_do_not_survive_and_scan_is_clean(sanitized_text: str) -> None:
    lowered = sanitized_text.lower()
    for original in ORIGINALS:
        assert original.lower() not in lowered
    assert scan(sanitized_text) == []


def test_sanitized_output_is_still_valid_json_with_pseudo_device_keys(sanitized_text: str) -> None:
    keys = json.loads(sanitized_text)["data"]["devices_status"]
    assert len(keys) == 2
    assert all(PSEUDO_ID_RE.fullmatch(key) for key in keys)


def test_deterministic_and_shared_mapping() -> None:
    first = sanitize_docs([real_looking_doc(), real_looking_doc()])
    second = sanitize_docs([real_looking_doc(), real_looking_doc()])
    assert first == second
    assert first[0] == first[1]


def test_extra_ids_fix_the_numbering() -> None:
    mapping = build_mapping([real_looking_doc()], extra_ids=["123456"])
    # sorted lowercase ids: 123456 < 3e5a71 < 9c2f4d71e0b5, so the 6-hex device is number 2
    assert mapping[GEN1_ID] == "000002"
    assert mapping[GEN3_ID] == "020000000003"


def test_all_forms_of_one_device_share_a_number() -> None:
    mapping = build_mapping([real_looking_doc()])
    assert mapping[GEN1_ID] == "000001"
    assert mapping[GEN1_MAC] == "020000000001"
    assert mapping[GEN3_ID] == "020000000002"
    assert mapping["9c:2f:4d:71:e0:b5"] == "02:00:00:00:00:02"
    assert mapping["71e0b5"] == "000002"


def test_upper_case_original_gives_upper_case_pseudo() -> None:
    assert apply("shellyht-3E5A71", {"3e5a71": "00000a"}) == "shellyht-00000A"
    assert apply("shellyht-3e5a71", {"3e5a71": "00000a"}) == "shellyht-00000a"


def test_apply_does_not_touch_longer_tokens() -> None:
    assert apply("x3e5a71y 3e5a710", {"3e5a71": "000001"}) == "x3e5a71y 3e5a710"


def test_input_already_in_pseudo_space_is_refused() -> None:
    doc = {"data": {"devices_status": {"020000000001": {}}}}
    with pytest.raises(SanitizeError):
        sanitize_docs([doc])


def test_non_hex_device_key_is_refused() -> None:
    with pytest.raises(SanitizeError):
        sanitize_docs([{"devices_status": {"not-an-id": {}}}])


def test_uppercase_ssid_stays_a_lowercase_pseudo() -> None:
    doc = {"devices_status": {"3e5a71": {"wifi": {"ssid": "HOMEWIFI"}}}}
    text = sanitize_docs([doc])[0]
    assert "fixture-ssid-1" in text
    assert scan(text) == []


@pytest.mark.parametrize(
    ("rule", "planted", "secret"),
    [
        ("mac_outside_pseudo_space", {"x": "aa:bb:cc:dd:ee:ff"}, "aa:bb:cc:dd:ee:ff"),
        ("mac_outside_pseudo_space", {"x": "aabbccddeeff"}, "aabbccddeeff"),
        ("ipv4_outside_pseudo_net", {"x": "10.0.0.1"}, "10.0.0.1"),
        ("ipv6_outside_pseudo_net", {"x": "fe80::1234:5678"}, "fe80::1234:5678"),
        ("ssid_not_pseudo", {"wifi": {"ssid": "MyHomeNet"}}, "MyHomeNet"),
        ("device_key_not_allowed", {"devices_status": {"aabbcc": {}}}, "aabbcc"),
        ("id_not_allowed", {"id": "aabbccddeeff"}, "aabbccddeeff"),
        ("long_token", {"x": "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA"}, "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA"),
        ("device_tail_outside_pseudo_space", {"x": "shellyht-A1B2C3"}, "A1B2C3"),
    ],
)
def test_scan_flags_each_planted_kind_without_echoing_it(
    rule: str, planted: dict[str, Any], secret: str
) -> None:
    findings = scan(json.dumps(planted))
    assert any(finding.startswith(rule) for finding in findings), findings
    for finding in findings:
        assert secret.lower() not in finding.lower()


def test_scan_allows_pseudo_spaces_loopback_and_unspecified() -> None:
    doc = {
        "devices_status": {"020000000001": {"id": "020000000001"}, "000002": {}},
        "a": [
            "02:01:00:00:00:07",
            "192.0.2.55",
            "2001:db8::3",
            "127.0.0.1",
            "0.0.0.0",  # noqa: S104
            "::1",
            "::",
        ],
        "wifi": {"ssid": "fixture-ssid-4"},
        "b": {"ssid": None},
        "c": {"ssid": ""},
    }
    assert scan(json.dumps(doc)) == []


def test_scan_respects_explicit_allowed_ids() -> None:
    text = json.dumps({"devices_status": {"aabbcc": {}}})
    assert scan(text, allowed_ids={"aabbcc"}) == []
    assert scan(text, allowed_ids={"ddeeff"}) != []


def test_scan_text_mode_reports_offsets() -> None:
    findings = scan("ssid = 'x'\nmac = aa:bb:cc:dd:ee:ff\n")
    assert any(f.startswith("mac_outside_pseudo_space at offset") for f in findings)


def test_scan_paths_never_contain_device_ids() -> None:
    text = json.dumps({"devices_status": {"aabbcc": {"x": "10.0.0.1"}}})
    for finding in scan(text):
        assert "aabbcc" not in finding


def test_cli_writes_sanitized_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    devices = tmp_path / "devices.toml"
    devices.write_text(f'[[device]]\nid = "{GEN3_ID}"\nname = "A"\n', encoding="utf-8")
    raw = tmp_path / "raw" / "capture.json"
    raw.parent.mkdir()
    raw.write_text(json.dumps(real_looking_doc()), encoding="utf-8")
    out = tmp_path / "out"
    assert sanitize.main(["--devices-file", str(devices), str(out), str(raw)]) == 0
    written = (out / "capture.json").read_text(encoding="utf-8")
    assert scan(written) == []
    assert GEN3_ID not in written
    assert GEN3_ID not in capsys.readouterr().out


def test_cli_refuses_and_writes_nothing_when_scan_finds_leftovers(tmp_path: Path) -> None:
    devices = tmp_path / "devices.toml"
    devices.write_text('[[device]]\nid = "3e5a71"\nname = "A"\n', encoding="utf-8")
    raw = tmp_path / "capture.json"
    # a long token the sanitizer cannot know about survives and must block the output
    doc = {"devices_status": {"3e5a71": {"token": "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA"}}}
    raw.write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "out"
    assert sanitize.main(["--devices-file", str(devices), str(out), str(raw)]) == 1
    assert not out.exists()
