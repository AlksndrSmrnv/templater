"""Prompt construction. Kept separate from the client / service so that prompts
can evolve independently and be unit-tested without LLM access.

The four *system* instructions are stored as editable defaults in
:data:`PROMPT_DEFS`. They can be overridden at runtime from the database
(``app_settings``); :func:`load_prompt_overrides` reads those overrides and the
caller hands them to :class:`PromptBuilder`. The dynamic *user* prompts (built
from template leaves / catalog rows) stay in code — only the instructions are
user-editable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from string import Template
from typing import TYPE_CHECKING

from app.utils.errors import ValidationFailed

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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


@dataclass(frozen=True)
class PromptDef:
    """Editable system instruction: its key, human metadata and default text."""

    key: str
    title: str
    description: str
    default: str
    variables: list[str] = field(default_factory=list)


# --- Default system instructions ---------------------------------------------
# Edit these to change behaviour out of the box; runtime overrides from the DB
# take precedence (see PromptBuilder / load_prompt_overrides).

_FIELD_MAPPING_DEFAULT = (
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
    "Инструменты перевода — откуда списываются и куда зачисляются деньги:\n"
    "  • srcPayTool (и поля с префиксом src) — ИСТОЧНИК СПИСАНИЯ: это инструмент "
    "ОТПРАВИТЕЛЯ. Размечай его поля как sender.* — даже если в пути нет слова "
    "sender.\n"
    "  • dstPayTool (и поля с префиксом dst) — НАЗНАЧЕНИЕ ЗАЧИСЛЕНИЯ: это инструмент "
    "ПОЛУЧАТЕЛЯ. Размечай его поля как receiver.* — даже если в пути нет слова "
    "receiver.\n"
    "  Внутри srcPayTool/dstPayTool лежит ОДИН вложенный объект, его ключ задаёт "
    "тип инструмента:\n"
    "    – depAcctId (или depAcct, account, acct) — это СЧЁТ: поля внутри размечай "
    "как <роль>.account.* (например sender.account.number).\n"
    "    – cardAcctId (или card) — это КАРТА: поля внутри размечай как "
    "<роль>.card.* (например sender.card.number).\n"
    "  Размечай ВСЕ содержательные поля инструмента, а не только номер: номер "
    "счёта/карты, тип и уровень карты, реквизиты подразделения банка "
    "(agency/branch/region), валюту и т.п. — каждое поле сопоставь наиболее "
    "подходящему полю каталога этой роли и сущности по его описанию.\n"
    "\n"
    "Поле clientId в объектах sender / recipient — это идентификатор клиента: "
    "размечай его ВСЕГДА (sender.clientId или receiver.clientId), никогда не "
    "пропускай.\n"
    "Получателя в шаблоне могут звать recipient или payee — это та же роль "
    "receiver.*.\n"
    "\n"
    "Оператор (operator/Operator) — это сотрудник банка, оформляющий операцию, а НЕ "
    "участник перевода. Его поля (имя, фамилия, отчество и любые другие) НЕ размечай: "
    "пропусти такие листья, оставь значение как в оригинале. Не путай оператора с "
    "sender/receiver/accountOwner.\n"
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
    "Пример 2 (инструменты перевода и clientId).\n"
    "Листья:\n"
    "L1  /srcPayTool/cardAcctId/cardNumber = 5536...0001\n"
    "L2  /srcPayTool/cardAcctId/branch = 9038/01\n"
    "L3  /dstPayTool/depAcctId/accountNumber = 40817...\n"
    "L4  /recipient/clientId = 1002345\n"
    "Каталог:\n"
    "sender.card.number — sender/card — Номер карты\n"
    "sender.card.branch — sender/card — Подразделение\n"
    "receiver.account.number — receiver/account — Номер счёта\n"
    "receiver.clientId — receiver/client — Идентификатор клиента\n"
    "Ответ: {\"placeholders\":["
    "{\"leaf\":\"L1\",\"field\":\"sender.card.number\"},"
    "{\"leaf\":\"L2\",\"field\":\"sender.card.branch\"},"
    "{\"leaf\":\"L3\",\"field\":\"receiver.account.number\"},"
    "{\"leaf\":\"L4\",\"field\":\"receiver.clientId\"}]}\n"
    "\n"
    "Ответь СТРОГО валидным JSON без пояснений:\n"
    "{\"placeholders\":[{\"leaf\":str,\"field\":str}]}\n"
)

_TRANSFER_PICK_DEFAULT = (
    "Ты подбираешь шаблон банковского перевода по запросу пользователя.\n"
    "Дан список шаблонов: id — краткое описание.\n"
    "Выбери ОДИН id наиболее подходящего шаблона.\n"
    "Ответь СТРОГО валидным JSON без пояснений: {\"template\": \"T1\"}.\n"
    "Если ничего не подходит — {\"template\": null}.\n"
)

# Uses string.Template placeholders ($roles / $owner_rule / $owner_answer) — the
# caller fills them based on whether an accountOwner role is needed. Dollar
# syntax is used deliberately so the literal JSON braces below are left intact.
_TRANSFER_PARTICIPANTS_DEFAULT = (
    "Ты подбираешь участников банковского перевода из тестовых данных по запросу.\n"
    "Роли: $roles.\n"
    "Для каждой нужной роли выбери клиента (C..). Если в запросе указан счёт или "
    "карта в конкретной валюте — выбери и конкретный счёт (A..) и/или карту (K..) "
    "этого клиента; иначе оставь null.\n"
    "Ориентируйся на описания и признаки сущностей.\n"
    "$owner_rule"
    "Ответь СТРОГО валидным JSON без пояснений:\n"
    '{"sender":{"client":"C1","account":null,"card":null},'
    '"receiver":{"client":"C2","account":null,"card":null}'
    "$owner_answer}\n"
    "Если роль не нужна — поставь её значение null.\n"
)

_TEMPLATE_META_DEFAULT = (
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


PROMPT_DEFS: dict[str, PromptDef] = {
    "field_mapping": PromptDef(
        key="field_mapping",
        title="Разметка полей шаблона",
        description=(
            "Сопоставляет листья шаблона с полями тестовых данных при анализе/импорте "
            "шаблона."
        ),
        default=_FIELD_MAPPING_DEFAULT,
    ),
    "template_meta": PromptDef(
        key="template_meta",
        title="Мета-описание шаблона",
        description="Генерирует краткое описание (summary) сути перевода в шаблоне.",
        default=_TEMPLATE_META_DEFAULT,
    ),
    "transfer_pick": PromptDef(
        key="transfer_pick",
        title="Выбор шаблона перевода",
        description="Ассистент: выбирает один подходящий шаблон по запросу пользователя.",
        default=_TRANSFER_PICK_DEFAULT,
    ),
    "transfer_participants": PromptDef(
        key="transfer_participants",
        title="Выбор участников перевода",
        description=(
            "Ассистент: подбирает клиента/счёт/карту для каждой роли перевода."
        ),
        default=_TRANSFER_PARTICIPANTS_DEFAULT,
        variables=["$roles", "$owner_rule", "$owner_answer"],
    ),
}


def prompt_setting_key(prompt_key: str) -> str:
    """Namespaced ``app_settings`` key for a prompt override."""

    return f"llm_prompt:{prompt_key}"


# Variables the participants prompt may reference (the only templated prompt).
_PARTICIPANTS_VARS = {"roles": "", "owner_rule": "", "owner_answer": ""}


def validate_prompt_text(prompt_key: str, text: str) -> None:
    """Reject a saved prompt that would fail at render time.

    Only ``transfer_participants`` is run through :class:`string.Template`, so
    only it can carry placeholders. ``substitute`` (not ``safe_substitute``)
    surfaces an unknown ``$placeholder`` (``KeyError``) or a stray/invalid ``$``
    such as ``$100`` (``ValueError``) — we turn either into a user-facing error
    at save time instead of letting it blow up a later LLM run. Blank text is
    allowed: it clears the override.
    """

    if not text.strip() or prompt_key != "transfer_participants":
        return
    try:
        Template(text).substitute(_PARTICIPANTS_VARS)
    except KeyError as exc:
        raise ValidationFailed(
            f"Неизвестная переменная ${exc.args[0]} в промпте. "
            "Допустимы только $roles, $owner_rule, $owner_answer."
        ) from exc
    except ValueError as exc:
        raise ValidationFailed(
            "Некорректный символ $ в промпте: экранируйте как $$ "
            "или используйте $roles / $owner_rule / $owner_answer."
        ) from exc


async def load_prompt_overrides(session: AsyncSession) -> dict[str, str]:
    """Read non-empty prompt overrides from ``app_settings``.

    Returns ``{prompt_key: text}`` for every prompt that has a stored, non-blank
    override. Called once per LLM request so edits apply without a restart.
    """

    from app.repositories.settings import SettingsRepository

    repo = SettingsRepository(session)
    overrides: dict[str, str] = {}
    for prompt_key in PROMPT_DEFS:
        value = await repo.get(prompt_setting_key(prompt_key))
        if isinstance(value, str) and value.strip():
            overrides[prompt_key] = value
    return overrides


class PromptBuilder:
    """Builds system + user prompts for tasks we ask the LLM to perform.

    System instructions come from :data:`PROMPT_DEFS` defaults unless a non-blank
    override is supplied (loaded from the DB by :func:`load_prompt_overrides`).
    """

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self._overrides = overrides or {}

    def _system(self, key: str) -> str:
        """Override text if present and non-blank, else the coded default."""

        override = self._overrides.get(key)
        if isinstance(override, str) and override.strip():
            return override
        return PROMPT_DEFS[key].default

    def build_template_field_mapping(
        self,
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

        system_prompt = self._system("field_mapping")

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

    def build_transfer_template_pick(
        self,
        *,
        request: str,
        templates: list[dict[str, str]],
    ) -> tuple[str, str]:
        """Pick ONE template id matching the user's free-form transfer request.

        ``templates`` are pre-condensed rows: ``{"id": "T1", "summary": str}``.
        Only LLM-processed templates are passed in by the caller. The short ids
        keep the model from echoing long UUIDs.
        """

        system_prompt = self._system("transfer_pick")
        rows = [
            f"{row['id']} — {_one_line(row.get('summary', ''), 200)}"
            for row in templates
        ]
        user_prompt = (
            f"Запрос: {_one_line(request, 400)}\n\nШаблоны:\n" + "\n".join(rows)
        )
        return system_prompt, user_prompt

    def build_transfer_participants(
        self,
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

        system_prompt = Template(self._system("transfer_participants")).safe_substitute(
            roles=roles,
            owner_rule=owner_rule,
            owner_answer=owner_answer,
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

    def build_template_meta(self, *, content: str, fmt: str) -> tuple[str, str]:
        system_prompt = self._system("template_meta")
        body = _compact_json_for_prompt(content) if fmt == "json" else content
        user_prompt = f"Формат: {fmt}\n\n{body}"
        return system_prompt, user_prompt
