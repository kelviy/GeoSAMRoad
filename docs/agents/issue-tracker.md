# Issue tracker: Linear

Issues and specs for this repo live in Linear: team **TheWay** (key `KEL`), project **GeoSamRoad** (`P-KEL-1`). Use the Linear MCP tools (`mcp__linear-server__*`) for all operations. Tools accept human-readable references (`KEL-123`, `P-KEL-1`, `KEL`, `me`), so ID lookups are rarely needed.

## Conventions

- **Create an issue**: `save_issue` (no `id`) with `team: "KEL"`, `project: "P-KEL-1"`, a title, and a markdown description (real newlines, no `\n` escapes). Check `list_issues` with a `query` first to avoid duplicates. Put agent-specific detail (plans, file lists, repro logs) in a collapsible `+++ Agent notes` … `+++` section at the end of the description.
- **Read an issue**: `get_issue` for the body, then `list_comments` for the discussion (`get_issue` does not include comments).
- **List issues**: `list_issues` filtered by `team: "KEL"`, `project: "P-KEL-1"`, and `label` / `state` / `assignee` as needed. Follow `cursor` while `hasNextPage` is true.
- **Make an issue a sub-issue of a parent**: `save_issue` with `parentId` set to the parent (e.g. `KEL-12`).
- **Comment on an issue**: `save_comment` with the issue ID and a markdown body.
- **Apply / remove labels**: `save_issue` with `id` plus `addLabels` / `removeLabels`. Never use `labels` for this; it replaces the whole label set.
- **Close**: `save_comment` with the closing note, then `save_issue` with `state: "Done"` (completed) or `state: "Canceled"` (won't do). Use `state: "Duplicate"` for duplicates.

Team statuses: Backlog, Todo, In Progress, In Review, Done, Canceled, Duplicate. Team type labels: Bug, Feature, Improvement.

## When a skill says "publish to the issue tracker"

Create a Linear issue in team `KEL`, project `P-KEL-1`.

## When a skill says "fetch the relevant ticket"

Read it as in **Read an issue** above.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single parent issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map` in `P-KEL-1`, holding the Notes / Decisions-so-far / Fog body.
- **Child ticket**: a sub-issue of the map (`parentId` = map). Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Create any missing `wayfinder:*` labels on team `KEL` first. Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: Linear's native "blocked by" issue relations, set via `save_issue` on the child. Where relations can't be set, fall back to a `Blocked by: KEL-<n>, KEL-<n>` line at the top of the child description. A ticket is unblocked when every blocker is Done/Canceled.
- **Frontier query**: `list_issues` for the map's open children (parent = map, state not completed/canceled); drop any with an open blocker or an assignee; first in map order wins.
- **Claim**: `save_issue` with `assignee: "me"` (and `state: "In Progress"`), the session's first write.
- **Resolve**: `save_comment` with the answer, set `state: "Done"`, then append a context pointer (gist + issue link) to the map's Decisions-so-far.
