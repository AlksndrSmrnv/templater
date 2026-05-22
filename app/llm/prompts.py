"""Prompt construction. Kept separate from the client / service so that prompts
can evolve independently and be unit-tested without LLM access."""

from __future__ import annotations

import json
from typing import Any


class PromptBuilder:
    """Builds system + user prompts for tasks we ask the LLM to perform."""

    @staticmethod
    def build_template_field_mapping(
        *,
        content: str,
        fmt: str,
        leaves: list[dict[str, Any]],
        catalog: list[dict[str, str]],
    ) -> tuple[str, str]:
        system_prompt = (
            "Ты помогаешь подготовить шаблон сообщения для тестирования банковской системы.\n"
            "Тебе дают исходный шаблон (JSON или XML) и список листовых значений в нём с путями.\n"
            "Также дают каталог доступных полей тестовых данных "
            "(sender.*, receiver.*, accountOwner.*).\n"
            "accountOwner — владелец счёта, третья сторона, отличная от отправителя и получателя; "
            "поля вида ownerName, accountHolder, владелецСчета размечай как accountOwner.*.\n"
            "Твоя задача: для каждого литерального значения предположить, какое поле из каталога\n"
            "его заменяет, если такое есть. Если очевидной замены нет — пропусти.\n"
            "Дополнительно опиши, что это за шаблон (summary, category, scenarios).\n"
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
            "Дай краткое summary (1–2 предложения), category (тип операции, например 'перевод' или 'выписка'),\n"
            "и список scenarios (короткие фразы, описывающие частные случаи использования).\n"
            "Ответ — валидный JSON: {\"summary\": str, \"category\": str, \"scenarios\": [str]}.\n"
        )
        user_payload = {"format": fmt, "template": content}
        user_prompt = json.dumps(user_payload, ensure_ascii=False, indent=2)
        return system_prompt, user_prompt
