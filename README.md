# Cursor Chat Transfer

This folder contains a small tool for exporting Cursor chat history from one
machine and importing it into another machine.

Files:

- `import_cursor_chat.py`: main command-line script
## Common Workflows

### Shared Environment Variables

Set these once in your shell before running the examples below:

```bash
export SOURCE_GLOBAL_DB="/path/to/source/User/globalStorage/state.vscdb" #/Users/USER/Library/Application Support/Cursor/User/globalStorage
export SOURCE_WORKSPACE_ID="<source-workspace-id>"
export SOURCE_WORKSPACE_DB="/path/to/source/User/workspaceStorage/<source-workspace-id>/state.vscdb"
export SOURCE_TRANSCRIPTS_DIR="/home/<user>/.cursor/projects/<source-project-id>/agent-transcripts"
export TARGET_GLOBAL_DB="/path/to/local/Cursor/User/globalStorage/state.vscdb"
export TARGET_WORKSPACE_ID="<target-workspace-id>"
export TARGET_PROJECT_ID="<target-project-id>"
export BUNDLE_PATH="/tmp/exported-chats.zip"
export COMPOSER_ID="<composer-id>"
```

You only need the variables that apply to the workflow you are running.

### 1. Export All Chats From A Transcript Directory

```bash
python import_cursor_chat.py \
  --source-db "$SOURCE_GLOBAL_DB" \
  --source-workspace-id "$SOURCE_WORKSPACE_ID" \
  --source-transcripts-dir "$SOURCE_TRANSCRIPTS_DIR" \
  --all-transcripts \
  --export-bundle "$BUNDLE_PATH" \
  --dry-run
```

Then run again without `--dry-run` to create the zip.

### 2. Export Only Specific Chat IDs

```bash
python import_cursor_chat.py \
  --source-db "$SOURCE_GLOBAL_DB" \
  --source-workspace-id "$SOURCE_WORKSPACE_ID" \
  --source-transcripts-dir "$SOURCE_TRANSCRIPTS_DIR" \
  --composer-ids \
    "<composer-id-a>" \
    "<composer-id-b>" \
    "<composer-id-c>" \
  --export-bundle "$BUNDLE_PATH" \
  --dry-run
```

### 3. Export One Chat Only

```bash
python import_cursor_chat.py \
  --source-db "$SOURCE_GLOBAL_DB" \
  --source-workspace-id "$SOURCE_WORKSPACE_ID" \
  --source-jsonl "$SOURCE_TRANSCRIPTS_DIR/$COMPOSER_ID/$COMPOSER_ID.jsonl" \
  --export-bundle "/tmp/$COMPOSER_ID.zip" \
  --dry-run
```

### 4. Import A Bundle On The Local Machine

```bash
python import_cursor_chat.py \
  --import-bundle "$BUNDLE_PATH" \
  --target-global-db "$TARGET_GLOBAL_DB" \
  --target-project-id "$TARGET_PROJECT_ID" \
  --target-workspace-id "$TARGET_WORKSPACE_ID" \
  --include-workspace-storage \
  --dry-run
```

If the dry run looks correct, remove `--dry-run`.

### 5. Repair Only Workspace Chat Visibility

Use this if the transcript files and global DB rows already exist, but the chats
still do not appear in the target workspace history.

```bash
python import_cursor_chat.py \
  --import-bundle "$BUNDLE_PATH" \
  --target-workspace-id "$TARGET_WORKSPACE_ID" \
  --workspace-storage-only \
  --dry-run
```

This mode only updates:

- `workspaceStorage/<target-workspace-id>/state.vscdb`

It skips:

- `globalStorage/state.vscdb`
- transcript file copies under `~/.cursor/projects/.../agent-transcripts`

## What It Can Do

- Export a single chat from `state.vscdb` + `jsonl` into a portable zip bundle
- Export many chats from an `agent-transcripts` directory into one bundle
- Optionally read the source workspace's `workspaceStorage/.../state.vscdb`
  so imported chats keep the source workspace chat head metadata
- Import that bundle into local Cursor:
  - target `globalStorage/state.vscdb`
  - target `~/.cursor/projects/<target-project-id>/agent-transcripts/...`
  - target `workspaceStorage/<workspace-id>/state.vscdb`

## Important Notes

- Close Cursor before importing.
- The tool makes backups by default unless you pass `--no-backup`.
- In batch export mode, if a transcript folder exists but its matching
  `composerData:<id>` row is missing from the source DB, that chat is skipped
  with a warning instead of aborting the whole run.

## How Cursor Chat Visibility Works

This section is the most important part of the README if imported chats exist on
disk but do not show in Cursor's sidebar/history.

Cursor chat state is split across multiple storage locations:

- `globalStorage/state.vscdb`
  - table: `cursorDiskKV`
  - stores the actual chat payload
  - important keys:
    - `composerData:<composerId>`
    - `bubbleId:<composerId>:<bubbleId>`
    - referenced blob keys inside those JSON payloads
- `workspaceStorage/<workspace-id>/state.vscdb`
  - table: `ItemTable`
  - stores the workspace-local chat history/index state
  - important keys:
    - `composer.composerData`
    - `workbench.panel.composerChatViewPane.<pane-id>`
    - `workbench.panel.aichat.<pane-id>.numberOfVisibleViews`
- transcript files
  - path:
    `~/.cursor/projects/<project-id>/agent-transcripts/<composerId>/<composerId>.jsonl`
  - useful for transcript browsing and external transfer, but not the primary
    driver of sidebar visibility

In practice:

- `globalStorage` answers: "Does the chat content exist?"
- `workspaceStorage` answers: "Should this workspace show the chat in history?"
- transcript files answer: "Does the jsonl transcript exist on disk?"

The critical workspace key is `composer.composerData`. Its `allComposers` array
contains sidebar/history head records. If a chat has `composerData:<id>` and
`bubbleId:<id>:*` in `globalStorage` but is missing from
`workspaceStorage/.../composer.composerData -> allComposers`, the chat content
exists but the sidebar usually will not list it.

Pane rows matter too, but they are secondary:

- `workbench.panel.composerChatViewPane.<pane-id>` stores a mapping like
  `workbench.panel.aichat.view.<composerId> -> { collapsed, isHidden, size }`
- `workbench.panel.aichat.<pane-id>.numberOfVisibleViews` is the matching
  per-pane count row

Important takeaway:

- chat content alone is not enough
- transcript files alone are not enough
- pane rows alone are not enough
- the sidebar needs a valid workspace head in `allComposers`

## Postmortem: Chats Imported But Missing In Sidebar

This tool was debugged against a real failure case where:

- transcript files existed
- `globalStorage/state.vscdb` already contained the imported
  `composerData:<id>` and `bubbleId:<id>:*` rows
- pane rows existed in `workspaceStorage`
- but imported chats still did not appear in Cursor's sidebar

### What Actually Happened

The imported chats were present in `globalStorage`, but their workspace-local
head records were either missing or later disappeared from:

- `workspaceStorage/<workspace-id>/state.vscdb`
- key: `composer.composerData`
- field: `allComposers`

During debugging we confirmed a newly created local chat did show up because it
had:

- a valid `composerData:<id>` row in `globalStorage`
- a valid head entry inside `composer.composerData -> allComposers`
- a pane mapping

The imported historical chats often still had:

- valid `composerData:<id>` rows
- valid `bubbleId:<id>:*` rows
- valid pane rows

but they no longer had a corresponding head entry in `allComposers`, so Cursor
had nothing to render in the sidebar history list.

### Why The Old Import Logic Was Fragile

The original workspace import logic had several problems:

1. It accepted source workspace heads too literally.
   Some source `allComposers` entries were sparse legacy head objects. After
   import, those heads could be missing fields that Cursor normally expects on
   modern sidebar entries.
2. It prepended every imported chat into both:
   - `selectedComposerIds`
   - `lastFocusedComposerIds`
   This made the workspace state noisier than a normal Cursor-created state.
3. It always created new pane rows.
   Re-running imports could create duplicate pane mappings for the same chat.
4. It left migration flags in an unstable state.
   Specifically, `hasMigratedComposerData` could remain `False`, which made it
   easier for Cursor startup logic to reinterpret or rewrite the workspace blob.

### What Fixed It

The importer was updated so workspace repair is now much more conservative and
much closer to Cursor's own steady-state data shape.

Current behavior:

- `--workspace-storage-only` updates only the target workspace DB
- source workspace heads are normalized by first building a full head from
  `composerData:<id>`, then overlaying source workspace metadata
- imported heads are upserted into `allComposers`
- existing pane rows are reused; new pane rows are created only when missing
- imported chats are no longer all pushed into `selectedComposerIds` /
  `lastFocusedComposerIds`
- migration flags are written as:
  - `hasMigratedComposerData = true`
  - `hasMigratedMultipleComposers = true`

This greatly reduces the chance that Cursor will later rewrite imported heads
out of `allComposers`.

### How To Recognize This Failure Again

If someone hits the same bug later, the usual signature is:

- transcript files exist
- `globalStorage/state.vscdb` contains `composerData:<id>`
- `globalStorage/state.vscdb` contains `bubbleId:<id>:*`
- `workspaceStorage/<workspace-id>/state.vscdb` contains pane rows
- but `composer.composerData -> allComposers` does not contain the imported
  `composerId`

That means:

- import content layer: good
- workspace visibility layer: broken

### Fastest Recovery Procedure

1. Close Cursor.
2. Re-run import in workspace-only mode:

```bash
python import_cursor_chat.py \
  --import-bundle "/path/to/bundle.zip" \
  --target-workspace-id "<workspace-id>" \
  --workspace-storage-only
```

3. Open Cursor and verify the chats are back in the sidebar.

If they still do not show, inspect:

- `composer.composerData`
- `allComposers`
- whether the imported `composerId` exists there at all

Do not start by debugging transcript files. In this incident, transcript files
were not the reason the sidebar was empty.

### Pitfalls And Lessons Learned

- Do not assume transcript files control sidebar visibility.
- Do not assume `globalStorage` is enough.
- Do not assume pane rows are the primary problem.
- If imported chats exist but sidebar is empty, inspect `allComposers` first.
- Avoid adding every imported chat to `selectedComposerIds` and
  `lastFocusedComposerIds`.
- Avoid recreating pane rows if a pane already exists for that `composerId`.
- Prefer normalizing workspace heads from `composerData:<id>` rather than
  trusting sparse legacy workspace heads.
- Keep migration flags in a modern completed state after repair.

### Remote Transcript Path Note

In the investigated scenario, the real source transcript root was remote, for
example:

- `/home/<user>/.cursor/projects/<source-project-id>/agent-transcripts`

That path matters for export and for understanding where the original jsonl
files came from. But once the content has already been imported into local
`globalStorage`, missing sidebar visibility is usually a `workspaceStorage`
problem, not a transcript path problem.


## Data Flow

Export side:

- source global DB:
  `.../User/globalStorage/state.vscdb`
- optional source workspace DB:
  `.../User/workspaceStorage/<source-workspace-id>/state.vscdb`

Import side:

- target global DB:
  `.../User/globalStorage/state.vscdb`
- target workspace DB:
  `.../User/workspaceStorage/<target-workspace-id>/state.vscdb`
- target transcript directory:
  `~/.cursor/projects/<target-project-id>/agent-transcripts/...`

So in practice this tool exports from two sources and imports into two targets:

- source `globalStorage`
- source `workspaceStorage`
- target `globalStorage`
- target `workspaceStorage`
- target transcript project directory

## Current Scenario Example

An example mapping looks like this:

- source global DB:
  `/path/to/source/User/globalStorage/state.vscdb`
- source workspace DB:
  `/path/to/source/User/workspaceStorage/<source-workspace-id>/state.vscdb`
- source transcript root:
  `/home/<user>/.cursor/projects/<source-project-id>/agent-transcripts`
- target global DB:
  `/path/to/local/Cursor/User/globalStorage/state.vscdb`
- target workspace DB:
  `/path/to/local/Cursor/User/workspaceStorage/<target-workspace-id>/state.vscdb`
- target project id:
  the local folder name under `~/.cursor/projects/<id>` that should receive the copied transcripts

Export all chats from the remote side:

```bash
python import_cursor_chat.py \
  --source-db "$SOURCE_GLOBAL_DB" \
  --source-workspace-db "$SOURCE_WORKSPACE_DB" \
  --source-transcripts-dir "$SOURCE_TRANSCRIPTS_DIR" \
  --all-transcripts \
  --export-bundle "$BUNDLE_PATH" \
  --dry-run
```

Import on the local side:

```bash
python import_cursor_chat.py \
  --import-bundle "$BUNDLE_PATH" \
  --target-global-db "$TARGET_GLOBAL_DB" \
  --target-project-id "$TARGET_PROJECT_ID" \
  --target-workspace-storage-id "$TARGET_WORKSPACE_ID" \
  --include-workspace-storage \
  --dry-run
```

## Relevant Flags

- `--source-db`: source machine `state.vscdb`
- `--source-jsonl`: one chat transcript file
- `--source-transcripts-dir`: transcript root for batch export
- `--source-workspace-db`: source workspaceStorage `state.vscdb` to preserve source workspace chat heads
- `--source-workspace-id`: shorthand that derives `workspaceStorage/<id>/state.vscdb` from `--source-db`
- `--all-transcripts`: use all IDs found under the transcript root
- `--composer-ids`: only export specified IDs
- `--export-bundle`: write a portable zip bundle
- `--import-bundle`: import from a portable zip bundle
- `--dest-global-db`: target local globalStorage DB path
- `--target-global-db`: alias for `--dest-global-db`
- `--target-project-id`: target local `~/.cursor/projects/<id>`
- `--target-workspace-id`: alias for `--target-workspace-storage-id`
- `--target-workspace-storage-id`: target local workspace storage id
- `--include-workspace-storage`: also merge chat heads into workspace storage
- `--workspace-storage-only`: repair workspace chat heads/panes without rewriting global DB or transcripts
- `--dry-run`: preview only
- `--no-backup`: skip backups

## Source vs Target IDs

These are different concepts and do not need to match:

- Source workspaceStorage id:
  example `<source-workspace-id>`
  This can be passed as `--source-workspace-id`.
  The script will derive:
  `parent(parent(--source-db))/workspaceStorage/<id>/state.vscdb`

- Target local project id:
  this is the local folder under `~/.cursor/projects/<id>` where imported
  transcripts are copied.

- Target local workspaceStorage id:
  example `<target-workspace-id>`
  This is the local workspace whose `workspaceStorage/.../state.vscdb` should be
  expanded during import. You can pass it as `--target-workspace-id`.
