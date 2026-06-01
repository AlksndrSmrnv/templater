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
    def build_template_meta(*, content: str, fmt: str) -> tuple[str, str]:
        system_prompt = (
            "Ты анализируешь шаблон сообщения банковской системы.\n"
            "В проекте только JSON денежных переводов: прочитай каждый параметр и дай подробное\n"
            "summary в 2–4 предложения. Обязательно отрази тип перевода по productId\n"
            "(TDD/A2A — перевод со счёта на счёт; another_int — перевод другому клиенту\n"
            "этого же банка; another_ext — перевод клиенту в другой банк; если плательщик\n"
            "и получатель совпадают, укажи перевод самому себе), Подразделение-источник\n"
            "банка, канал операции, валюту перевода, Комиссию — есть/нет и в какой валюте.\n"
            "Также верни category (тип операции, например 'перевод') и список scenarios\n"
            "(короткие фразы, описывающие частные случаи использования).\n"
            "Ответ — валидный JSON: {\"summary\": str, \"category\": str, \"scenarios\": [str]}.\n"
        )
        body = _compact_json_for_prompt(content) if fmt == "json" else content
        user_prompt = f"Формат: {fmt}\n\n{body}"
        return system_prompt, user_prompt
