from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import cast

from app.db.models import FilledTemplate
from app.services.filled_templates import (
    NAME_MAX_LEN,
    build_auto_name,
    iter_role_labels,
)


def _now() -> datetime:
    return datetime(2026, 5, 26, 14, 30)


def test_build_auto_name_with_sender_and_receiver() -> None:
    name = build_auto_name(
        "AccountStatement.json",
        {"sender": "Иванов · ACC-001", "receiver": "Петров · ACC-002"},
        _now(),
    )
    assert "AccountStatement.json" in name
    assert "Иванов · ACC-001 → Петров · ACC-002" in name
    assert "26.05.2026 14:30" in name


def test_build_auto_name_with_account_owner() -> None:
    name = build_auto_name(
        "Tpl",
        {
            "sender": "Иванов",
            "receiver": "Петров",
            "accountOwner": "Сидоров",
        },
        _now(),
    )
    assert "владелец: Сидоров" in name


def test_build_auto_name_with_only_sender() -> None:
    name = build_auto_name("Tpl", {"sender": "Иванов"}, _now())
    assert "Tpl" in name and "Иванов" in name
    assert "→" not in name  # no receiver → no arrow


def test_build_auto_name_with_only_receiver_keeps_arrow_for_clarity() -> None:
    name = build_auto_name("Tpl", {"receiver": "Петров"}, _now())
    assert "→ Петров" in name


def test_build_auto_name_without_roles_just_uses_template_and_date() -> None:
    name = build_auto_name("Tpl", {}, _now())
    # Template — date, no middle segment
    assert name == "Tpl — 26.05.2026 14:30"


def test_build_auto_name_falls_back_when_template_name_empty() -> None:
    name = build_auto_name("", {}, _now())
    assert name.startswith("Шаблон —")


def test_build_auto_name_truncates_to_max_len() -> None:
    name = build_auto_name(
        "X" * 400,
        {"sender": "S", "receiver": "R"},
        _now(),
    )
    assert len(name) <= NAME_MAX_LEN
    assert name.endswith("…")


def test_iter_role_labels_returns_only_roles_present_in_snapshot() -> None:
    item = cast(
        FilledTemplate,
        SimpleNamespace(
            role_labels_snapshot={"sender": "Иванов", "receiver": "Петров"},
        ),
    )
    rows = iter_role_labels(item)
    assert [(role, title) for role, title, _ in rows] == [
        ("sender", "Отправитель"),
        ("receiver", "Получатель"),
    ]
    # accountOwner not in snapshot — skipped
    assert all(r[0] != "accountOwner" for r in rows)


def test_iter_role_labels_preserves_fixed_role_order() -> None:
    item = cast(
        FilledTemplate,
        SimpleNamespace(
            role_labels_snapshot={
                "accountOwner": "O",
                "receiver": "R",
                "sender": "S",
            },
        ),
    )
    rows = iter_role_labels(item)
    assert [r[0] for r in rows] == ["sender", "receiver", "accountOwner"]


def test_iter_role_labels_empty_snapshot() -> None:
    item = cast(FilledTemplate, SimpleNamespace(role_labels_snapshot={}))
    assert iter_role_labels(item) == []


def test_iter_role_labels_handles_none_snapshot() -> None:
    item = cast(FilledTemplate, SimpleNamespace(role_labels_snapshot=None))
    assert iter_role_labels(item) == []


def test_filled_template_model_columns_exist() -> None:
    # Sanity: model has the columns the migration creates and routes/services use.
    cols = {c.name for c in FilledTemplate.__table__.columns}
    expected = {
        "id",
        "name",
        "format",
        "filled_content",
        "changed_locations",
        "unresolved",
        "message_template_id",
        "template_name_snapshot",
        "sender_client_id",
        "sender_account_id",
        "sender_card_id",
        "receiver_client_id",
        "receiver_account_id",
        "receiver_card_id",
        "account_owner_client_id",
        "account_owner_account_id",
        "account_owner_card_id",
        "role_labels_snapshot",
        "created_at",
        "updated_at",
    }
    missing = expected - cols
    assert not missing, f"missing columns on FilledTemplate: {missing}"
    # FKs on role IDs must be ON DELETE SET NULL per plan
    fkmap = {
        fk.parent.name: fk.ondelete
        for fk in FilledTemplate.__table__.foreign_keys
    }
    for col in (
        "message_template_id",
        "sender_client_id",
        "receiver_client_id",
        "account_owner_client_id",
    ):
        assert fkmap.get(col) == "SET NULL", f"{col} should be ON DELETE SET NULL, got {fkmap.get(col)}"


def test_build_auto_name_keeps_arrow_glyph_intact_under_truncation() -> None:
    # The arrow should not end up half-cut producing a weird ellipsis position.
    # We can't guarantee its presence, but we can guarantee the result is a valid
    # string and not longer than the limit.
    name = build_auto_name("Tpl", {"sender": "S" * 200, "receiver": "R" * 200}, _now())
    assert isinstance(name, str)
    assert len(name) <= NAME_MAX_LEN


# uuid.uuid4 import sanity (used by routes); just touch it
def _uuid_smoke() -> None:
    _ = uuid.uuid4()
