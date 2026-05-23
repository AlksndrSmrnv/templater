from __future__ import annotations

from app.utils.paths import path_segments, pointer_path_segments, segment_tokens


def test_path_segments_support_template_and_catalog_notation() -> None:
    assert path_segments("/root/accountOwner[0][1]/client[2]/firstName") == [
        "root",
        "accountOwner",
        "client",
        "firstName",
    ]
    assert path_segments("accountOwner.client.personName.firstName") == [
        "accountOwner",
        "client",
        "personName",
        "firstName",
    ]
    assert path_segments("/root/@type/#text/0") == ["root", "type"]
    assert path_segments("/root/foo~1bar/baz~0qux") == ["root", "foo/bar", "baz~qux"]


def test_pointer_path_segments_preserve_dots_inside_json_pointer_keys() -> None:
    assert pointer_path_segments("/owner.sender/firstName") == ["owner.sender", "firstName"]


def test_segment_tokens_splits_dots_and_camel_case() -> None:
    assert segment_tokens("owner.sender") == {"owner", "sender"}
    assert segment_tokens("accountOwner") == {"account", "owner"}
