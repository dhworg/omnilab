# OmniLab CLI conventions

Source of truth: `project-spec-v1.md` § "CLI conventions". This page is
the implementation contract — what every OmniLab command MUST honor so
the CLI is a usable agent API.

## 1. Dual-mode output

Every **read-only** command emits both human-readable text/TUI (default)
and a structured JSON snapshot (`--json`). The CLI is the API.

- `--json` is a **root-level** flag accepted before the subcommand:
  ```sh
  omnilab --json doctor
  omnilab --json inspect
  ```
- In JSON mode commands MUST NOT print prose to stdout. Only the JSON
  document goes to stdout; warnings/errors go to stderr.
- The JSON schema for each command is documented inline in its module
  docstring and tested in `tests/test_*_json_schema.py`.
- Implementation: use `omnilab._output.emit(human="…", data={…})` —
  the helper picks the right mode automatically.

## 2. Destructive-command safety

Every **destructive** command (`clean`, `down`, `record --stop`, etc.)
MUST accept:

| Flag | Meaning |
|---|---|
| `--dry-run` | Preview the actions, **do not** execute. Exit 0. |
| `--yes` / `-y` | Skip the confirmation prompt. For scripting / agents. |

Default behavior:
1. Print a summary of what will happen (in human mode) or the structured
   plan (in JSON mode).
2. If `--dry-run`: stop here with exit 0.
3. Else if `--yes`: proceed.
4. Else: prompt `Proceed? [y/N]` interactively. "No" exits 0.

In **JSON mode**, interactive prompts are not possible — destructive
commands therefore require `--yes` explicitly when `--json` is set, and
emit a structured "would do" preview otherwise. Calling a destructive
command with `--json` and without `--yes` exits with code 2 (invalid
args).

Implementation: `omnilab._safety.confirm_or_exit(summary=…, items=…,
yes=…, dry_run=…)`.

## 3. Documented exit codes

Agents rely on these.

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Generic error (no more specific code applies) |
| `2` | Invalid arguments / usage |
| `3` | State error (e.g. container not running, no `omnilab.yaml`) |
| `4` | Network or remote-auth error (registry pull, GHCR login) |
| `5` | Permission / local-auth error (sudo needed, missing group) |

Avoid raising bare `typer.Exit(1)` from new code — pick the specific
class (`emit_error(..., code=3)` etc.) so agents can branch on it.

## 4. Predictable structure

Same flag means the same thing across commands.

| Flag | Reserved meaning |
|---|---|
| `--json` | Machine-readable output |
| `--dry-run` | Preview only |
| `--yes` / `-y` | Skip confirmation |
| `--directory` / `-d` | Project directory (default `cwd`) |
| `--all` | Cross-project scope (`clean`, `record --stop`) |
| `--aggressive` | Escalate (`clean`) |
| `--refresh <hz>` | TUI refresh rate (`inspect`, etc.) |
| `--validate <path>` | Lint the named config file |

If you need a new flag whose name overlaps with one above but means
something different, **rename it**. Agents and humans both rely on this
contract.

## 5. Testing requirements

For each command:

- `--help` exits 0 and lists all flags.
- `--json` output is valid JSON (round-trips through `json.loads`).
- `--json` schema is stable and documented (renames are breaking
  changes; require a SemVer minor bump).
- For destructive commands: `--dry-run` is a no-op (no podman calls,
  no filesystem writes); `--yes` skips prompt; default flow asks.
- Documented exit codes are returned for documented failure modes.
