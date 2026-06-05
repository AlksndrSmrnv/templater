"""Prompt construction. Kept separate from the client / service so that prompts
can evolve independently and be unit-tested without LLM access."""

from __future__ import annotations

import json


def _compact_json_for_prompt(content: str) -> str:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return content

    cleaned = _remove_none_object_keys(parsed)
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))


def _remove_none_object_keys(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _remove_none_object_keys(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_remove_none_object_keys(item) for item in value]
    return value


def _slim_catalog(catalog: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {key: value for key, value in entry.items() if key in ("path", "label")}
        for entry in catalog
    ]


_MAX_LEAF_VALUE_CHARS = 120


def _format_leaf_value(value: str) -> str:
    """Single-line, length-capped rendering of a leaf value for the prompt."""

    flat = " ".join(str(value).split())
    if len(flat) > _MAX_LEAF_VALUE_CHARS:
        return flat[:_MAX_LEAF_VALUE_CHARS] + "…"
    return flat


def _one_line(value: str, limit: int) -> str:
    """Collapse whitespace and cap length — keeps catalog rows to one short line
    so the prompt stays small for a weak model on a tight token budget."""

    flat = " ".join(str(value or "").split())
    if len(flat) > limit:
        return flat[:limit] + "…"
    return flat


class PromptBuilder:
    """Builds system + user prompts for tasks we ask the LLM to perform."""

    @staticmethod
    def build_template_field_mapping(
        *,
        leaves: list[dict[str, str]],
        catalog: list[dict[str, str]],
    ) -> tuple[str, str, dict[str, str]]:
        """Build the field-mapping prompt.

        Each leaf gets a short id (``L1``, ``L2`` …) so the model never has to
        echo a long JSON-pointer / XML path back byte-exact — it answers with the
        id, and we expand it to the real ``location`` via the returned map.

        Returns ``(system_prompt, user_prompt, id_to_location)``.
        """

        system_prompt = (
            "Ты размечаешь шаблон банковского сообщения для тестирования: подбираешь "
            "каждому значению из шаблона подходящее поле тестовых данных.\n"
            "\n"
            "Тебе дают листья шаблона (id, путь, значение) и каталог полей "
            "(путь — описание).\n"
            "Для каждого листа, которому подходит поле из каталога, верни его id и путь "
            "поля. Если подходящего поля нет — просто пропусти лист, не добавляй его в "
            "ответ.\n"
            "\n"
            "Роли участников (видны в начале пути и в описании поля):\n"
            "  • sender.* — отправитель/плательщик (инициатор операции).\n"
            "  • receiver.* — получатель (адресат операции).\n"
            "  • accountOwner.* — третья сторона: владелец/держатель счёта или карты, "
            "бенефициар (ownerName, accountHolder, владелецСчёта, держательКарты, блоки "
            "owner/holder). Не сворачивай его в sender или receiver — это отдельная "
            "сторона.\n"
            "\n"
            "Пример.\n"
            "Листья:\n"
            "L1  /Payer/INN = 7701234567\n"
            "L2  /Note = спасибо\n"
            "Каталог:\n"
            "sender.inn — sender/client — ИНН\n"
            "Ответ: {\"placeholders\":[{\"leaf\":\"L1\",\"field\":\"sender.inn\"}]}\n"
            "(L2 пропущен: подходящего поля нет.)\n"
            "\n"
            "Ответь СТРОГО валидным JSON без пояснений:\n"
            "{\"placeholders\":[{\"leaf\":str,\"field\":str}]}\n"
        )

        id_to_location: dict[str, str] = {}
        leaf_lines: list[str] = []
        for idx, leaf in enumerate(leaves, start=1):
            leaf_id = f"L{idx}"
            location = leaf.get("location", "")
            id_to_location[leaf_id] = location
            value = _format_leaf_value(leaf.get("value", ""))
            leaf_lines.append(f"{leaf_id}  {location} = {value}")

        catalog_lines = [
            f"{entry['path']} — {entry['label']}" for entry in _slim_catalog(catalog)
        ]

        user_prompt = (
            "Листья шаблона (id, путь, значение):\n"
            + "\n".join(leaf_lines)
            + "\n\nКаталог полей (путь — описание):\n"
            + "\n".join(catalog_lines)
        )
        return system_prompt, user_prompt, id_to_location

    @staticmethod
    def build_transfer_template_pick(
        *,
        request: str,
        templates: list[dict[str, str]],
    ) -> tuple[str, str]:
        """Pick ONE template id matching the user's free-form transfer request.

        ``templates`` are pre-condensed rows: ``{"id": "T1", "summary": str}``.
        Only LLM-processed templates are passed in by the caller. The short ids
        keep the model from echoing long UUIDs.
        """

        system_prompt = (
            "Ты подбираешь шаблон банковского перевода по запросу пользователя.\n"
            "Дан список шаблонов: id — краткое описание.\n"
            "Выбери ОДИН id наиболее подходящего шаблона.\n"
            "Ответь СТРОГО валидным JSON без пояснений: {\"template\": \"T1\"}.\n"
            "Если ничего не подходит — {\"template\": null}.\n"
        )
        rows = [
            f"{row['id']} — {_one_line(row.get('summary', ''), 200)}"
            for row in templates
        ]
        user_prompt = (
            f"Запрос: {_one_line(request, 400)}\n\nШаблоны:\n" + "\n".join(rows)
        )
        return system_prompt, user_prompt

    @staticmethod
    def build_transfer_participants(
        *,
        request: str,
        clients: list[dict[str, str]],
        accounts: list[dict[str, str]],
        cards: list[dict[str, str]],
        need_account_owner: bool,
    ) -> tuple[str, str]:
        """Pick participants (client/account/card per role) for the transfer.

        Rows are pre-condensed short-id catalogs:
          clients  — ``{"id": "C1", "traits": str, "description": str}``
          accounts — ``{"id": "A1", "client": "C1", "currency": str, "description": str}``
          cards    — ``{"id": "K1", "account": "A1", "description": str}``
        The model answers with short ids only; the caller expands them to UUIDs.
        """

        roles = "sender (отправитель/плательщик), receiver (получатель)"
        owner_rule = ""
        owner_answer = ""
        if need_account_owner:
            roles += (
                ", accountOwner (владелец счёта/карты — третья сторона, не отправитель и "
                "не получатель)"
            )
            owner_rule = "Заполни accountOwner, если он подразумевается запросом.\n"
            owner_answer = ',"accountOwner":{"client":"C3","account":null,"card":null}'

        system_prompt = (
            "Ты подбираешь участников банковского перевода из тестовых данных по запросу.\n"
            f"Роли: {roles}.\n"
            "Для каждой нужной роли выбери клиента (C..). Если в запросе указан счёт или "
            "карта в конкретной валюте — выбери и конкретный счёт (A..) и/или карту (K..) "
            "этого клиента; иначе оставь null.\n"
            "Ориентируйся на описания и признаки сущностей.\n"
            f"{owner_rule}"
            "Ответь СТРОГО валидным JSON без пояснений:\n"
            '{"sender":{"client":"C1","account":null,"card":null},'
            '"receiver":{"client":"C2","account":null,"card":null}'
            f"{owner_answer}}}\n"
            "Если роль не нужна — поставь её значение null.\n"
        )

        client_rows = [
            f"{row['id']} | {_one_line(row.get('traits', ''), 60) or '—'} | "
            f"{_one_line(row.get('description', ''), 160) or '—'}"
            for row in clients
        ]
        account_rows = [
            f"{row['id']} | {row.get('client', '')} | "
            f"{_one_line(row.get('currency', ''), 24) or '—'} | "
            f"{_one_line(row.get('description', ''), 160) or '—'}"
            for row in accounts
        ]
        card_rows = [
            f"{row['id']} | {row.get('account', '')} | "
            f"{_one_line(row.get('description', ''), 160) or '—'}"
            for row in cards
        ]
        user_prompt = (
            f"Запрос: {_one_line(request, 400)}\n\n"
            "Клиенты (id | признаки | описание):\n" + "\n".join(client_rows) + "\n\n"
            "Счета (id | клиент | валюта | описание):\n" + "\n".join(account_rows) + "\n\n"
            "Карты (id | счёт | описание):\n" + "\n".join(card_rows)
        )
        return system_prompt, user_prompt

    @staticmethod
    def build_template_meta(*, content: str, fmt: str) -> tuple[str, str]:
        system_prompt = (
            "Ты анализируешь JSON-шаблон банковского денежного перевода.\n"
            "Дай точное описание сути перевода в 2–4 предложениях (поле summary). "
            "В описании обязательно отрази:\n"
            "  • канал, в котором проводится перевод (канал операции);\n"
            "  • тип перевода по productId и связке счёт/карта: TDD/A2A — со счёта на счёт; "
            "TDC/A2C — со счёта на карту; TCD/C2A — с карты на счёт; TCC/C2C — с карты на "
            "карту (и аналогичные);\n"
            "  • кому адресован перевод: самому себе (плательщик и получатель совпадают), "
            "другому клиенту этого же банка (another_int), или клиенту в другой банк "
            "(another_ext);\n"
            "  • с каких счетов/карт и в какой валюте идёт перевод, есть ли конверсия валют;\n"
            "  • из какого подразделения-источника банка инициирован перевод;\n"
            "  • есть ли в шаблоне владелец счёта (accountOwner — третья сторона).\n"
            "Описывай только СУТЬ перевода. НЕ указывай конкретные динамические значения: "
            "суммы, имена/идентификаторы клиентов, номера счетов и карт.\n"
            "Ответ — валидный JSON строго вида: {\"summary\": str}.\n"
        )
        body = _compact_json_for_prompt(content) if fmt == "json" else content
        user_prompt = f"Формат: {fmt}\n\n{body}"
        return system_prompt, user_prompt
