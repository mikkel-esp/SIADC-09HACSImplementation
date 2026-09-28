"""Protocol tests ported from the SIADC09Debugger vitest suite.

Every vector here comes from ``test/dc09.test.ts`` in that project, so a parity
failure between the two implementations shows up as a test failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.sia_dc09.dc09 import (
    Dc09ExtendedData,
    build_ack,
    build_nak,
    decode,
    enrich,
    parse_key,
    wire_to_bytes,
)

from .conftest import decode_text


class TestFrameDecoding:
    """Framing, CRC and length handling."""

    def test_accepts_sia_dcs_with_extended_data(self, key: bytes) -> None:
        result = decode_text(
            '\n7C37006b"SIA-DCS"0000RC0FFDL1234#080027E62A64'
            "[#080027E62A64|NHB0003][X115000][X115000][X115000]"
            "_13:53:11,06-22-2015\r",
            key,
        )

        assert result.errors == ()
        assert result.ok is True
        frame = result.frame
        assert frame is not None
        assert (frame.crc.received, frame.crc.calculated, frame.crc.valid) == (
            "7C37",
            "7C37",
            True,
        )
        assert frame.length.valid is True
        assert frame.protocol == "SIA-DCS"
        assert frame.sequence == "0000"
        assert frame.receiver == "C0FFD"
        assert frame.line_prefix == "1234"
        assert frame.account == "080027E62A64"
        assert frame.body == "#080027E62A64|NHB0003"
        assert frame.extended_data == (
            Dc09ExtendedData("X", "115000"),
            Dc09ExtendedData("X", "115000"),
            Dc09ExtendedData("X", "115000"),
        )
        assert frame.timestamp_utc == datetime(2015, 6, 22, 13, 53, 11, tzinfo=UTC)
        assert frame.timestamp_drift_seconds == 0

    def test_reports_crc_mismatch_instead_of_raising(self, key: bytes) -> None:
        result = decode_text(
            '\n0000006b"SIA-DCS"0000RC0FFDL1234#080027E62A64'
            "[#080027E62A64|NHB0003]_13:53:11,06-22-2015\r",
            key,
        )

        assert result.ok is False
        assert "CRC mismatch" in " ".join(result.errors)

    def test_reports_length_mismatch(self, key: bytes) -> None:
        result = decode_text('\nB0AC0099"ADM-CID"0001L0#7099[#7099|1628 01 000]\r', key)

        assert result.ok is False
        assert "Length mismatch" in " ".join(result.errors)

    def test_parses_message_without_receiver_field(self, key: bytes) -> None:
        result = decode_text(
            '\n2729002e"ACK"0001L0#080027E62A64[]_16:07:07,04-14-2015\r', key
        )

        assert result.ok is True
        assert result.frame is not None
        assert result.frame.receiver is None
        assert result.frame.line_prefix == "0"
        assert result.frame.account == "080027E62A64"

    def test_parses_static_nak_where_account_marker_is_a(self, key: bytes) -> None:
        result = decode_text('\n3C830025"NAK"0000R0L0A0[]_16:09:09,04-14-2015\r', key)

        assert result.ok is True
        assert result.frame is not None
        assert result.frame.receiver == "0"
        assert result.frame.line_prefix == "0"
        assert result.frame.account == "0"

    def test_treats_null_as_link_test(self, key: bytes) -> None:
        result = decode_text(
            '\n1D85002B"NULL"0001L0#00653210[]_09:17:32,05-18-2016\r', key
        )

        assert result.ok is True
        assert result.payload is not None
        assert result.payload.link_test is True
        enriched = enrich(result)
        assert enriched is not None
        assert "Link test" in enriched.summary

    def test_rejects_non_hex_length_field(self, key: bytes) -> None:
        result = decode_text(
            '\n01fc003g"ACK"0000Rc0ffdL1234#080027E62A64[]_12:10:40,06-22-2015\r',
            key,
        )

        assert result.ok is False
        assert "Length field" in " ".join(result.errors)


class TestEncryptedMessages:
    """AES-CBC handling with the DC-09 prefix padding scheme."""

    ENCRYPTED_SIA = (
        '\nE88800a7"*SIA-DCS"0000RC0FFDL1234#080027E62A64'
        "[A054F111518361720BAB126501C6D994CD9151D2C84881C0FEA933A0790F49A9"
        "236EBD45F2AED32FAA252EB1AA18FC3CDEDE2452F33185E5931D31308D9B9882\r"
    )
    ENCRYPTED_ACK = (
        '\nE34C0062"*ACK"0001R5678L1234#080027E62A64'
        "[14F088FF5DCB19D1908069507EAB97C7CDB20F1C6EFA550BB59864D54B2DFA1C\r"
    )

    def test_decrypts_encrypted_sia_dcs(self, key: bytes) -> None:
        result = decode_text(self.ENCRYPTED_SIA, key)

        assert result.ok is True
        frame = result.frame
        assert frame is not None
        assert frame.encrypted is True
        assert frame.protocol == "SIA-DCS"
        assert frame.body == "#080027E62A64|NHB0003"
        assert frame.extended_data == (Dc09ExtendedData("E", "115000"),)
        assert frame.timestamp == "15:12:40,06-22-2015"

    def test_decrypts_encrypted_ack_with_empty_data_block(self, key: bytes) -> None:
        result = decode_text(self.ENCRYPTED_ACK, key)

        assert result.ok is True
        assert result.frame is not None
        assert result.frame.body == ""
        assert result.frame.timestamp == "16:08:12,04-14-2015"

    def test_fails_clearly_when_the_key_is_wrong(self) -> None:
        wrong = parse_key("ABCDABCDABCDABCDABCDABCDABCDABCE")
        result = decode(wire_to_bytes(self.ENCRYPTED_SIA), key=wrong)

        assert result.ok is False

    def test_fails_clearly_when_no_key_is_configured(self) -> None:
        result = decode(wire_to_bytes(self.ENCRYPTED_ACK))

        assert "no AES key" in " ".join(result.errors)

    def test_round_trips_through_the_encrypter(self, key: bytes) -> None:
        from custom_components.sia_dc09.dc09 import decrypt_body, encrypt_body

        cipher = encrypt_body("#1234|Nri1/BA12]", key)

        assert decrypt_body(cipher, key) == "#1234|Nri1/BA12]"


class TestSiaPayload:
    """SIA-DCS body parsing."""

    def test_splits_modifier_from_code_and_operand(self, key: bytes) -> None:
        result = decode_text(
            '\n9B6D0059"SIA-DCS"0000RC0FFDL1234#080027E62A64'
            "[#080027E62A64|NHB0003][E115000]_14:25:17,06-22-2015\r",
            key,
        )

        assert result.payload is not None
        assert result.payload.account == "080027E62A64"
        assert len(result.payload.events) == 1
        event = result.payload.events[0]
        assert event.code == "HB"
        assert event.qualifier == "N"
        assert event.qualifier_meaning == "New event"
        assert event.address == "0003"
        assert event.area is None
        assert event.text is None
        assert event.source == "NHB0003"

    def test_parses_area_modifier_and_text_descriptors(self, key: bytes) -> None:
        result = decode_text(
            '\n953a004c"SIA-DCS"0001L0#7878'
            "[#7878|Nri01^TOTAL^/LX901^centrale d'alarme JA-106K(R)^]\r",
            key,
        )

        assert result.ok is True
        assert result.payload is not None
        blocks = result.payload.blocks
        assert (blocks[0].kind, blocks[0].code) == ("modifier", "N")
        assert (blocks[1].kind, blocks[1].code, blocks[1].value) == (
            "modifier",
            "ri",
            "01",
        )
        assert len(result.payload.events) == 1
        event = result.payload.events[0]
        assert event.code == "LX"
        assert event.address == "901"
        assert event.area == "01"
        assert event.text == "centrale d'alarme JA-106K(R)"

    def test_captures_user_and_partition_modifiers(self, key: bytes) -> None:
        from custom_components.sia_dc09.dc09 import parse_sia_payload

        payload = parse_sia_payload("#1234|Nri1/id0007/pm03/CL001")
        event = payload.events[0]

        assert event.code == "CL"
        assert event.area == "1"
        assert event.user == "0007"
        assert event.partition == "03"

    def test_backfills_modifiers_that_follow_the_event(self) -> None:
        from custom_components.sia_dc09.dc09 import parse_sia_payload

        payload = parse_sia_payload("#1234|NCL001/id0007")

        assert payload.events[0].user == "0007"


class TestCidPayload:
    """ADM-CID body parsing."""

    def test_splits_qualifier_code_group_and_zone(self, key: bytes) -> None:
        result = decode_text('\nB0AC0027"ADM-CID"0001L0#7099[#7099|1628 01 000]\r', key)

        assert result.ok is True
        assert result.payload is not None
        assert result.payload.account == "7099"
        event = result.payload.events[0]
        assert event.code == "628"
        assert event.qualifier == "1"
        assert event.area == "01"
        assert event.address == "000"


class TestEnrichment:
    """Joining decoded events to the code tables."""

    def test_adds_spreadsheet_metadata_to_a_sia_event(self, key: bytes) -> None:
        result = decode_text(
            '\n9B6D0059"SIA-DCS"0000RC0FFDL1234#080027E62A64'
            "[#080027E62A64|NHB0003][E115000]_14:25:17,06-22-2015\r",
            key,
        )
        enriched = enrich(result)

        assert enriched is not None
        event = enriched.events[0]
        assert event.code == "HB"
        assert event.known is True
        assert event.title == "Holdup Bypass"
        assert event.address_meaning == "Zone or point"
        assert event.address_label == "Zone or point 3"
        assert event.category == "Holdup"
        assert event.severity == "trouble"

    def test_classifies_a_burglary_alarm_as_an_alarm(self, key: bytes) -> None:
        result = decode_text('\n68AF0025"SIA-DCS"0001L0#1234[#1234|Nri1/BA12]\r', key)
        enriched = enrich(result)

        assert result.ok is True
        assert enriched is not None
        event = enriched.events[0]
        assert event.code == "BA"
        assert event.title == "Burglary Alarm"
        assert event.severity == "alarm"
        assert event.category == "Burglary"
        assert event.event.area == "1"
        assert event.address_label == "Zone or point 12"
        assert enriched.summary == "Burglary Alarm - Zone or point 12 (area 1)"

    def test_adds_contact_id_metadata(self, key: bytes) -> None:
        result = decode_text('\nA18D0027"ADM-CID"0001L0#7099[#7099|1131 01 003]\r', key)
        enriched = enrich(result)

        assert enriched is not None
        event = enriched.events[0]
        assert event.code == "131"
        assert event.known is True
        assert event.title == "Perimeter"
        assert event.category == "Alarm"
        assert event.address_meaning == "Zone or point"
        assert event.address_label == "Zone or point 3"

    def test_marks_unknown_codes_instead_of_dropping_them(self, key: bytes) -> None:
        result = decode_text('\n99A80025"SIA-DCS"0001L0#1234[#1234|Nri1/QQ99]\r', key)
        enriched = enrich(result)

        assert result.payload is not None
        assert any(block.kind == "unknown" for block in result.payload.blocks)
        assert enriched is not None
        assert enriched.events == ()


class TestResponses:
    """ACK, NAK and DUH builders."""

    def test_ack_echoes_the_request_addressing(self, key: bytes) -> None:
        result = decode_text(
            '\n7ea80059"SIA-DCS"0000Rc0ffdL1234#080027E62A64'
            "[#080027E62A64|NHB0003][E115000]_09:00:13,06-23-2015\r",
            key,
        )
        assert result.frame is not None
        ack = build_ack(
            result.frame, datetime(2015, 6, 22, 12, 10, 40, tzinfo=UTC)
        ).decode("latin-1")

        assert ack == (
            '\n01FC0037"ACK"0000Rc0ffdL1234#080027E62A64[]_12:10:40,06-22-2015\r'
        )
        assert decode_text(ack, key).ok is True

    def test_ack_omits_the_receiver_field_when_absent(self, key: bytes) -> None:
        result = decode_text(
            '\n2729002e"ACK"0001L0#080027E62A64[]_16:07:07,04-14-2015\r', key
        )
        assert result.frame is not None
        ack = build_ack(
            result.frame, datetime(2015, 4, 14, 16, 7, 7, tzinfo=UTC)
        ).decode("latin-1")

        assert ack == '\n2729002E"ACK"0001L0#080027E62A64[]_16:07:07,04-14-2015\r'

    def test_builds_the_static_nak(self) -> None:
        nak = build_nak(datetime(2015, 4, 14, 16, 9, 9, tzinfo=UTC)).decode("latin-1")

        assert nak == '\n3C830025"NAK"0000R0L0A0[]_16:09:09,04-14-2015\r'

    def test_duh_mirrors_the_ack_addressing(self, key: bytes) -> None:
        from custom_components.sia_dc09.dc09 import build_duh

        result = decode_text('\n68AF0025"SIA-DCS"0001L0#1234[#1234|Nri1/BA12]\r', key)
        assert result.frame is not None
        duh = build_duh(result.frame).decode("latin-1")

        assert '"DUH"0001L0#1234[]' in duh
        assert decode_text(duh, key).ok is True


class TestKeyParsing:
    """AES key acceptance rules."""

    @pytest.mark.parametrize("length", [32, 48, 64])
    def test_accepts_hex_keys_of_every_permitted_length(self, length: int) -> None:
        parsed = parse_key("A" * length)

        assert parsed is not None
        assert len(parsed) == length // 2

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_returns_none_for_an_empty_key(self, value: str | None) -> None:
        assert parse_key(value) is None

    def test_accepts_a_passphrase(self) -> None:
        # 16 characters are too short to be a valid hex key (that would be 8
        # bytes), so they are taken as a 128-bit passphrase instead.
        parsed = parse_key("0123456789abcdef")

        assert parsed == b"0123456789abcdef"

    def test_rejects_a_key_of_the_wrong_length(self) -> None:
        from custom_components.sia_dc09.dc09 import Dc09CryptoError

        with pytest.raises(Dc09CryptoError):
            parse_key("ABC")
