#!/usr/bin/env python3
"""
Import one Cursor chat from a source state.vscdb + transcript jsonl
into the local Cursor installation.

What this script does:
1. Copies the transcript jsonl into:
   ~/.cursor/projects/<target-project-id>/agent-transcripts/<composerId>/<composerId>.jsonl
2. Imports the related rows from a source state.vscdb into:
   ~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
3. Optionally merges a chat head into:
   ~/Library/Application Support/Cursor/User/workspaceStorage/<workspace-id>/state.vscdb

Global import rows:
- composerData:<composerId>
- bubbleId:<composerId>:*
- recursively referenced agentKv:blob:* rows

Workspace import rows:
- composer.composerData
- workbench.panel.composerChatViewPane.<pane-id>
- workbench.panel.aichat.<pane-id>.numberOfVisibleViews

Important:
- Close Cursor before running this script.
- Keep backups of your destination DB.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


BLOB_KEY_RE = re.compile(r"agentKv:blob:[0-9a-f]+")


class MissingComposerRowError(Exception):
    """Raised when a transcript exists but the matching composerData row does not."""


@dataclass
class ChatRows:
    composer_key: str
    composer_value: str
    bubble_rows: Dict[str, str]
    blob_rows: Dict[str, str]


@dataclass
class WorkspaceUpdatePlan:
    workspace_db: Path
    composer_data_json: str
    pane_entries: List[Tuple[str, str, str, str]]
    head_count_before: int
    head_count_after: int
    selected_before: List[str]
    selected_after: List[str]


@dataclass
class BundleData:
    composer_id: str
    title: str
    rows: ChatRows
    transcript_text: str
    workspace_head: Dict[str, Any] | None = None


@dataclass
class BundleCollection:
    chats: List[BundleData]


def parse_args() -> argparse.Namespace:
    home = Path.home()
    default_cursor_root = home / "Library" / "Application Support" / "Cursor"
    default_projects_root = home / ".cursor" / "projects"
    default_global_db = (
        default_cursor_root / "User" / "globalStorage" / "state.vscdb"
    )

    parser = argparse.ArgumentParser(
        description="Import one Cursor chat into the local machine."
    )
    parser.add_argument(
        "--source-db",
        help="Path to the source machine's state.vscdb",
    )
    parser.add_argument(
        "--source-jsonl",
        help="Path to the source chat transcript jsonl file",
    )
    parser.add_argument(
        "--source-transcripts-dir",
        help="Directory containing <composerId>/<composerId>.jsonl folders",
    )
    parser.add_argument(
        "--source-workspace-db",
        help="Optional source workspaceStorage state.vscdb path for preserving workspace chat heads",
    )
    parser.add_argument(
        "--source-workspace-id",
        help="Source workspaceStorage id; auto-resolves to <source-user-root>/workspaceStorage/<id>/state.vscdb",
    )
    parser.add_argument(
        "--all-transcripts",
        action="store_true",
        help="Use all transcript IDs found under --source-transcripts-dir",
    )
    parser.add_argument(
        "--composer-ids",
        nargs="+",
        help="One or more composer IDs to export from --source-transcripts-dir",
    )
    parser.add_argument(
        "--export-bundle",
        help="Export a portable chat bundle zip at this path",
    )
    parser.add_argument(
        "--import-bundle",
        help="Import from a previously exported chat bundle zip",
    )
    parser.add_argument(
        "--composer-id",
        help="Composer/chat id. If omitted, inferred from the jsonl filename.",
    )
    parser.add_argument(
        "--target-project-id",
        help="Target ~/.cursor/projects/<id> folder name",
    )
    parser.add_argument(
        "--target-workspace-storage-id",
        help="workspaceStorage id to merge the imported chat into",
    )
    parser.add_argument(
        "--target-workspace-id",
        help="Alias for --target-workspace-storage-id",
    )
    parser.add_argument(
        "--include-workspace-storage",
        action="store_true",
        help="Also merge the imported chat head into workspaceStorage",
    )
    parser.add_argument(
        "--workspace-storage-only",
        action="store_true",
        help="Only update workspaceStorage chat heads/panes; skip global DB and transcript writes",
    )
    parser.add_argument(
        "--cursor-root",
        default=str(default_cursor_root),
        help="Cursor application data root on destination machine",
    )
    parser.add_argument(
        "--projects-root",
        default=str(default_projects_root),
        help="Cursor projects root on destination machine",
    )
    parser.add_argument(
        "--dest-global-db",
        default=str(default_global_db),
        help="Destination globalStorage/state.vscdb path",
    )
    parser.add_argument(
        "--target-global-db",
        help="Alias for --dest-global-db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without writing anything",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip destination backups",
    )
    return parser.parse_args()


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        fail(f"{label} not found: {path}")


def derive_user_root_from_global_db(global_db_path: Path) -> Path:
    # Expected: <user-root>/globalStorage/state.vscdb
    return global_db_path.parent.parent


def infer_composer_id(jsonl_path: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    if jsonl_path.suffix == ".jsonl":
        return jsonl_path.stem
    fail("Cannot infer composer id; please pass --composer-id")
    raise AssertionError("unreachable")


def normalize_composer_ids(raw_ids: List[str] | None) -> List[str]:
    if not raw_ids:
        return []
    result = []
    for item in raw_ids:
        for part in item.replace(",", " ").split():
            if part:
                result.append(part.strip())
    return result


def open_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def open_rw(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path), timeout=10)


def fetch_one(cur: sqlite3.Cursor, sql: str, params: Tuple[str, ...]) -> Tuple | None:
    return cur.execute(sql, params).fetchone()


def extract_blob_refs(text: str) -> Set[str]:
    return set(BLOB_KEY_RE.findall(text or ""))


def load_chat_rows(source_db: Path, composer_id: str) -> ChatRows:
    composer_key = f"composerData:{composer_id}"

    with open_ro(source_db) as conn:
        cur = conn.cursor()

        row = fetch_one(
            cur,
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (composer_key,),
        )
        if not row or row[0] is None:
            raise MissingComposerRowError(
                f"Composer row not found in source DB: {composer_key}"
            )
        composer_value = row[0]

        bubble_rows = {
            key: value
            for key, value in cur.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
                (f"bubbleId:{composer_id}:%",),
            )
        }

        blob_rows: Dict[str, str] = {}
        pending = list(extract_blob_refs(composer_value))
        for value in bubble_rows.values():
            pending.extend(extract_blob_refs(value))

        seen: Set[str] = set()
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            blob = fetch_one(
                cur,
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (key,),
            )
            if not blob or blob[0] is None:
                continue
            blob_rows[key] = blob[0]
            pending.extend(extract_blob_refs(blob[0]) - seen)

    return ChatRows(
        composer_key=composer_key,
        composer_value=composer_value,
        bubble_rows=bubble_rows,
        blob_rows=blob_rows,
    )


def preview_title(composer_value: str) -> str:
    try:
        data = json.loads(composer_value)
    except Exception:
        return "<unknown>"
    return data.get("name") or data.get("title") or "<untitled>"


def extract_chat_times(composer_value: str) -> Tuple[int | None, int | None]:
    try:
        data = json.loads(composer_value)
    except Exception:
        return None, None
    created_at = data.get("createdAt")
    updated_at = data.get("lastUpdatedAt", created_at)
    return (
        created_at if isinstance(created_at, int) else None,
        updated_at if isinstance(updated_at, int) else None,
    )


def format_unix_ms(unix_ms: int | None) -> str:
    if unix_ms is None:
        return "-"
    try:
        return datetime.fromtimestamp(unix_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(unix_ms)


def summarize_chat(chat: BundleData) -> str:
    created_at, updated_at = extract_chat_times(chat.rows.composer_value)
    return (
        f"{chat.composer_id}: {chat.title} "
        f"[created={format_unix_ms(created_at)}, "
        f"updated={format_unix_ms(updated_at)}]"
    )


def parse_json_or_fail(text: str, label: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        fail(f"Failed to parse JSON for {label}: {exc}")
    if not isinstance(parsed, dict):
        fail(f"Expected JSON object for {label}")
    return parsed


def truncate_subtitle(text: str | None, limit: int = 80) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def build_workspace_head_from_composer(composer_value: str) -> Dict[str, Any]:
    source = parse_json_or_fail(composer_value, "composerData")
    head: Dict[str, Any] = {
        "type": "head",
        "composerId": source["composerId"],
        "createdAt": source.get("createdAt"),
        "lastUpdatedAt": source.get("lastUpdatedAt", source.get("createdAt")),
        "unifiedMode": source.get("unifiedMode", "agent"),
        "forceMode": source.get("forceMode", "edit"),
        "hasUnreadMessages": source.get("hasUnreadMessages", False),
        "totalLinesAdded": source.get("totalLinesAdded", 0),
        "totalLinesRemoved": source.get("totalLinesRemoved", 0),
        "filesChangedCount": source.get("filesChangedCount", 0),
        "hasBlockingPendingActions": False,
        "isArchived": False,
        "isDraft": source.get("isDraft", False),
        "isWorktree": False,
        "worktreeStartedReadOnly": source.get("worktreeStartedReadOnly", False),
        "isSpec": source.get("isSpec", False),
        "isProject": source.get("isProject", False),
        "isBestOfNSubcomposer": source.get("isBestOfNSubcomposer", False),
        "numSubComposers": len(source.get("subComposerIds", [])),
        "referencedPlans": source.get("referencedPlans", []),
        "branches": source.get("branches", []),
    }
    if source.get("name"):
        head["name"] = source["name"]
    if source.get("subtitle"):
        head["subtitle"] = truncate_subtitle(source.get("subtitle"))
    if "contextUsagePercent" in source:
        head["contextUsagePercent"] = source["contextUsagePercent"]
    if source.get("createdOnBranch"):
        head["createdOnBranch"] = source["createdOnBranch"]
    if source.get("activeBranch"):
        head["activeBranch"] = source["activeBranch"]
    return head


def normalize_workspace_head(head: Dict[str, Any], composer_value: str) -> Dict[str, Any]:
    # Start from a fully populated head derived from composerData, then let the
    # source workspace head override any matching fields it already knows about.
    normalized = build_workspace_head_from_composer(composer_value)
    normalized.update(head)
    source = parse_json_or_fail(composer_value, "composerData")
    normalized["composerId"] = source["composerId"]
    normalized["type"] = "head"
    normalized.setdefault("createdAt", source.get("createdAt"))
    normalized.setdefault(
        "lastUpdatedAt", source.get("lastUpdatedAt", source.get("createdAt"))
    )
    if not normalized.get("name") and source.get("name"):
        normalized["name"] = source["name"]
    if not normalized.get("subtitle") and source.get("subtitle"):
        normalized["subtitle"] = truncate_subtitle(source.get("subtitle"))
    return normalized


def load_source_workspace_heads(source_workspace_db: Path) -> Dict[str, Dict[str, Any]]:
    ensure_exists(source_workspace_db, "Source workspace DB")
    with open_ro(source_workspace_db) as conn:
        cur = conn.cursor()
        row = fetch_one(
            cur,
            "SELECT value FROM ItemTable WHERE key='composer.composerData'",
            (),
        )
    if not row or row[0] is None:
        return {}
    payload = parse_json_or_fail(row[0], "source workspace composer.composerData")
    all_composers = payload.get("allComposers", [])
    if not isinstance(all_composers, list):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for item in all_composers:
        if not isinstance(item, dict):
            continue
        composer_id = item.get("composerId")
        if isinstance(composer_id, str):
            result[composer_id] = item
    return result


def resolve_source_workspace_db(
    source_db: Path,
    source_workspace_db_arg: str | None,
    source_workspace_id: str | None,
) -> Path | None:
    if source_workspace_db_arg:
        path = Path(source_workspace_db_arg).expanduser().resolve()
        ensure_exists(path, "Source workspace DB")
        return path
    if source_workspace_id:
        user_root = derive_user_root_from_global_db(source_db)
        path = user_root / "workspaceStorage" / source_workspace_id / "state.vscdb"
        ensure_exists(path, "Derived source workspace DB")
        return path
    return None


def dedupe_keep_first(items: List[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def upsert_workspace_head(
    all_composers: List[Any], imported_head: Dict[str, Any]
) -> List[Any]:
    composer_id = imported_head["composerId"]
    merged = [
        item
        for item in all_composers
        if not isinstance(item, dict) or item.get("composerId") != composer_id
    ]
    merged.insert(0, imported_head)
    return merged


def choose_primary_imported_chat(chats: List[BundleData]) -> str | None:
    ranked: List[Tuple[int, str]] = []
    for chat in chats:
        _, updated_at = extract_chat_times(chat.rows.composer_value)
        ranked.append((updated_at or 0, chat.composer_id))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][1]


def build_workspace_update_plan(
    cursor_root: Path,
    workspace_id: str,
    chats: List[BundleData],
) -> WorkspaceUpdatePlan:
    workspace_db = (
        cursor_root
        / "User"
        / "workspaceStorage"
        / workspace_id
        / "state.vscdb"
    )
    ensure_exists(workspace_db, "Target workspaceStorage DB")

    with open_ro(workspace_db) as conn:
        cur = conn.cursor()
        row = fetch_one(
            cur,
            "SELECT value FROM ItemTable WHERE key='composer.composerData'",
            (),
        )
        existing = parse_json_or_fail(
            row[0] if row and row[0] else "{}",
            "workspace composer.composerData",
        )
        existing_pane_rows = cur.execute(
            "SELECT value FROM ItemTable WHERE key LIKE 'workbench.panel.composerChatViewPane.%'"
        ).fetchall()

    all_composers = existing.get("allComposers", [])
    if not isinstance(all_composers, list):
        all_composers = []

    selected_before = existing.get("selectedComposerIds", [])
    if not isinstance(selected_before, list):
        selected_before = []
    last_focused_before = existing.get("lastFocusedComposerIds", [])
    if not isinstance(last_focused_before, list):
        last_focused_before = []

    selected_after = list(selected_before)
    last_focused_after = list(last_focused_before)
    pane_entries: List[Tuple[str, str, str, str]] = []
    existing_pane_composer_ids: Set[str] = set()
    for (pane_value_raw,) in existing_pane_rows:
        try:
            pane_value = json.loads(pane_value_raw)
        except Exception:
            continue
        if not isinstance(pane_value, dict):
            continue
        for key in pane_value:
            prefix = "workbench.panel.aichat.view."
            if key.startswith(prefix):
                existing_pane_composer_ids.add(key[len(prefix) :])

    for chat in chats:
        if chat.workspace_head is not None:
            imported_head = normalize_workspace_head(chat.workspace_head, chat.rows.composer_value)
        else:
            imported_head = build_workspace_head_from_composer(chat.rows.composer_value)
        composer_id = imported_head["composerId"]
        all_composers = upsert_workspace_head(all_composers, imported_head)

        if composer_id not in existing_pane_composer_ids:
            pane_id = str(uuid.uuid4())
            pane_key = f"workbench.panel.composerChatViewPane.{pane_id}"
            pane_value = json.dumps(
                {
                    f"workbench.panel.aichat.view.{composer_id}": {
                        "collapsed": False,
                        "isHidden": False,
                        "size": 703,
                    }
                },
                separators=(",", ":"),
            )
            pane_visible_key = f"workbench.panel.aichat.{pane_id}.numberOfVisibleViews"
            pane_visible_value = "1"
            pane_entries.append(
                (pane_key, pane_value, pane_visible_key, pane_visible_value)
            )
            existing_pane_composer_ids.add(composer_id)

    primary_imported_chat = choose_primary_imported_chat(chats)
    if primary_imported_chat and not selected_after:
        selected_after = [primary_imported_chat]
    else:
        selected_after = dedupe_keep_first(selected_after)

    if primary_imported_chat and not last_focused_after:
        last_focused_after = [primary_imported_chat]
    else:
        last_focused_after = dedupe_keep_first(last_focused_after)

    updated = dict(existing)
    updated["allComposers"] = all_composers
    updated["selectedComposerIds"] = selected_after
    updated["lastFocusedComposerIds"] = last_focused_after
    # We are writing modern head entries backed by global composerData rows, so
    # mark the workspace blob as already migrated to reduce Cursor rewriting it
    # on next startup.
    updated["hasMigratedComposerData"] = True
    updated["hasMigratedMultipleComposers"] = True

    return WorkspaceUpdatePlan(
        workspace_db=workspace_db,
        composer_data_json=json.dumps(updated, separators=(",", ":")),
        pane_entries=pane_entries,
        head_count_before=len(existing.get("allComposers", []))
        if isinstance(existing.get("allComposers", []), list)
        else 0,
        head_count_after=len(all_composers),
        selected_before=selected_before,
        selected_after=selected_after,
    )


def backup_file(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.import-backup-{timestamp}")
    shutil.copy2(path, backup)
    return backup


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_bundle(bundle_path: Path, bundle_collection: BundleCollection, dry_run: bool) -> None:
    metadata = {
        "version": 2,
        "exportedAt": datetime.now().isoformat(),
        "chatCount": len(bundle_collection.chats),
        "chats": [
            {
                "composerId": chat.composer_id,
                "title": chat.title,
                "workspaceHead": chat.workspace_head,
                "rowsPath": f"chats/{chat.composer_id}/rows.json",
                "jsonlPath": f"chats/{chat.composer_id}/{chat.composer_id}.jsonl",
            }
            for chat in bundle_collection.chats
        ],
    }
    # if not args.export_bundle:
    #     print(f"Bundle destination: {bundle_path}")
    #     print(f"  chats:      {len(bundle_collection.chats)}")
    #     for chat in bundle_collection.chats:
    #         print(
    #             f"  - {summarize_chat(chat)} "
    #             f"(bubbles={len(chat.rows.bubble_rows)}, blobs={len(chat.rows.blob_rows)})"
    #         )

    if dry_run:
        return

    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        for chat in bundle_collection.chats:
            rows_payload = {
                "composer_key": chat.rows.composer_key,
                "composer_value": chat.rows.composer_value,
                "bubble_rows": chat.rows.bubble_rows,
                "blob_rows": chat.rows.blob_rows,
            }
            zf.writestr(
                f"chats/{chat.composer_id}/rows.json",
                json.dumps(rows_payload, ensure_ascii=False),
            )
            zf.writestr(
                f"chats/{chat.composer_id}/{chat.composer_id}.jsonl",
                chat.transcript_text,
            )


def load_bundle(bundle_path: Path) -> BundleCollection:
    ensure_exists(bundle_path, "Bundle file")
    with zipfile.ZipFile(bundle_path, "r") as zf:
        metadata = json.loads(zf.read("metadata.json").decode("utf-8"))
        if metadata.get("version") == 2:
            chats = []
            for chat_meta in metadata.get("chats", []):
                rows_payload = json.loads(
                    zf.read(chat_meta["rowsPath"]).decode("utf-8")
                )
                transcript_text = zf.read(chat_meta["jsonlPath"]).decode("utf-8")
                rows = ChatRows(
                    composer_key=rows_payload["composer_key"],
                    composer_value=rows_payload["composer_value"],
                    bubble_rows=dict(rows_payload["bubble_rows"]),
                    blob_rows=dict(rows_payload["blob_rows"]),
                )
                chats.append(
                    BundleData(
                        composer_id=chat_meta["composerId"],
                        title=chat_meta.get("title", "<untitled>"),
                        rows=rows,
                        transcript_text=transcript_text,
                        workspace_head=chat_meta.get("workspaceHead"),
                    )
                )
            return BundleCollection(chats=chats)

        rows_payload = json.loads(zf.read("rows.json").decode("utf-8"))
        composer_id = metadata["composerId"]
        transcript_name = f"{composer_id}.jsonl"
        transcript_text = zf.read(transcript_name).decode("utf-8")
        rows = ChatRows(
            composer_key=rows_payload["composer_key"],
            composer_value=rows_payload["composer_value"],
            bubble_rows=dict(rows_payload["bubble_rows"]),
            blob_rows=dict(rows_payload["blob_rows"]),
        )
        return BundleCollection(
            chats=[
                BundleData(
                    composer_id=composer_id,
                    title=metadata.get("title", "<untitled>"),
                    rows=rows,
                    transcript_text=transcript_text,
                    workspace_head=metadata.get("workspaceHead"),
                )
            ]
        )


def import_rows(dest_db: Path, rows: ChatRows, dry_run: bool) -> None:
    all_rows: List[Tuple[str, str]] = [
        (rows.composer_key, rows.composer_value),
        *rows.bubble_rows.items(),
        *rows.blob_rows.items(),
    ]

    print(f"Will import {len(all_rows)} rows into: {dest_db}")
    print(f"  composer rows: 1")
    print(f"  bubble rows:   {len(rows.bubble_rows)}")
    print(f"  blob rows:     {len(rows.blob_rows)}")

    if dry_run:
        return

    try:
        with open_rw(dest_db) as conn:
            cur = conn.cursor()
            cur.execute("BEGIN")
            cur.executemany(
                "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
                all_rows,
            )
            conn.commit()
    except sqlite3.OperationalError as exc:
        fail(
            f"Failed to write destination DB. Close Cursor first. SQLite said: {exc}"
        )


def apply_workspace_update(plan: WorkspaceUpdatePlan, dry_run: bool) -> None:
    print(f"Will update workspace DB: {plan.workspace_db}")
    print(f"  composer heads: {plan.head_count_before} -> {plan.head_count_after}")
    print(f"  selected IDs:   {plan.selected_before} -> {plan.selected_after}")
    print(f"  new panes:      {len(plan.pane_entries)}")

    if dry_run:
        return

    try:
        with open_rw(plan.workspace_db) as conn:
            cur = conn.cursor()
            cur.execute("BEGIN")
            cur.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                ("composer.composerData", plan.composer_data_json),
            )
            for pane_key, pane_value, pane_visible_key, pane_visible_value in plan.pane_entries:
                cur.execute(
                    "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                    (pane_key, pane_value),
                )
                cur.execute(
                    "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                    (pane_visible_key, pane_visible_value),
                )
            conn.commit()
    except sqlite3.OperationalError as exc:
        fail(
            "Failed to write workspaceStorage DB. "
            f"Close Cursor first. SQLite said: {exc}"
        )


def copy_transcript(
    transcript_text: str,
    projects_root: Path,
    target_project_id: str,
    composer_id: str,
    dry_run: bool,
) -> Path:
    dest = (
        projects_root
        / target_project_id
        / "agent-transcripts"
        / composer_id
        / f"{composer_id}.jsonl"
    )
    print(f"Transcript destination: {dest}")
    if dry_run:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(transcript_text, encoding="utf-8")
    return dest


def validate_workspace_storage(cursor_root: Path, workspace_id: str | None) -> None:
    if not workspace_id:
        return
    path = cursor_root / "User" / "workspaceStorage" / workspace_id
    ensure_exists(path, "Target workspaceStorage folder")
    print(f"Validated workspaceStorage: {path}")


def resolve_jsonl_path_from_dir(transcripts_dir: Path, composer_id: str) -> Path:
    candidate = transcripts_dir / composer_id / f"{composer_id}.jsonl"
    ensure_exists(candidate, f"Transcript for {composer_id}")
    return candidate


def discover_composer_ids(transcripts_dir: Path) -> List[str]:
    ensure_exists(transcripts_dir, "Source transcripts dir")
    ids = []
    for child in sorted(transcripts_dir.iterdir()):
        if not child.is_dir():
            continue
        jsonl_path = child / f"{child.name}.jsonl"
        if jsonl_path.exists():
            ids.append(child.name)
    return ids


def resolve_bundle_or_source(args: argparse.Namespace) -> BundleCollection:
    if args.import_bundle:
        bundle_path = Path(args.import_bundle).expanduser().resolve()
        bundle = load_bundle(bundle_path)
        print("Loaded bundle")
        print(f"  file:       {bundle_path}")
        print(f"  chats:      {len(bundle.chats)}")
        return bundle

    if not args.source_db:
        fail("Missing --source-db")

    source_db = Path(args.source_db).expanduser().resolve()
    ensure_exists(source_db, "Source DB")
    workspace_heads: Dict[str, Dict[str, Any]] = {}
    source_workspace_db = resolve_source_workspace_db(
        source_db=source_db,
        source_workspace_db_arg=args.source_workspace_db,
        source_workspace_id=args.source_workspace_id,
    )
    if source_workspace_db is not None:
        workspace_heads = load_source_workspace_heads(source_workspace_db)
        print(
            f"Loaded source workspace heads: {len(workspace_heads)} from {source_workspace_db}"
        )

    chats: List[BundleData] = []
    if args.source_jsonl:
        source_jsonl = Path(args.source_jsonl).expanduser().resolve()
        ensure_exists(source_jsonl, "Source jsonl")
        composer_id = infer_composer_id(source_jsonl, args.composer_id)
        rows = load_chat_rows(source_db, composer_id)
        chats.append(
            BundleData(
                composer_id=composer_id,
                title=preview_title(rows.composer_value),
                rows=rows,
                transcript_text=read_text_file(source_jsonl),
                workspace_head=workspace_heads.get(composer_id),
            )
        )
        return BundleCollection(chats=chats)

    if not args.source_transcripts_dir:
        fail("Missing --source-jsonl or --source-transcripts-dir")

    transcripts_dir = Path(args.source_transcripts_dir).expanduser().resolve()
    ensure_exists(transcripts_dir, "Source transcripts dir")
    composer_ids = normalize_composer_ids(args.composer_ids)
    if args.all_transcripts:
        composer_ids = discover_composer_ids(transcripts_dir)
    elif composer_ids:
        composer_ids = composer_ids
    else:
        fail(
            "When using --source-transcripts-dir, pass either --all-transcripts "
            "or --composer-ids"
        )

    skipped_ids: List[str] = []
    for composer_id in composer_ids:
        source_jsonl = resolve_jsonl_path_from_dir(transcripts_dir, composer_id)
        try:
            rows = load_chat_rows(source_db, composer_id)
        except MissingComposerRowError as exc:
            skipped_ids.append(composer_id)
            print(f"WARNING: {exc}. Skipping {composer_id}.")
            continue
        chats.append(
            BundleData(
                composer_id=composer_id,
                title=preview_title(rows.composer_value),
                rows=rows,
                transcript_text=read_text_file(source_jsonl),
                workspace_head=workspace_heads.get(composer_id),
            )
        )
    if skipped_ids:
        print(f"Skipped {len(skipped_ids)} chat(s) with missing composerData.")
    if not chats:
        fail("No importable chats were found after filtering/skipping.")
    return BundleCollection(chats=chats)


def main() -> None:
    args = parse_args()
    cursor_root = Path(args.cursor_root).expanduser().resolve()
    projects_root = Path(args.projects_root).expanduser().resolve()
    target_global_db_arg = args.target_global_db or args.dest_global_db
    dest_global_db = Path(target_global_db_arg).expanduser().resolve()
    target_workspace_id = args.target_workspace_id or args.target_workspace_storage_id
    do_workspace_import = args.include_workspace_storage or args.workspace_storage_only

    bundle = resolve_bundle_or_source(args)

    if args.export_bundle:
        bundle_path = Path(args.export_bundle).expanduser().resolve()
        print("Export summary")
        print(f"  chats:      {len(bundle.chats)}")
        for chat in bundle.chats:
            print(f"  - {summarize_chat(chat)}")
        write_bundle(bundle_path, bundle, args.dry_run)
        print("")
        if args.dry_run:
            print("Dry run complete. No files were changed.")
        else:
            print("Export complete.")
        return

    if not args.workspace_storage_only and not args.target_project_id:
        fail("Missing --target-project-id for import")

    if args.workspace_storage_only and not target_workspace_id:
        fail(
            "--workspace-storage-only requires --target-workspace-id "
            "or --target-workspace-storage-id"
        )

    if not args.workspace_storage_only:
        ensure_exists(dest_global_db, "Destination global DB")
    validate_workspace_storage(
        cursor_root,
        target_workspace_id if do_workspace_import else None,
    )

    print("Import summary")
    print(f"  chats:      {len(bundle.chats)}")
    if args.workspace_storage_only:
        print("  mode:       workspaceStorage-only")
    else:
        print("  mode:       full import")
        print(f"  target global db: {dest_global_db}")
        print(f"  target pid: {args.target_project_id}")
    if target_workspace_id:
        print(f"  target workspace id: {target_workspace_id}")
    for chat in bundle.chats:
        print(f"  - {summarize_chat(chat)}")

    workspace_plan = None
    if do_workspace_import:
        if not target_workspace_id:
            fail(
                "--include-workspace-storage requires --target-workspace-id "
                "or --target-workspace-storage-id"
            )
        workspace_plan = build_workspace_update_plan(
            cursor_root=cursor_root,
            workspace_id=target_workspace_id,
            chats=bundle.chats,
        )

    if not args.no_backup and not args.dry_run:
        if not args.workspace_storage_only:
            db_backup = backup_file(dest_global_db)
            print(f"Backed up destination DB: {db_backup}")

            for chat in bundle.chats:
                transcript_dest = (
                    projects_root
                    / args.target_project_id
                    / "agent-transcripts"
                    / chat.composer_id
                    / f"{chat.composer_id}.jsonl"
                )
                if transcript_dest.exists():
                    transcript_backup = backup_file(transcript_dest)
                    print(f"Backed up existing transcript: {transcript_backup}")

        if workspace_plan is not None:
            workspace_backup = backup_file(workspace_plan.workspace_db)
            print(f"Backed up workspace DB: {workspace_backup}")

    if not args.workspace_storage_only:
        for chat in bundle.chats:
            import_rows(dest_global_db, chat.rows, args.dry_run)
    if workspace_plan is not None:
        apply_workspace_update(workspace_plan, args.dry_run)
    transcript_paths = []
    if not args.workspace_storage_only:
        for chat in bundle.chats:
            transcript_paths.append(
                copy_transcript(
                    transcript_text=chat.transcript_text,
                    projects_root=projects_root,
                    target_project_id=args.target_project_id,
                    composer_id=chat.composer_id,
                    dry_run=args.dry_run,
                )
            )

    print("")
    if args.dry_run:
        print("Dry run complete. No files were changed.")
    else:
        print("Import complete.")
        if args.workspace_storage_only:
            print("Workspace storage updated only.")
        else:
            print(f"Transcripts copied: {len(transcript_paths)}")
        print("Next steps:")
        print("  1. Start Cursor")
        print("  2. Open the target workspace")
        print("  3. Check chat history / composer history")


if __name__ == "__main__":
    main()
