"""Header-preset CRUD: reusable endpoint URL + headers, tagged by project.

A preset bundles a standard ``url`` and a list of HTTP headers under one project
label. The template UI offers only presets matching a template's project; picking
one *copies* the url + headers onto the template (see :meth:`apply_to_template`),
so presets carry no live link and can be edited or deleted freely.
"""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HeaderPreset, MessageTemplate
from app.repositories.header_preset import HeaderPresetRepository
from app.repositories.project import ProjectRepository
from app.schemas.header_preset import HeaderPresetCreate, HeaderPresetUpdate, PresetHeaderIn
from app.utils.errors import NotFoundError, ValidationFailed

# A header value carrying a ``{{token}}`` placeholder (e.g. ``{{rquid}}``) is
# dynamic — its value is resolved at send time. Unlike the importer's key-based
# ``apply_dynamic_headers``, presets carry the token in the *value*, so detection
# is value-based here.
_DYNAMIC_VALUE_RE = re.compile(r"\{\{\s*[^}]+\s*\}\}")


def header_mode(value: str) -> str:
    return "dynamic" if _DYNAMIC_VALUE_RE.search(value or "") else "literal"


def normalize_preset_headers(rows: list[PresetHeaderIn] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the shared header shape from editor rows.

    Each entry → ``{"key","value","mode","original","disabled"}``. ``mode`` is
    derived from the value (``dynamic`` when it contains ``{{…}}``); the value is
    stored verbatim (``{{rquid}}`` is *not* canonicalised) and mirrored into
    ``original``. Rows with a blank key are dropped.
    """

    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, PresetHeaderIn):
            key, value = row.key, row.value
        else:
            key = str(row.get("key", ""))
            value = str(row.get("value", ""))
        key = key.strip()
        if not key:
            continue
        out.append(
            {
                "key": key,
                "value": value,
                "mode": header_mode(value),
                "original": value,
                "disabled": False,
            }
        )
    return out


class HeaderPresetService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = HeaderPresetRepository(session)
        self.projects = ProjectRepository(session)

    async def list_all(self) -> list[HeaderPreset]:
        return await self.repo.list_all()

    async def list_by_project(self, project_id: uuid.UUID) -> list[HeaderPreset]:
        return await self.repo.list_by_project(project_id)

    async def get(self, preset_id: uuid.UUID) -> HeaderPreset:
        preset = await self.repo.get(preset_id)
        if preset is None:
            raise NotFoundError("Пресет не найден")
        return preset

    async def _require_project(self, project_id: uuid.UUID) -> None:
        if await self.projects.get(project_id) is None:
            raise ValidationFailed("Проект не найден")

    async def _require_unique(
        self, project_id: uuid.UUID, name: str, *, exclude: uuid.UUID | None = None
    ) -> None:
        existing = await self.repo.get_by_name_in_project(project_id, name)
        if existing is not None and existing.id != exclude:
            raise ValidationFailed("Пресет с таким именем уже есть в этом проекте")

    async def create(self, data: HeaderPresetCreate) -> HeaderPreset:
        name = data.name.strip()
        if not name:
            raise ValidationFailed("Название пресета не может быть пустым")
        await self._require_project(data.project_id)
        await self._require_unique(data.project_id, name)
        return await self.repo.add(
            HeaderPreset(
                name=name,
                project_id=data.project_id,
                url=data.url.strip(),
                headers=normalize_preset_headers(data.headers),
            )
        )

    async def update(self, preset_id: uuid.UUID, data: HeaderPresetUpdate) -> HeaderPreset:
        preset = await self.get(preset_id)
        # Resolve the target project first — uniqueness is checked against it.
        project_id = data.project_id if data.project_id is not None else preset.project_id
        if data.project_id is not None and data.project_id != preset.project_id:
            await self._require_project(data.project_id)
        if data.name is not None:
            name = data.name.strip()
            if not name:
                raise ValidationFailed("Название пресета не может быть пустым")
            await self._require_unique(project_id, name, exclude=preset.id)
            preset.name = name
        elif data.project_id is not None:
            await self._require_unique(project_id, preset.name, exclude=preset.id)
        if data.project_id is not None:
            preset.project_id = data.project_id
        if data.url is not None:
            preset.url = data.url.strip()
        if data.headers is not None:
            preset.headers = normalize_preset_headers(data.headers)
        await self.session.flush()
        return preset

    async def delete(self, preset_id: uuid.UUID) -> None:
        preset = await self.get(preset_id)
        await self.repo.delete(preset)

    @staticmethod
    def apply_to_template(template: MessageTemplate, preset: HeaderPreset) -> None:
        """Copy a preset's URL + headers onto a template (replace, not merge).

        Deep-copies the header dicts so later edits to the preset don't mutate the
        template's stored JSONB. The HTTP method is left untouched.
        """

        template.url = preset.url or ""
        template.headers = copy.deepcopy(preset.headers or [])
