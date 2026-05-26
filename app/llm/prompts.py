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


class PromptBuilder:
    """Builds system + user prompts for tasks we ask the LLM to perform."""

    @staticmethod
    def build_template_field_mapping(
        *,
        content: str,
        fmt: str,
        leaves: list[str],
        catalog: list[dict[str, str]],
    ) -> tuple[str, str]:
        system_prompt = (
            "Ты помогаешь подготовить шаблон сообщения для тестирования банковской системы.\n"
            "Тебе дают исходный шаблон (JSON или XML), список путей листовых значений\n"
            "и каталог доступных полей тестовых данных.\n"
            "\n"
            "В поле `leaves` массив путей всех листовых значений шаблона. В ответе\n"
            "`placeholders[].location` ОБЯЗАТЕЛЬНО возвращай строку из этого массива\n"
            "побайтово равной исходной строке: без изменений, нормализации, сокращения\n"
            "или перевода в точечную нотацию; значение листа смотри в `template` по этому пути.\n"
            "Для JSON это JSON Pointer, например \"location\": \"/RqHdr/RqUID\".\n"
            "Для XML путь может содержать индексы `[idx]`, текстовый маркер `#text` и\n"
            "атрибуты `@attr`; не нормализуй и не удаляй эти части.\n"
            "\n"
            "В системе три РОЛИ участников; роль каждого поля каталога видна в начале path\n"
            "и в label:\n"
            "  • sender.* — отправитель (инициатор операции, плательщик).\n"
            "  • receiver.* — получатель (адресат операции).\n"
            "  • accountOwner.* — ВЛАДЕЛЕЦ СЧЁТА: третья сторона, чей счёт/карта фигурируют\n"
            "    в операции, но кто не является ни отправителем, ни получателем.\n"
            "\n"
            "ВАЖНО про accountOwner: если в шаблоне описан участник, который НЕ отправитель\n"
            "и НЕ получатель (владелец/держатель счёта или карты, бенефициар, третье лицо;\n"
            "поля вида ownerName, accountHolder, владелецСчета, держательКарты, блоки\n"
            "owner/holder/accountOwner) — его поля ОБЯЗАТЕЛЬНО размечай как accountOwner.*.\n"
            "Не сворачивай такого участника в sender или receiver: это разные стороны.\n"
            "\n"
            "Твоя задача: для литеральных значений выбрать подходящее поле из каталога,\n"
            "если оно есть. В ответе `placeholders` включай ТОЛЬКО те `location`,\n"
            "для которых ты выбрал поле из каталога. Если подходящего поля нет — "
            "НЕ возвращай эту запись вовсе, не присылай её с пустым `suggestion`.\n"
            "Служебные поля конверта rqUID, operUID, rqTm, channelDateTime обрабатываются\n"
            "отдельно как динамические параметры ({{rqUID}}, {{operUID}}, {{rqTm}},\n"
            "{{channelDateTime}}). Не предлагай для них замену на поля участников.\n"
            "\n"
            "Дополнительно опиши шаблон (summary, category, scenarios). В проекте только\n"
            "JSON денежных переводов: прочитай каждый параметр и сделай summary максимально\n"
            "точным, в 2–4 предложения. Обязательно отрази: тип перевода по productId\n"
            "(TDD/A2A — перевод со счёта на счёт; another_int — перевод другому клиенту\n"
            "этого же банка; another_ext — перевод клиенту в другой банк; если плательщик\n"
            "и получатель совпадают, укажи перевод самому себе), Подразделение-источник\n"
            "банка, канал операции, валюту перевода, Комиссию — есть/нет и в какой валюте.\n"
            "\n"
            "Отвечай СТРОГО в виде валидного JSON без пояснений:\n"
            "{\n"
            "  \"meta\": {\"summary\": str, \"category\": str, \"scenarios\": [str]},\n"
            "  \"placeholders\": [{\"location\": str, \"suggestion\": str}]\n"
            "}\n"
        )
        prompt_content = _compact_json_for_prompt(content) if fmt == "json" else content
        user_payload = {
            "format": fmt,
            "template": prompt_content,
            "leaves": leaves,
            "catalog": _slim_catalog(catalog),
        }
        user_prompt = json.dumps(user_payload, ensure_ascii=False, indent=2)
        return system_prompt, user_prompt

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
        user_payload = {"format": fmt, "template": content}
        user_prompt = json.dumps(user_payload, ensure_ascii=False, indent=2)
        return system_prompt, user_prompt
