from __future__ import annotations

from app.services.role_resolver import resolve_role_from_path


def test_resolve_role_from_path_detects_receiver_synonyms() -> None:
    assert resolve_role_from_path("/recipient/personName/firstname") == "receiver"
    assert resolve_role_from_path("/payment/payeeInfo/firstName") == "receiver"
    assert resolve_role_from_path("/creditor/name") == "receiver"


def test_resolve_role_from_path_detects_account_owner_synonyms() -> None:
    assert resolve_role_from_path("/accountOwner/client/personName/firstname") == "accountOwner"
    assert resolve_role_from_path("/client/holder/personName/firstName") == "accountOwner"
    assert resolve_role_from_path("/счет/владелец/имя") == "accountOwner"


def test_resolve_role_from_path_detects_sender_synonyms_and_camel_case() -> None:
    assert resolve_role_from_path("/sender/personName/firstname") == "sender"
    assert resolve_role_from_path("/payment/PayerInfo/firstName") == "sender"
    assert resolve_role_from_path("/debtor/name") == "sender"


def test_resolve_role_from_path_returns_none_without_role_tokens() -> None:
    assert resolve_role_from_path("/operuid") is None
    assert resolve_role_from_path("/rquid") is None
    assert resolve_role_from_path("/totalAmount") is None


def test_resolve_role_from_path_prefers_inner_role_segment() -> None:
    assert resolve_role_from_path("/sender/recipient/personName/firstname") == "receiver"
    assert resolve_role_from_path("/receiver/accountOwner/personName/firstname") == "accountOwner"


def test_resolve_role_from_path_uses_priority_for_ambiguous_segment() -> None:
    assert resolve_role_from_path("/senderRecipient/personName/firstname") == "receiver"
    assert resolve_role_from_path("/payerAccountOwner/personName/firstname") == "accountOwner"


def test_resolve_role_from_path_handles_xml_path_noise() -> None:
    assert resolve_role_from_path("/root/payment[0]/recipient[1]/#text") == "receiver"
    assert resolve_role_from_path("/root/payment[0]/@payerId") == "sender"
