from __future__ import annotations

from app.services.placeholders import PlaceholderFiller


def test_fill_content_replaces_tokens():
    filler = PlaceholderFiller(session=None)  # session unused for fill_content
    ctx = {
        "sender": {"fullName": "Иванов", "account": {"number": "40817-100"}},
        "receiver": {"fullName": "Петров", "card": {"number": "4276-...-0001"}},
    }
    content = (
        '{"from": "{{sender.fullName}}", "fromAccount": "{{sender.account.number}}",'
        ' "to": "{{receiver.fullName}}", "toCard": "{{receiver.card.number}}",'
        ' "note": "{{sender.unknown.path}}"}'
    )
    rendered, unresolved = filler.fill_content(content, ctx)
    assert "Иванов" in rendered
    assert "40817-100" in rendered
    assert "Петров" in rendered
    assert "4276-...-0001" in rendered
    assert unresolved == ["sender.unknown.path"]
