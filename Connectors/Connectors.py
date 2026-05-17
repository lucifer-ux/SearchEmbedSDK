from __future__ import annotations

import os
import time
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import requests

from . import store

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


@dataclass(frozen=True)
class StaticTokenAuth:
    token: str


@dataclass(frozen=True)
class OAuthTokenAuth:
    access_token: str
    refresh_token: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True)
class AccountIdentity:
    provider: str
    account_id: str
    display_name: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RemoteRef:
    external_id: str
    object_type: str
    title: str
    url: str | None = None
    parent_id: str | None = None
    container_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorDocument:
    provider: str
    account_id: str
    external_id: str
    uri: str
    title: str
    body_text: str
    url: str | None
    updated_at: str | None
    object_type: str
    parent_id: str | None = None
    container_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_json: str = "{}"
    content_hash: str = ""

    def __post_init__(self) -> None:
        body = (self.body_text or self.title or self.external_id).strip()
        if not self.body_text:
            object.__setattr__(self, "body_text", body)
        if not self.content_hash:
            object.__setattr__(self, "content_hash", store.compute_content_hash(body))

    @property
    def content(self) -> str:
        return self.body_text


@dataclass(frozen=True)
class ConnectorSyncResult:
    provider: str
    account_id: str
    discovered: int
    fetched: int
    updated: int
    unchanged: int
    failed: int
    warnings: list[str] = field(default_factory=list)


class ConnectorProvider:
    provider_name = ""

    def validate_auth(self, auth: StaticTokenAuth | OAuthTokenAuth) -> AccountIdentity:
        raise NotImplementedError

    def discover_roots(
        self,
        account: AccountIdentity,
    ) -> list[RemoteRef]:
        raise NotImplementedError

    def crawl_object(
        self,
        account: AccountIdentity,
        ref: RemoteRef,
    ) -> tuple[list[ConnectorDocument], list[RemoteRef]]:
        raise NotImplementedError


class NotionProvider(ConnectorProvider):
    provider_name = "notion"

    def __init__(self, auth: StaticTokenAuth | OAuthTokenAuth):
        self.auth = auth
        self.session = requests.Session()
        token = auth.token if isinstance(auth, StaticTokenAuth) else auth.access_token
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{NOTION_API_BASE}{path}"
        for attempt in range(5):
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                timeout=30,
            )
            if response.status_code == 429 and attempt < 4:
                retry_after = response.headers.get("Retry-After", "1")
                try:
                    wait_s = max(1.0, float(retry_after))
                except ValueError:
                    wait_s = 1.0
                time.sleep(wait_s)
                continue
            if response.status_code in {500, 502, 503, 504} and attempt < 4:
                time.sleep(1.0 + attempt)
                continue
            if response.status_code >= 400:
                detail: Any
                try:
                    detail = response.json()
                except ValueError:
                    detail = response.text
                raise requests.HTTPError(
                    f"Notion API {method} {path} failed with {response.status_code}: {detail}",
                    response=response,
                )
            return response.json()
        raise RuntimeError(f"Notion API {method} {path} exhausted retries")

    def _paginate(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        results_key: str = "results",
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        next_cursor: str | None = None
        while True:
            if method.upper() == "GET":
                call_params = dict(params or {})
                if next_cursor:
                    call_params["start_cursor"] = next_cursor
                call_params.setdefault("page_size", 100)
                payload = self._request(method, path, params=call_params)
            else:
                call_json = dict(json_body or {})
                if next_cursor:
                    call_json["start_cursor"] = next_cursor
                call_json.setdefault("page_size", 100)
                payload = self._request(method, path, json_body=call_json)
            batch = payload.get(results_key, [])
            if isinstance(batch, list):
                out.extend(batch)
            if not payload.get("has_more"):
                break
            next_cursor = payload.get("next_cursor")
            if not next_cursor:
                break
        return out

    def validate_auth(self, auth: StaticTokenAuth | OAuthTokenAuth) -> AccountIdentity:
        payload = self._request("GET", "/users/me")
        bot = payload.get("bot") or {}
        owner = bot.get("owner") or {}
        workspace_name = bot.get("workspace_name") or owner.get("workspace_name")
        account_id = str(payload.get("id") or owner.get("user", {}).get("id") or "notion")
        display_name = (
            payload.get("name")
            or workspace_name
            or payload.get("type")
            or "Notion"
        )
        return AccountIdentity(
            provider=self.provider_name,
            account_id=account_id,
            display_name=str(display_name),
            metadata=payload,
        )

    def discover_roots(
        self,
        account: AccountIdentity,
    ) -> list[RemoteRef]:
        rows = self._paginate("POST", "/search", json_body={})
        seen: set[tuple[str, str]] = set()
        out: list[RemoteRef] = []
        for row in rows:
            object_type = str(row.get("object") or "")
            if object_type not in {"page", "database"}:
                continue
            external_id = str(row.get("id") or "")
            if not external_id or (object_type, external_id) in seen:
                continue
            seen.add((object_type, external_id))
            out.append(
                RemoteRef(
                    external_id=external_id,
                    object_type=object_type,
                    title=self._extract_title(row, fallback=external_id),
                    url=row.get("url"),
                    parent_id=self._extract_parent_id(row),
                    metadata=row,
                )
            )
        return out

    def crawl_object(
        self,
        account: AccountIdentity,
        ref: RemoteRef,
    ) -> tuple[list[ConnectorDocument], list[RemoteRef]]:
        if ref.object_type == "database":
            return self._crawl_database(account, ref)
        return self._crawl_page(account, ref.external_id, container_id=ref.container_id)

    def _crawl_database(
        self,
        account: AccountIdentity,
        ref: RemoteRef,
    ) -> tuple[list[ConnectorDocument], list[RemoteRef]]:
        payload = self._request("GET", f"/databases/{ref.external_id}")
        title = self._extract_title(payload, fallback=ref.title or ref.external_id)
        property_names = ", ".join(sorted((payload.get("properties") or {}).keys()))
        body = f"{title}\n\nDatabase schema:\n{property_names}".strip()
        metadata = {
            "provider_object": "database",
            "properties": list((payload.get("properties") or {}).keys()),
        }
        doc = self._make_document(
            account=account,
            external_id=ref.external_id,
            object_type="database",
            title=title,
            body=body,
            url=payload.get("url"),
            updated_at=payload.get("last_edited_time"),
            parent_id=self._extract_parent_id(payload),
            container_id=None,
            metadata=metadata,
            raw_json=payload,
        )

        page_rows = self._paginate("POST", f"/databases/{ref.external_id}/query", json_body={})
        discovered_refs = [
            RemoteRef(
                external_id=str(row.get("id")),
                object_type="page",
                title=self._extract_title(row, fallback=str(row.get("id") or "page")),
                url=row.get("url"),
                parent_id=self._extract_parent_id(row),
                container_id=ref.external_id,
                metadata=row,
            )
            for row in page_rows
            if row.get("id")
        ]
        docs: list[ConnectorDocument] = [doc]
        nested_refs: list[RemoteRef] = []
        for page_ref in discovered_refs:
            page_docs, page_nested = self._crawl_page(
                account,
                page_ref.external_id,
                container_id=ref.external_id,
            )
            docs.extend(page_docs)
            nested_refs.extend(page_nested)
        return docs, nested_refs

    def _crawl_page(
        self,
        account: AccountIdentity,
        page_id: str,
        *,
        container_id: str | None = None,
    ) -> tuple[list[ConnectorDocument], list[RemoteRef]]:
        payload = self._request("GET", f"/pages/{page_id}")
        title = self._extract_title(payload, fallback=page_id)
        properties_text = self._extract_properties_text(payload)
        blocks = self._paginate("GET", f"/blocks/{page_id}/children")
        block_text, discovered_refs = self._flatten_blocks(blocks)
        sections = [title]
        if properties_text:
            sections.extend(["", "Properties", properties_text])
        if block_text:
            sections.extend(["", "Content", block_text])
        body = "\n".join(part for part in sections if part is not None).strip()
        doc = self._make_document(
            account=account,
            external_id=page_id,
            object_type="page",
            title=title,
            body=body,
            url=payload.get("url"),
            updated_at=payload.get("last_edited_time"),
            parent_id=self._extract_parent_id(payload),
            container_id=container_id,
            metadata={"provider_object": "page", "has_children": payload.get("has_children")},
            raw_json=payload,
        )
        return [doc], discovered_refs

    def _flatten_blocks(self, blocks: list[dict[str, Any]]) -> tuple[str, list[RemoteRef]]:
        lines: list[str] = []
        discovered_refs: list[RemoteRef] = []
        queue: deque[dict[str, Any]] = deque(blocks)
        while queue:
            block = queue.popleft()
            block_type = str(block.get("type") or "")
            payload = block.get(block_type) or {}
            text = self._block_text(block_type, payload)
            if text:
                lines.append(text)
            if block_type == "child_page":
                child_id = str(block.get("id") or "")
                if child_id:
                    discovered_refs.append(
                        RemoteRef(
                            external_id=child_id,
                            object_type="page",
                            title=str(payload.get("title") or child_id),
                            parent_id=None,
                            metadata=block,
                        )
                    )
            elif block_type == "child_database":
                child_id = str(block.get("id") or "")
                if child_id:
                    discovered_refs.append(
                        RemoteRef(
                            external_id=child_id,
                            object_type="database",
                            title=str(payload.get("title") or child_id),
                            parent_id=None,
                            metadata=block,
                        )
                    )
            if block.get("has_children"):
                child_id = str(block.get("id") or "")
                if child_id:
                    children = self._paginate("GET", f"/blocks/{child_id}/children")
                    queue.extend(children)
        return "\n".join(line for line in lines if line.strip()), discovered_refs

    def _block_text(self, block_type: str, payload: dict[str, Any]) -> str:
        rich_text = self._plain_text(payload.get("rich_text"))
        if block_type in {
            "paragraph",
            "heading_1",
            "heading_2",
            "heading_3",
            "bulleted_list_item",
            "numbered_list_item",
            "toggle",
            "quote",
            "callout",
        }:
            return rich_text
        if block_type == "to_do":
            prefix = "[x]" if payload.get("checked") else "[ ]"
            return f"{prefix} {rich_text}".strip()
        if block_type == "code":
            language = payload.get("language") or "plain"
            return f"[code:{language}] {rich_text}".strip()
        if block_type == "bookmark":
            return str(payload.get("url") or "")
        if block_type == "equation":
            return str(payload.get("expression") or "")
        if block_type in {"image", "file", "pdf", "video"}:
            caption = self._plain_text(payload.get("caption"))
            return caption
        if block_type in {"child_page", "child_database"}:
            return str(payload.get("title") or "")
        return rich_text

    def _extract_title(self, obj: dict[str, Any], fallback: str) -> str:
        if obj.get("object") == "database":
            title = self._plain_text(obj.get("title"))
            return title or fallback
        props = obj.get("properties") or {}
        for value in props.values():
            value_type = str(value.get("type") or "")
            if value_type == "title":
                title = self._plain_text(value.get("title"))
                if title:
                    return title
        return str(obj.get("child_page", {}).get("title") or fallback)

    def _extract_parent_id(self, obj: dict[str, Any]) -> str | None:
        parent = obj.get("parent") or {}
        for key in ("page_id", "database_id", "workspace"):
            if parent.get(key):
                return str(parent.get(key))
        return None

    def _extract_properties_text(self, page: dict[str, Any]) -> str:
        props = page.get("properties") or {}
        lines: list[str] = []
        for name, value in props.items():
            text = self._property_text(value)
            if text:
                lines.append(f"{name}: {text}")
        return "\n".join(lines)

    def _property_text(self, value: dict[str, Any]) -> str:
        value_type = str(value.get("type") or "")
        payload = value.get(value_type)
        if value_type in {"title", "rich_text"}:
            return self._plain_text(payload)
        if value_type == "select":
            return str((payload or {}).get("name") or "")
        if value_type == "multi_select":
            return ", ".join(
                str(item.get("name") or "") for item in (payload or []) if item.get("name")
            )
        if value_type == "status":
            return str((payload or {}).get("name") or "")
        if value_type == "date":
            if not isinstance(payload, dict):
                return ""
            start = payload.get("start") or ""
            end = payload.get("end") or ""
            return f"{start} {end}".strip()
        if value_type in {"email", "url", "phone_number", "number"}:
            return str(payload or "")
        if value_type == "checkbox":
            return "true" if bool(payload) else "false"
        if value_type == "people":
            return ", ".join(
                str(person.get("name") or person.get("id") or "")
                for person in (payload or [])
            )
        if value_type == "relation":
            return ", ".join(str(item.get("id") or "") for item in (payload or []))
        if value_type == "formula":
            if isinstance(payload, dict):
                for key in ("string", "number", "boolean", "date"):
                    if payload.get(key) is not None:
                        return str(payload.get(key))
            return ""
        return ""

    def _plain_text(self, value: Any) -> str:
        if not value:
            return ""
        if isinstance(value, list):
            return "".join(
                str(item.get("plain_text") or item.get("text", {}).get("content") or "")
                for item in value
                if isinstance(item, dict)
            ).strip()
        if isinstance(value, dict):
            return str(value.get("plain_text") or value.get("content") or "").strip()
        return str(value).strip()

    def _make_document(
        self,
        *,
        account: AccountIdentity,
        external_id: str,
        object_type: str,
        title: str,
        body: str,
        url: str | None,
        updated_at: str | None,
        parent_id: str | None,
        container_id: str | None,
        metadata: dict[str, Any],
        raw_json: dict[str, Any],
    ) -> ConnectorDocument:
        uri = f"notion://{account.account_id}/{object_type}/{external_id}"
        content_hash = store.compute_content_hash(body)
        return ConnectorDocument(
            provider="notion",
            account_id=account.account_id,
            external_id=external_id,
            uri=uri,
            title=title or external_id,
            body_text=body or (title or external_id),
            url=url,
            updated_at=updated_at,
            object_type=object_type,
            parent_id=parent_id,
            container_id=container_id,
            metadata=metadata,
            raw_json=json.dumps(raw_json, separators=(",", ":")),
            content_hash=content_hash,
        )


def _normalize_provider_name(provider: str) -> str:
    return (provider or "").strip().lower()


def _provider_for_name(
    provider: str,
    auth: StaticTokenAuth | OAuthTokenAuth,
) -> ConnectorProvider:
    normalized = _normalize_provider_name(provider)
    if normalized == "notion":
        return NotionProvider(auth)
    raise ValueError(f"Unsupported connector provider: {provider}")


def sync_connector(provider: str, api_key: str) -> ConnectorSyncResult:
    if not api_key or not api_key.strip():
        raise ValueError("api_key is required")

    auth = StaticTokenAuth(token=api_key.strip())
    backend = _provider_for_name(provider, auth)
    account = backend.validate_auth(auth)
    store.upsert_account(
        provider=account.provider,
        account_id=account.account_id,
        display_name=account.display_name,
        auth_mode="static_token",
        metadata=account.metadata,
    )

    discovered_roots = backend.discover_roots(account)
    known_rows = store.list_known_objects(
        account.provider,
        account.account_id,
        object_types=("page", "database"),
    )
    root_map: dict[tuple[str, str], RemoteRef] = {}
    for row in known_rows:
        key = (str(row["object_type"]), str(row["external_id"]))
        root_map[key] = RemoteRef(
            external_id=str(row["external_id"]),
            object_type=str(row["object_type"]),
            title=str(row.get("title") or row["external_id"]),
            url=row.get("url"),
            parent_id=row.get("parent_id"),
            container_id=row.get("container_id"),
        )
    for ref in discovered_roots:
        root_map[(ref.object_type, ref.external_id)] = ref

    queue: deque[RemoteRef] = deque(root_map.values())
    visited: set[tuple[str, str]] = set()
    fetched = 0
    updated = 0
    unchanged = 0
    failed = 0
    warnings: list[str] = []

    while queue:
        ref = queue.popleft()
        key = (ref.object_type, ref.external_id)
        if key in visited:
            continue
        visited.add(key)
        try:
            documents, child_refs = backend.crawl_object(account, ref)
            fetched += len(documents)
            for document in documents:
                result = store.upsert_document_state(document)
                if result == "inserted":
                    updated += 1
                elif result == "updated":
                    updated += 1
                else:
                    unchanged += 1
            for child_ref in child_refs:
                child_key = (child_ref.object_type, child_ref.external_id)
                if child_key not in visited:
                    queue.append(child_ref)
        except requests.HTTPError as exc:
            failed += 1
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code in {403, 404}:
                store.mark_deleted(account.provider, account.account_id, ref.external_id, error=str(exc))
            else:
                warnings.append(str(exc))
        except Exception as exc:
            failed += 1
            warnings.append(str(exc))

    return ConnectorSyncResult(
        provider=account.provider,
        account_id=account.account_id,
        discovered=len(discovered_roots),
        fetched=fetched,
        updated=updated,
        unchanged=unchanged,
        failed=failed,
        warnings=warnings,
    )


def sync_connector_from_env(provider: str, env_var: str | None = None) -> ConnectorSyncResult:
    normalized = _normalize_provider_name(provider)
    if env_var:
        key_name = env_var
    elif normalized == "notion":
        key_name = "CONTEXTCORE_CONNECTOR_NOTION_TOKEN"
    else:
        key_name = f"CONTEXTCORE_CONNECTOR_{normalized.upper()}_TOKEN"
    token = os.getenv(key_name, "").strip()
    if not token:
        raise ValueError(f"Missing connector token in env var {key_name}")
    return sync_connector(provider=normalized, api_key=token)
