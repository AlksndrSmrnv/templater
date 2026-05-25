"""Prompt construction. Kept separate from the client / service so that prompts
can evolve independently and be unit-tested without LLM access."""

from __future__ import annotations

import json


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
            "В поле `leaves` массив JSON Pointer-путей всех листовых значений шаблона.\n"
            "В ответе `placeholders[].location` ОБЯЗАТЕЛЬНО возвращай ровно строку из\n"
            "этого массива, без изменений; значение листа смотри в `template` по этому пути.\n"
            "Формат location: \"location\": \"/RqHdr/RqUID\" — один JSON Pointer,\n"
            "начинается с `/`, сегменты разделены `/`, без квадратных скобок и без\n"
            "точечной нотации.\n"
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
            "Твоя задача: для каждого литерального значения предположить, какое поле из каталога\n"
            "его заменяет, если такое есть. Если очевидной замены нет — пропусти.\n"
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
            "  \"placeholders\": [{\"location\": str, \"suggestion\": str | null}]\n"
            "}\n"
        )
        user_payload = {
            "format": fmt,
            "template": content,
            "leaves": leaves,
            "catalog": catalog,
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
