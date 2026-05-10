from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import sqlite3
import threading
import uuid

from findme.search import (
    DEFAULT_FOLDER_QUALITY,
    DirectoryFilter,
    DirectoryFilterRule,
    DirectoryFilterState,
    SearchIndex,
    SearchPreferences,
)


PROFILE_NONE_ID = "none"
PROFILE_DEFAULT_ID = "default"
PROFILE_NONE_NAME = "None"
PROFILE_DEFAULT_NAME = "Default"

ProfileKind = str


class _Unset:
    pass


_UNSET = _Unset()


@dataclass(frozen=True)
class FolderSetting:
    source_id: str
    directory: str
    quality_override: int | None = None
    selection_state: DirectoryFilterState | None = None


@dataclass(frozen=True)
class PersistedSearchProfile:
    id: str
    name: str
    kind: ProfileKind
    version: int
    sort_order: int


@dataclass
class SearchProfileSession:
    search_session: str
    active_profile_id: str | None
    active_profile_name: str | None
    active_profile_kind: ProfileKind | None
    base_profile_version: int | None
    working_settings: dict[tuple[str, str], FolderSetting]
    persisted_settings: dict[tuple[str, str], FolderSetting]
    preferences: SearchPreferences

    @property
    def dirty_scoring(self) -> bool:
        return _scoring_settings(self.working_settings) != _scoring_settings(
            self.persisted_settings
        )

    @property
    def dirty_selection(self) -> bool:
        return _selection_settings(self.working_settings) != _selection_settings(
            self.persisted_settings
        )

    @property
    def dirty(self) -> bool:
        return self.dirty_scoring or self.dirty_selection

    @property
    def dirty_folder_keys(self) -> frozenset[tuple[str, str]]:
        return _dirty_folder_keys(self.working_settings, self.persisted_settings)


class SearchProfileConflict(RuntimeError):
    pass


class SearchProfileValidationError(ValueError):
    pass


class SearchProfileStore:
    def list_profiles(self, db: sqlite3.Connection) -> list[PersistedSearchProfile]:
        return [
            PersistedSearchProfile(
                id=row["id"],
                name=row["name"],
                kind=row["kind"],
                version=row["version"],
                sort_order=row["sort_order"],
            )
            for row in db.execute(
                """
                SELECT id, name, kind, version, sort_order
                FROM search_profiles
                ORDER BY sort_order, name COLLATE NOCASE, id
                """
            ).fetchall()
        ]

    def get_profile(
        self,
        db: sqlite3.Connection,
        profile_id: str,
    ) -> PersistedSearchProfile:
        row = db.execute(
            """
            SELECT id, name, kind, version, sort_order
            FROM search_profiles
            WHERE id = ?
            """,
            (profile_id,),
        ).fetchone()
        if row is None:
            raise KeyError(profile_id)
        return PersistedSearchProfile(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            version=row["version"],
            sort_order=row["sort_order"],
        )

    def load_settings(
        self,
        db: sqlite3.Connection,
        profile_id: str,
    ) -> dict[tuple[str, str], FolderSetting]:
        rows = db.execute(
            """
            SELECT source_id, directory, quality_override, selection_state
            FROM search_profile_folder_settings
            WHERE profile_id = ?
            ORDER BY source_id, directory
            """,
            (profile_id,),
        ).fetchall()
        return _normalize_settings(
            {
                (row["source_id"], row["directory"]): FolderSetting(
                    source_id=row["source_id"],
                    directory=row["directory"],
                    quality_override=row["quality_override"],
                    selection_state=row["selection_state"],
                )
                for row in rows
            }
        )

    def replace_settings(
        self,
        db: sqlite3.Connection,
        profile_id: str,
        settings: dict[tuple[str, str], FolderSetting],
    ) -> None:
        db.execute(
            "DELETE FROM search_profile_folder_settings WHERE profile_id = ?",
            (profile_id,),
        )
        for setting in _normalize_settings(settings).values():
            db.execute(
                """
                INSERT INTO search_profile_folder_settings(
                  profile_id,
                  source_id,
                  directory,
                  quality_override,
                  selection_state,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    profile_id,
                    setting.source_id,
                    setting.directory,
                    setting.quality_override,
                    setting.selection_state,
                ),
            )

    def create_custom_profile(
        self,
        db: sqlite3.Connection,
        *,
        name: str,
        settings: dict[tuple[str, str], FolderSetting],
    ) -> PersistedSearchProfile:
        normalized_name = name.strip()
        if not normalized_name:
            raise SearchProfileValidationError("profile name is required")
        profile_id = f"profile-{uuid.uuid4().hex}"
        sort_order = (
            db.execute(
                "SELECT COALESCE(MAX(sort_order), 1) + 1 FROM search_profiles"
            ).fetchone()[0]
            or 2
        )
        db.execute(
            """
            INSERT INTO search_profiles(id, name, kind, version, sort_order)
            VALUES (?, ?, 'custom', 1, ?)
            """,
            (profile_id, normalized_name, sort_order),
        )
        self.replace_settings(db, profile_id, settings)
        return self.get_profile(db, profile_id)

    def save_existing_profile(
        self,
        db: sqlite3.Connection,
        *,
        profile_id: str,
        expected_version: int | None,
        settings: dict[tuple[str, str], FolderSetting],
        overwrite: bool = False,
    ) -> PersistedSearchProfile:
        profile = self.get_profile(db, profile_id)
        if profile.kind == "none":
            raise SearchProfileValidationError("None profile cannot be saved")
        if not overwrite and expected_version is not None and profile.version != expected_version:
            raise SearchProfileConflict("profile has changed")
        self.replace_settings(db, profile_id, settings)
        db.execute(
            """
            UPDATE search_profiles
            SET version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (profile_id,),
        )
        return self.get_profile(db, profile_id)

    def delete_custom_profile(
        self,
        db: sqlite3.Connection,
        profile_id: str,
    ) -> None:
        profile = self.get_profile(db, profile_id)
        if profile.kind != "custom":
            raise SearchProfileValidationError("only custom profiles can be deleted")
        db.execute("DELETE FROM search_profiles WHERE id = ?", (profile_id,))


class SearchProfileSessionManager:
    def __init__(self, search_index: SearchIndex) -> None:
        self._search_index = search_index
        self._store = SearchProfileStore()
        self._lock = threading.RLock()
        self._sessions: dict[str, SearchProfileSession] = {}

    def session(self, db: sqlite3.Connection, search_session: str | None) -> SearchProfileSession:
        session_id = search_session or "default-browser-session"
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            session = self._new_session_from_profile(db, session_id, PROFILE_DEFAULT_ID)
            self._sessions[session_id] = session
            return session

    def profiles_response(
        self,
        db: sqlite3.Connection,
        search_session: str | None,
    ) -> dict[str, object]:
        session = self.session(db, search_session)
        return {
            "profiles": [
                _profile_response(profile) for profile in self._store.list_profiles(db)
            ],
            "session": self.session_response(session),
            "settings": [_folder_setting_response(setting) for setting in session.working_settings.values()],
        }

    def preferences(
        self,
        db: sqlite3.Connection,
        search_session: str | None,
    ) -> SearchPreferences:
        return self.session(db, search_session).preferences

    def select_profile(
        self,
        db: sqlite3.Connection,
        *,
        search_session: str | None,
        profile_id: str,
        discard: bool = False,
    ) -> SearchProfileSession:
        with self._lock:
            session = self.session(db, search_session)
            if session.dirty and not discard:
                raise SearchProfileConflict("search profile has unsaved changes")
            next_session = self._new_session_from_profile(db, session.search_session, profile_id)
            self._sessions[session.search_session] = next_session
            return next_session

    def update_setting(
        self,
        db: sqlite3.Connection,
        *,
        search_session: str | None,
        source_id: str,
        directory: str,
        quality_override: int | None | object = _UNSET,
        selection_state: DirectoryFilterState | None | object = _UNSET,
    ) -> SearchProfileSession:
        self._validate_directory(source_id, directory)
        with self._lock:
            session = self.session(db, search_session)
            if session.active_profile_id == PROFILE_NONE_ID:
                session.active_profile_id = None
                session.active_profile_name = None
                session.active_profile_kind = None
                session.base_profile_version = None
                session.persisted_settings = {}
            current = session.working_settings.get(
                (source_id, directory),
                FolderSetting(source_id=source_id, directory=directory),
            )
            next_quality = current.quality_override
            next_selection = current.selection_state
            if quality_override is not _UNSET:
                if quality_override is not None and not isinstance(quality_override, int):
                    raise SearchProfileValidationError("quality_override must be an integer or null")
                if isinstance(quality_override, int) and not 0 <= quality_override <= 10:
                    raise SearchProfileValidationError("quality_override must be between 0 and 10")
                next_quality = (
                    None
                    if quality_override in {None, DEFAULT_FOLDER_QUALITY}
                    else quality_override
                )
            if selection_state is not _UNSET:
                if selection_state not in {None, "whitelist", "blacklist"}:
                    raise SearchProfileValidationError(
                        "selection_state must be whitelist, blacklist, or null"
                    )
                next_selection = selection_state
            next_settings = dict(session.working_settings)
            next_setting = FolderSetting(
                source_id=source_id,
                directory=directory,
                quality_override=next_quality,
                selection_state=next_selection,
            )
            if next_setting.quality_override is None and next_setting.selection_state is None:
                next_settings.pop((source_id, directory), None)
            else:
                next_settings[(source_id, directory)] = next_setting
            _validate_selection_rules(next_settings)
            session.working_settings = _normalize_settings(next_settings)
            session.preferences = self._compile_preferences(session.working_settings)
            return session

    def reset_scoring(
        self,
        db: sqlite3.Connection,
        *,
        search_session: str | None,
    ) -> SearchProfileSession:
        with self._lock:
            session = self.session(db, search_session)
            next_settings = dict(session.working_settings)
            for key, setting in list(next_settings.items()):
                persisted = session.persisted_settings.get(key)
                next_quality = persisted.quality_override if persisted else None
                if next_quality is None and setting.selection_state is None:
                    next_settings.pop(key)
                else:
                    next_settings[key] = FolderSetting(
                        source_id=setting.source_id,
                        directory=setting.directory,
                        quality_override=next_quality,
                        selection_state=setting.selection_state,
                    )
            for key, persisted in session.persisted_settings.items():
                if persisted.quality_override is None:
                    continue
                setting = next_settings.get(
                    key,
                    FolderSetting(source_id=persisted.source_id, directory=persisted.directory),
                )
                next_settings[key] = FolderSetting(
                    source_id=persisted.source_id,
                    directory=persisted.directory,
                    quality_override=persisted.quality_override,
                    selection_state=setting.selection_state,
                )
            session.working_settings = _normalize_settings(next_settings)
            session.preferences = self._compile_preferences(session.working_settings)
            return session

    def reset_selection(
        self,
        db: sqlite3.Connection,
        *,
        search_session: str | None,
    ) -> SearchProfileSession:
        with self._lock:
            session = self.session(db, search_session)
            next_settings = dict(session.working_settings)
            for key, setting in list(next_settings.items()):
                persisted = session.persisted_settings.get(key)
                next_selection = persisted.selection_state if persisted else None
                if setting.quality_override is None and next_selection is None:
                    next_settings.pop(key)
                else:
                    next_settings[key] = FolderSetting(
                        source_id=setting.source_id,
                        directory=setting.directory,
                        quality_override=setting.quality_override,
                        selection_state=next_selection,
                    )
            for key, persisted in session.persisted_settings.items():
                if persisted.selection_state is None:
                    continue
                setting = next_settings.get(
                    key,
                    FolderSetting(source_id=persisted.source_id, directory=persisted.directory),
                )
                next_settings[key] = FolderSetting(
                    source_id=persisted.source_id,
                    directory=persisted.directory,
                    quality_override=setting.quality_override,
                    selection_state=persisted.selection_state,
                )
            _validate_selection_rules(next_settings)
            session.working_settings = _normalize_settings(next_settings)
            session.preferences = self._compile_preferences(session.working_settings)
            return session

    def save(
        self,
        db: sqlite3.Connection,
        *,
        search_session: str | None,
        name: str | None = None,
        overwrite: bool = False,
    ) -> SearchProfileSession:
        with self._lock:
            session = self.session(db, search_session)
            if session.active_profile_id is None:
                profile = self._store.create_custom_profile(
                    db,
                    name=name or "",
                    settings=session.working_settings,
                )
            else:
                profile = self._store.save_existing_profile(
                    db,
                    profile_id=session.active_profile_id,
                    expected_version=session.base_profile_version,
                    settings=session.working_settings,
                    overwrite=overwrite,
                )
            persisted = self._store.load_settings(db, profile.id)
            session.active_profile_id = profile.id
            session.active_profile_name = profile.name
            session.active_profile_kind = profile.kind
            session.base_profile_version = profile.version
            session.persisted_settings = persisted
            session.working_settings = dict(persisted)
            session.preferences = self._compile_preferences(session.working_settings)
            return session

    def create_profile(
        self,
        db: sqlite3.Connection,
        *,
        search_session: str | None,
        name: str,
    ) -> SearchProfileSession:
        with self._lock:
            session = self.session(db, search_session)
            profile = self._store.create_custom_profile(
                db,
                name=name,
                settings=session.working_settings,
            )
            persisted = self._store.load_settings(db, profile.id)
            session.active_profile_id = profile.id
            session.active_profile_name = profile.name
            session.active_profile_kind = profile.kind
            session.base_profile_version = profile.version
            session.persisted_settings = persisted
            session.working_settings = dict(persisted)
            session.preferences = self._compile_preferences(session.working_settings)
            return session

    def delete_profile(
        self,
        db: sqlite3.Connection,
        *,
        search_session: str | None,
        profile_id: str,
        discard: bool = False,
    ) -> SearchProfileSession:
        with self._lock:
            session = self.session(db, search_session)
            if (
                session.active_profile_id == profile_id
                and session.dirty
                and not discard
            ):
                raise SearchProfileConflict("search profile has unsaved changes")
            self._store.delete_custom_profile(db, profile_id)
            if session.active_profile_id == profile_id:
                session = self._new_session_from_profile(db, session.search_session, PROFILE_DEFAULT_ID)
                self._sessions[session.search_session] = session
            return session

    def session_response(self, session: SearchProfileSession) -> dict[str, object]:
        return {
            "search_session": session.search_session,
            "active_profile_id": session.active_profile_id,
            "active_profile_name": session.active_profile_name,
            "active_profile_kind": session.active_profile_kind,
            "base_profile_version": session.base_profile_version,
            "dirty_scoring": session.dirty_scoring,
            "dirty_selection": session.dirty_selection,
            "dirty": session.dirty,
            "settings_fingerprint": session.preferences.settings_fingerprint,
        }

    def dirty_folder_subtree_keys(
        self, session: SearchProfileSession
    ) -> frozenset[tuple[str, str]]:
        return _folder_subtree_keys(session.dirty_folder_keys)

    def _new_session_from_profile(
        self,
        db: sqlite3.Connection,
        search_session: str,
        profile_id: str,
    ) -> SearchProfileSession:
        profile = self._store.get_profile(db, profile_id)
        settings = self._store.load_settings(db, profile_id)
        return SearchProfileSession(
            search_session=search_session,
            active_profile_id=profile.id,
            active_profile_name=profile.name,
            active_profile_kind=profile.kind,
            base_profile_version=profile.version,
            working_settings=dict(settings),
            persisted_settings=dict(settings),
            preferences=self._compile_preferences(settings),
        )

    def _compile_preferences(
        self,
        settings: dict[tuple[str, str], FolderSetting],
    ) -> SearchPreferences:
        normalized = _normalize_settings(settings)
        return self._search_index.compile_preferences(
            directory_filter=_settings_to_filter(normalized),
            quality_overrides=_quality_overrides(normalized),
        )

    def _validate_directory(self, source_id: str, directory: str) -> None:
        if (source_id, directory) not in self._search_index.directory_counts:
            raise SearchProfileValidationError("indexed folder was not found")
        if not _is_normalized_directory_path(directory):
            raise SearchProfileValidationError(
                "directory must be a normalized source-relative path"
            )


def _profile_response(profile: PersistedSearchProfile) -> dict[str, object]:
    return {
        "id": profile.id,
        "name": profile.name,
        "kind": profile.kind,
        "version": profile.version,
        "sort_order": profile.sort_order,
    }


def _folder_setting_response(setting: FolderSetting) -> dict[str, object]:
    return {
        "source_id": setting.source_id,
        "directory": setting.directory,
        "quality_override": setting.quality_override,
        "selection_state": setting.selection_state,
    }


def _normalize_settings(
    settings: dict[tuple[str, str], FolderSetting],
) -> dict[tuple[str, str], FolderSetting]:
    normalized = {}
    for key, setting in settings.items():
        quality = setting.quality_override
        if quality == DEFAULT_FOLDER_QUALITY:
            quality = None
        selection = setting.selection_state
        if quality is None and selection is None:
            continue
        normalized[key] = FolderSetting(
            source_id=setting.source_id,
            directory=setting.directory,
            quality_override=quality,
            selection_state=selection,
        )
    return dict(sorted(normalized.items()))


def _quality_overrides(
    settings: dict[tuple[str, str], FolderSetting],
) -> dict[tuple[str, str], int]:
    return {
        key: setting.quality_override
        for key, setting in settings.items()
        if setting.quality_override is not None
    }


def _settings_to_filter(
    settings: dict[tuple[str, str], FolderSetting],
) -> DirectoryFilter:
    return DirectoryFilter(
        tuple(
            DirectoryFilterRule(
                source_id=setting.source_id,
                path=setting.directory,
                state=setting.selection_state,
            )
            for setting in settings.values()
            if setting.selection_state is not None
        )
    )


def _scoring_settings(
    settings: dict[tuple[str, str], FolderSetting],
) -> tuple[tuple[str, str, int], ...]:
    return tuple(
        sorted(
            (setting.source_id, setting.directory, setting.quality_override)
            for setting in _normalize_settings(settings).values()
            if setting.quality_override is not None
        )
    )


def _selection_settings(
    settings: dict[tuple[str, str], FolderSetting],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (setting.source_id, setting.directory, setting.selection_state)
            for setting in _normalize_settings(settings).values()
            if setting.selection_state is not None
        )
    )


def _dirty_folder_keys(
    working_settings: dict[tuple[str, str], FolderSetting],
    persisted_settings: dict[tuple[str, str], FolderSetting],
) -> frozenset[tuple[str, str]]:
    keys = set(working_settings) | set(persisted_settings)
    return frozenset(
        key
        for key in keys
        if working_settings.get(key) != persisted_settings.get(key)
    )


def _folder_subtree_keys(
    folder_keys: frozenset[tuple[str, str]],
) -> frozenset[tuple[str, str]]:
    subtree_keys: set[tuple[str, str]] = set()
    for source_id, directory in folder_keys:
        subtree_keys.add((source_id, ""))
        if not directory:
            continue
        parts = directory.split("/")
        for index in range(1, len(parts) + 1):
            subtree_keys.add((source_id, "/".join(parts[:index])))
    return frozenset(subtree_keys)


def _validate_selection_rules(
    settings: dict[tuple[str, str], FolderSetting],
) -> None:
    _normalize_settings(settings)


def _is_normalized_directory_path(path: str) -> bool:
    if path == "":
        return True
    pure = PurePosixPath(path)
    return (
        str(pure) == path
        and not path.startswith("/")
        and not path.endswith("/")
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )
