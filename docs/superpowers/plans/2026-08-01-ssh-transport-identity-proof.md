# SSH Transport Identity Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the partial SSH-alias migration from PR #365 with one end-to-end, provider-authoritative endpoint proof that binds the remote, canonical host, key fingerprint, SSH principal, migration plan, and Git publication path.

**Architecture:** Stable repository identity remains provider-owned and separate from machine-local transport evidence. A constrained SSH configuration parser and key/principal proof pipeline produce a structured `ReviewedSshEndpoint`; migration persists that proof, and the Git transport router revalidates it before constructing a canonical execution URL under operation-scoped key material. Legacy path-only SSH profiles remain readable but cannot authorize network writes.

**Tech Stack:** Python 3.10+, frozen dataclasses, protocols, TOML source/resolved configuration, bounded subprocess adapters, pytest, Ruff, mypy strict, RepoForge durable configuration and publication contracts.

## Global Constraints

- Do not modify `~/.ssh/config`, global Git configuration, the active `gh` account, or repository remotes.
- Normal observation, migration authority, and write admission must not invoke `ssh -G`.
- The selected named account or reviewed profile is the sole authority for the GitHub API host.
- Private key bytes must not enter argv, environment values, domain objects, logs, exceptions, receipts, or persistent test artifacts.
- SSH remotes with incomplete or unsafe evidence are blocking; migration must not silently fall back from SSH to HTTPS.
- `portal-spa` must keep `git@github-work:cicdata-io/portal-spa.git` unchanged.
- Existing HTTPS profiles and persisted operation identity records must remain decodable.
- Existing path-only SSH profiles remain parseable but must fail network writes with a typed migration-required error.
- Every production behavior change follows RED → observed expected failure → GREEN → focused refactor.
- Keep each changed Python file at or below the repository's checked 400-line limit; split new responsibilities into focused modules.

---

## File Structure

### New domain and port files

- `src/repoforge/domain/git_remote_identity.py`: pure parsed-remote, alias, key, principal, and reviewed-endpoint values plus canonical execution URL construction.
- `src/repoforge/ports/git_remote_identity.py`: protocols for remote parsing, alias resolution, key inspection/materialization, principal verification, and endpoint revalidation.

### New adapters

- `src/repoforge/adapters/git/remote_identity.py`: bounded Git remote parser and constrained OpenSSH user-config parser.
- `src/repoforge/adapters/git/ssh_key_identity.py`: no-follow key inspection, `ssh-keygen` fingerprint adapter, and operation-scoped materialization.
- `src/repoforge/adapters/github/ssh_principal.py`: bounded GitHub SSH principal probe.

### Existing files to modify

- `src/repoforge/domain/git_transport_identity.py`: transport spec/evidence reference the structured endpoint proof.
- `src/repoforge/ports/auth_inspection.py`: repository target contains no transport alias; observation accepts an expected provider host.
- `src/repoforge/adapters/github/repository_observation.py`: parse local target without alias authority and call the API only on the expected host.
- `src/repoforge/application/configuration/source.py`: parse/render structured SSH endpoint proof while preserving legacy fields.
- `src/repoforge/config.py`: compile source endpoint proof into runtime `GitTransportSpec`.
- `src/repoforge/application/auth_migration.py`: gather one endpoint snapshot, block unsafe SSH, persist proof, and remove SSH→HTTPS fallback.
- `src/repoforge/adapters/git/transport.py`: revalidate raw endpoint and execute canonical URL with operation-scoped key material.
- `src/repoforge/adapters/publication.py`: continue preserving raw topology while consuming canonical transport evidence.
- `src/repoforge/bootstrap.py`: compose the same endpoint proof adapters for CLI and managed runtime.
- `src/repoforge/application/auth_ux.py`: report proof completeness and legacy migration-required state.
- `src/repoforge/domain/errors.py`: add typed error codes only where existing codes cannot express migration-required, unsafe SSH config, stale endpoint proof, key mismatch, or principal mismatch.
- `docs/development/REPOSITORY_IDENTITY.md`: document provider-host authority, endpoint proofs, and legacy behavior.

### Test files

- Create `tests/test_git_remote_identity.py`.
- Create `tests/test_ssh_config_alias.py`.
- Create `tests/test_ssh_key_identity.py`.
- Create `tests/test_github_ssh_principal.py`.
- Modify `tests/test_auth_profile_config.py`.
- Modify `tests/test_auth_repository_observation.py`.
- Modify `tests/test_auth_migration.py`.
- Modify `tests/test_git_transport_identity.py`.
- Modify `tests/test_publication_adapter.py`.
- Modify `tests/test_auth_cli.py` and `tests/test_service_tools.py` for production composition.

---

### Task 1: Separate stable repository identity from structured transport proof

**Files:**
- Create: `src/repoforge/domain/git_remote_identity.py`
- Modify: `src/repoforge/domain/git_transport_identity.py`
- Modify: `src/repoforge/application/configuration/source.py`
- Modify: `src/repoforge/config.py`
- Create: `tests/test_git_remote_identity.py`
- Modify: `tests/test_auth_profile_config.py`

**Interfaces:**
- Produces:
  ```python
  class GitRemoteKind(str, Enum):
      SSH = "ssh"
      HTTPS = "https"

  @dataclass(frozen=True, slots=True)
  class ParsedGitRemote:
      kind: GitRemoteKind
      raw_host: str
      owner: str
      repository: str
      user: str | None
      port: int | None
      raw_url_digest: str

  @dataclass(frozen=True, slots=True)
  class SshAliasDefinition:
      alias: str
      canonical_host: str
      user: str
      port: int
      identity_file: str
      source_config_digest: str
      selected_block_digest: str

  @dataclass(frozen=True, slots=True)
  class SshKeyProof:
      canonical_path: str
      public_key_fingerprint: str
      owner_uid: int
      mode: int
      observed_at: str

  @dataclass(frozen=True, slots=True)
  class SshPrincipalProof:
      provider_host: str
      principal_kind: str
      principal_login: str
      expected_actor_id: str
      key_fingerprint: str
      observed_at: str
      proof_digest: str

  @dataclass(frozen=True, slots=True)
  class ReviewedSshEndpoint:
      schema_version: int
      raw_host: str
      canonical_host: str
      user: str
      port: int
      owner: str
      repository: str
      raw_url_digest: str
      alias: SshAliasDefinition | None
      key: SshKeyProof
      principal: SshPrincipalProof
      proof_digest: str

      def canonical_url(self) -> str:
          raise NotImplementedError
  ```
- `GitTransportSpec` gains `ssh_endpoint: ReviewedSshEndpoint | None`; `ssh_identity_file` remains temporarily available only for legacy decoding.
- `SourceAuthProfile` gains `ssh_endpoint: SourceSshEndpointProof | None`, where `SourceSshEndpointProof.from_table()` validates the nested `ssh_endpoint` TOML table and `as_table()` produces only safe scalar metadata.

- [ ] **Step 1: Write failing domain tests**

Create tests proving:

```python
def _reviewed_endpoint(
    *,
    raw_host: str = "github-work",
    canonical_host: str = "github.com",
    key_fingerprint: str = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    principal_fingerprint: str = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
) -> ReviewedSshEndpoint:
    key = SshKeyProof(
        canonical_path="/Users/trung.ngo/.ssh/id_rsa_work",
        public_key_fingerprint=key_fingerprint,
        owner_uid=501,
        mode=0o600,
        observed_at="2026-08-01T00:00:00+00:00",
    )
    principal = SshPrincipalProof(
        provider_host=canonical_host,
        principal_kind="github_account",
        principal_login="matw-ngo",
        expected_actor_id="173029271",
        key_fingerprint=principal_fingerprint,
        observed_at="2026-08-01T00:00:00+00:00",
        proof_digest="b" * 64,
    )
    return ReviewedSshEndpoint(
        schema_version=1,
        raw_host=raw_host,
        canonical_host=canonical_host,
        user="git",
        port=22,
        owner="cicdata-io",
        repository="portal-spa",
        raw_url_digest="c" * 64,
        alias=None,
        key=key,
        principal=principal,
        proof_digest="d" * 64,
    )


def test_reviewed_ssh_endpoint_builds_a_canonical_execution_url() -> None:
    endpoint = _reviewed_endpoint(raw_host="github-work", canonical_host="github.com")
    assert endpoint.canonical_url() == "ssh://git@github.com:22/cicdata-io/portal-spa.git"


def test_endpoint_rejects_a_principal_key_fingerprint_mismatch() -> None:
    with pytest.raises(ValueError, match="principal key fingerprint"):
        _reviewed_endpoint(
            key_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            principal_fingerprint="SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        )


def test_repository_identity_observation_no_longer_accepts_transport_alias() -> None:
    with pytest.raises(TypeError):
        RepositoryIdentityObservation(
            provider=RepositoryProvider.GITHUB,
            provider_host="github.com",
            repository_id="341454638",
            canonical_name="github.com/cicdata-io/portal-spa",
            exists=True,
            observed_at="2026-08-01T00:00:00+00:00",
            config_revision="e" * 64,
            transport_alias="github-work",
        )
```

- [ ] **Step 2: Run domain tests and capture RED**

Run:

```bash
uv run pytest -q tests/test_git_remote_identity.py tests/test_auth_profile_config.py
```

Expected: collection/import failures for missing endpoint types and failing config round-trip assertions.

- [ ] **Step 3: Implement minimal pure domain values and digest validation**

Implement safe constructors that validate bounded lowercase hosts, owner/repository names, absolute paths, UID/mode ranges, port `1..65535`, SHA256/digest formats, matching endpoint/key/principal host and fingerprint, and canonical URL encoding without credentials.

- [ ] **Step 4: Add source configuration round trip**

Add:

```toml
[auth_profiles.matw-ngo.ssh_endpoint]
schema_version = 1
raw_host = "github-work"
canonical_host = "github.com"
user = "git"
port = 22
owner = "cicdata-io"
repository = "portal-spa"
raw_url_digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
canonical_path = "/Users/trung.ngo/.ssh/id_rsa_work"
public_key_fingerprint = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
owner_uid = 501
mode = 384
source_config_digest = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
selected_block_digest = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
principal_kind = "github_account"
principal_login = "matw-ngo"
principal_actor_id = "173029271"
principal_observed_at = "2026-08-01T00:00:00+00:00"
principal_proof_digest = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
proof_digest = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
```

Legacy profiles with `source_ssh_alias` and `ssh_identity_file` but no `ssh_endpoint` must parse and compile to `GitTransportSpec(ssh_endpoint=None, ssh_identity_file="/Users/trung.ngo/.ssh/id_rsa_work")`.

- [ ] **Step 5: Run focused tests GREEN**

Run:

```bash
uv run pytest -q tests/test_git_remote_identity.py tests/test_auth_profile_config.py
```

Expected: PASS.

- [ ] **Step 6: Run type/lint gate and commit**

Run:

```bash
uv run ruff format --check src/repoforge/domain/git_remote_identity.py src/repoforge/domain/git_transport_identity.py src/repoforge/application/configuration/source.py src/repoforge/config.py tests/test_git_remote_identity.py tests/test_auth_profile_config.py
uv run ruff check src/repoforge/domain/git_remote_identity.py src/repoforge/domain/git_transport_identity.py src/repoforge/application/configuration/source.py src/repoforge/config.py tests/test_git_remote_identity.py tests/test_auth_profile_config.py
uv run mypy --strict src/repoforge
```

Commit:

```bash
git add src/repoforge/domain/git_remote_identity.py src/repoforge/domain/git_transport_identity.py src/repoforge/application/configuration/source.py src/repoforge/config.py tests/test_git_remote_identity.py tests/test_auth_profile_config.py
git commit -m "feat(identity): model reviewed SSH endpoint proofs"
```

---

### Task 2: Replace implicit `ssh -G` authority with a constrained SSH config parser

**Files:**
- Create: `src/repoforge/ports/git_remote_identity.py`
- Create: `src/repoforge/adapters/git/remote_identity.py`
- Modify: `src/repoforge/adapters/git/ssh_alias_discovery.py`
- Create: `tests/test_ssh_config_alias.py`
- Modify: `tests/test_auth_import_adapters.py`

**Interfaces:**
- Produces:
  ```python
  class GitRemoteParser(Protocol):
      def parse(self, remote_url: str) -> ParsedGitRemote:
          raise NotImplementedError

  class SshAliasResolver(Protocol):
      def resolve(self, alias: str) -> SshAliasDefinition:
          raise NotImplementedError

  @dataclass(frozen=True, slots=True)
  class EffectiveUserPaths:
      home: Path
      ssh_config: Path

  class ConstrainedSshConfigAliasResolver:
      def __init__(self, *, paths: EffectiveUserPaths) -> None:
          self._paths = paths

      def resolve(self, alias: str) -> SshAliasDefinition:
          raise NotImplementedError
  ```
- `SshCommandAliasDiscovery` remains available only to explicit `rf auth import ssh` diagnostics and is not injected into repository observation or migration authority.

- [ ] **Step 1: Write failing parser tests**

Cover exact alias success and fail-closed cases:

```python
def test_exact_alias_resolves_without_running_ssh(tmp_path: Path) -> None:
    resolver = _resolver(tmp_path, """
    Host github-work
      HostName github.com
      User git
      Port 22
      IdentityFile ~/.ssh/id_rsa_work
      IdentitiesOnly yes
    """)
    resolved = resolver.resolve("github-work")
    assert resolved.alias == "github-work"
    assert resolved.canonical_host == "github.com"
    assert resolved.user == "git"
    assert resolved.port == 22
    assert resolved.identity_file == str(tmp_path / "home/.ssh/id_rsa_work")


@pytest.mark.parametrize("unsafe", [
    "Match exec true",
    "Include ~/.ssh/conf.d/*",
    "Host github-*",
    "ProxyCommand nc %h %p",
    "ProxyJump bastion",
    "IdentityAgent ~/.ssh/agent.sock",
    "CanonicalizeHostname yes",
])
def test_command_bearing_or_undecidable_config_is_blocking(tmp_path: Path, unsafe: str) -> None:
    with pytest.raises(RepoForgeError) as failure:
        _resolver(tmp_path, f"Host github-work\n  {unsafe}\n").resolve("github-work")
    assert failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH
```

Also prove repeated matching blocks, wildcard/negated host, multiple identity files, missing `IdentitiesOnly yes`, non-`git` user, invalid port, another-user `~name`, `%`/`$` tokens, and symlinked config are rejected.

- [ ] **Step 2: Run parser tests RED**

Run:

```bash
uv run pytest -q tests/test_ssh_config_alias.py
```

Expected: import failure for `ConstrainedSshConfigAliasResolver`.

- [ ] **Step 3: Implement bounded parser**

Read the effective user's explicit config path once with `O_NOFOLLOW`, require a regular file owned by the effective UID, bound it to 200 KiB and 2,000 lines, parse only exact `Host` blocks, and compute source/selected-block SHA256 digests. Do not invoke `ssh`, a shell, `Match exec`, or `Include`.

- [ ] **Step 4: Restrict `SshCommandAliasDiscovery` to diagnostics**

Keep its current public `inspect(alias)` behavior for explicit import output, but remove documentation claiming it is an authority and ensure production bootstrap no longer passes it to repository observation or migration endpoint proof.

- [ ] **Step 5: Run focused GREEN and compatibility tests**

Run:

```bash
uv run pytest -q tests/test_ssh_config_alias.py tests/test_auth_import_adapters.py
```

Expected: PASS, with diagnostic `ssh -G` tests still green.

- [ ] **Step 6: Commit**

Run Ruff/mypy over changed files, then:

```bash
git add src/repoforge/ports/git_remote_identity.py src/repoforge/adapters/git/remote_identity.py src/repoforge/adapters/git/ssh_alias_discovery.py tests/test_ssh_config_alias.py tests/test_auth_import_adapters.py
git commit -m "feat(identity): parse SSH aliases without OpenSSH evaluation"
```

---

### Task 3: Bind key material by fingerprint and materialize it without a path race

**Files:**
- Create: `src/repoforge/adapters/git/ssh_key_identity.py`
- Modify: `src/repoforge/ports/git_remote_identity.py`
- Create: `tests/test_ssh_key_identity.py`
- Modify: `src/repoforge/domain/errors.py`

**Interfaces:**
- Produces:
  ```python
  class SshKeyInspector(Protocol):
      def inspect(self, identity_file: str, *, observed_at: str) -> SshKeyProof:
          raise NotImplementedError

  class SshIdentityMaterial(Protocol):
      @property
      def path(self) -> Path:
          raise NotImplementedError

      def close(self) -> None:
          raise NotImplementedError

  class SshIdentityMaterialProvider(Protocol):
      def open_verified(self, expected: SshKeyProof) -> SshIdentityMaterial:
          raise NotImplementedError

  class FileSshKeyIdentity:
      def inspect(self, identity_file: str, *, observed_at: str) -> SshKeyProof:
          raise NotImplementedError

      def open_verified(self, expected: SshKeyProof) -> SshIdentityMaterial:
          raise NotImplementedError
  ```

- [ ] **Step 1: Write failing key proof tests**

Use disposable synthetic private-key fixtures generated inside `tmp_path` or a recording fingerprint executor; never persist real operator key material. Prove:

```python
def test_key_inspection_rejects_symlink_and_group_writable_file(tmp_path: Path) -> None:
    target = _write_private_key(tmp_path / "target", mode=0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    with pytest.raises(RepoForgeError):
        _identity(tmp_path).inspect(str(alias), observed_at=NOW)
    target.chmod(0o660)
    with pytest.raises(RepoForgeError):
        _identity(tmp_path).inspect(str(target), observed_at=NOW)

def test_replacing_the_key_at_the_same_path_fails_open_verified(tmp_path: Path) -> None:
    path = _write_private_key(tmp_path / "id_work", mode=0o600, body=KEY_A)
    identity = _identity(tmp_path)
    proof = identity.inspect(str(path), observed_at=NOW)
    path.write_text(KEY_B, encoding="utf-8")
    with pytest.raises(RepoForgeError) as failure:
        identity.open_verified(proof)
    assert failure.value.code is ErrorCode.GIT_TRANSPORT_KEY_MISMATCH

def test_materialization_copies_from_the_verified_open_descriptor(tmp_path: Path) -> None:
    path = _write_private_key(tmp_path / "id_work", mode=0o600, body=KEY_A)
    identity = _identity(tmp_path)
    proof = identity.inspect(str(path), observed_at=NOW)
    material = identity.open_verified(proof)
    try:
        assert material.path != path
        assert material.path.stat().st_mode & 0o777 == 0o600
    finally:
        material.close()
    assert not material.path.exists()

def test_material_cleanup_runs_when_the_ssh_child_raises(tmp_path: Path) -> None:
    material = _material(tmp_path)
    with pytest.raises(RuntimeError, match="child failed"):
        with material:
            raise RuntimeError("child failed")
    assert not material.path.exists()
```

- [ ] **Step 2: Run key tests RED**

Run:

```bash
uv run pytest -q tests/test_ssh_key_identity.py
```

Expected: import failure for `FileSshKeyIdentity`.

- [ ] **Step 3: Implement no-follow inspection and fingerprinting**

Use `os.open(path, os.O_RDONLY | os.O_NOFOLLOW)`, `os.fstat()`, effective UID ownership, regular-file requirement, and reject group/world writable modes. Derive the public fingerprint through a bounded `ssh-keygen -y -f material.path` followed by `ssh-keygen -lf -E sha256 -`, or an equivalent adapter that never returns the public key body beyond the local method.

- [ ] **Step 4: Implement operation-scoped materialization**

Copy from the already-open descriptor into a `0600` file under the operation workspace/state temp root, `fsync`, pass only that temporary path to SSH, and guarantee close/unlink in `__exit__`/`close()`. Do not claim filesystem zeroisation.

- [ ] **Step 5: Run GREEN and secret-safety assertions**

Run:

```bash
uv run pytest -q tests/test_ssh_key_identity.py
```

Expected: PASS; captured argv/environment/errors contain no fixture private-key contents.

- [ ] **Step 6: Commit**

Run Ruff/mypy, then:

```bash
git add src/repoforge/adapters/git/ssh_key_identity.py src/repoforge/ports/git_remote_identity.py src/repoforge/domain/errors.py tests/test_ssh_key_identity.py
git commit -m "feat(identity): bind SSH keys by verified fingerprint"
```

---

### Task 4: Make the selected provider host authoritative and prove the SSH principal

**Files:**
- Modify: `src/repoforge/ports/auth_inspection.py`
- Modify: `src/repoforge/adapters/github/repository_observation.py`
- Create: `src/repoforge/adapters/github/ssh_principal.py`
- Modify: `src/repoforge/ports/git_remote_identity.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_auth_repository_observation.py`
- Create: `tests/test_github_ssh_principal.py`
- Modify: `tests/test_auth_cli.py`

**Interfaces:**
- `RepositoryObservationTarget` becomes provider-owned only:
  ```python
  @dataclass(frozen=True, slots=True)
  class RepositoryObservationTarget:
      provider: RepositoryProvider
      provider_host: str
      owner: str
      repository: str
  ```
- Observer surface becomes:
  ```python
  def target(
      self,
      repo: RepositoryConfig,
      *,
      expected_provider_host: str,
  ) -> RepositoryObservationTarget:
      raise NotImplementedError

  def observe(
      self,
      repo: RepositoryConfig,
      *,
      expected_provider_host: str,
      config_revision: str,
      context: ProcessAuthContext,
  ) -> RepositoryIdentityObservation:
      raise NotImplementedError
  ```
- Principal verifier:
  ```python
  class SshPrincipalVerifier(Protocol):
      def verify(
          self,
          *,
          provider_host: str,
          expected_login: str,
          expected_actor_id: str,
          key: SshKeyProof,
          observed_at: str,
      ) -> SshPrincipalProof:
          raise NotImplementedError
  ```

- [ ] **Step 1: Write RED observation tests**

Replace the PR #365 alias tests with provider-authority tests:

```python
def test_alias_cannot_choose_the_api_host(tmp_path: Path) -> None:
    observer = _observer_with_remote("git@github-work:acme/demo.git")
    with pytest.raises(RepoForgeError) as failure:
        observer.observe(
            _repo(tmp_path),
            expected_provider_host="github.com",
            config_revision=_SHA,
            context=_selected_context(),
        )
    assert failure.value.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH
    assert no_gh_api_call_was_made()


def test_canonical_enterprise_host_comes_from_the_selected_profile(tmp_path: Path) -> None:
    observed = observer.observe(
        _repo(tmp_path),
        expected_provider_host="github.acme-corp.net",
        config_revision=_SHA,
        context=_selected_context(),
    )
    assert gh_api_hostname() == "github.acme-corp.net"
```

The observer should parse raw remote owner/repository but must not evaluate aliases. Alias resolution happens in migration/endpoint resolution before the API call.

- [ ] **Step 2: Write RED GitHub principal tests**

Prove accepted bounded response `Hi matw-ngo! You've successfully authenticated, but GitHub does not provide shell access.`, wrong login, malformed response, deploy-key/repository-style principal, timeout, and non-GitHub host behavior. Expected wrong-login error: `GIT_TRANSPORT_IDENTITY_MISMATCH` or the new typed principal mismatch code.

- [ ] **Step 3: Run RED selectors**

Run:

```bash
uv run pytest -q tests/test_auth_repository_observation.py tests/test_github_ssh_principal.py tests/test_auth_cli.py -k "provider_host or alias or principal or production_auth_dependencies"
```

Expected: signature failures and missing principal verifier.

- [ ] **Step 4: Implement provider-authoritative observation**

Validate `expected_provider_host` before any command. Require canonical HTTPS/SSH remote host equality when no alias endpoint proof is supplied; do not call `ssh -G`. Call `gh api --hostname expected_provider_host` only after the transport resolver has proved raw host equivalence.

- [ ] **Step 5: Implement bounded principal probe**

Use operation-scoped verified material, `ssh -T -F /dev/null -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -i material.path git@provider_host`, capture bounded output, accept the provider's no-shell response even when SSH exits with GitHub's expected non-zero code, and compare the observed login exactly.

- [ ] **Step 6: Update bootstrap composition and run GREEN**

`build_auth_command_dependencies()` and managed runtime composition must create the remote parser, constrained alias resolver, key identity adapter, and principal verifier. Pass expected host from `StoredGhAccountSpec.host` or the explicit migration host; never use the alias as API authority.

Run focused tests, Ruff, mypy, then commit:

```bash
git add src/repoforge/ports/auth_inspection.py src/repoforge/adapters/github/repository_observation.py src/repoforge/adapters/github/ssh_principal.py src/repoforge/ports/git_remote_identity.py src/repoforge/bootstrap.py tests/test_auth_repository_observation.py tests/test_github_ssh_principal.py tests/test_auth_cli.py
git commit -m "feat(identity): bind SSH principals to selected GitHub actors"
```

---

### Task 5: Gather one migration endpoint snapshot and remove SSH-to-HTTPS fallback

**Files:**
- Modify: `src/repoforge/application/auth_migration.py`
- Modify: `src/repoforge/domain/auth_migration.py`
- Modify: `src/repoforge/application/configuration/source.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_auth_migration.py`
- Modify: `tests/test_auth_profile_config.py`

**Interfaces:**
- Replace separate observation/SSH candidate gathering with:
  ```python
  class ResolveRepositoryEndpoint(Protocol):
      def __call__(
          self,
          repo_id: str,
          *,
          provider_host: str,
          login: str,
          expected_actor_id: str,
      ) -> tuple[RepositoryIdentityObservation, ParsedGitRemote, ReviewedSshEndpoint | None]:
          raise NotImplementedError
  ```
- `_Gathered` stores `remote: ParsedGitRemote` and `ssh_endpoint: ReviewedSshEndpoint | None`; it no longer stores `SshAliasCandidate`.
- `PIN_TRANSPORT` change attributes include transport kind, raw/canonical host, key fingerprint, principal login, endpoint proof digest, and no private material.

- [ ] **Step 1: Write RED migration tests**

Add:

```python
def test_ssh_remote_with_unresolved_alias_is_blocking_not_https(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        accounts=Accounts((_personal(login="matw-ngo"),)),
        endpoint_error=RepoForgeError(
            "unsafe SSH config",
            code=ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
        ),
    )
    plan = service.inspect(repo_id="demo", login="matw-ngo")
    assert plan.ready is False
    assert not any(dict(change.attributes).get("transport_kind") == "https" for change in plan.changes)


def test_inspect_and_apply_bind_the_same_endpoint_proof(tmp_path: Path) -> None:
    plan = service.inspect(repo_id="demo", login="matw-ngo")
    endpoint.rotate_key_fingerprint()
    with pytest.raises(RepoForgeError) as failure:
        service.apply(
            repo_id="demo",
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            actor="operator:test",
            login="matw-ngo",
        )
    assert failure.value.code is ErrorCode.CONFIG_STALE


def test_migrated_profile_persists_structured_endpoint_proof(tmp_path: Path) -> None:
    service, store, endpoint = _service_with_endpoint(tmp_path, login="matw-ngo")
    plan = service.inspect(repo_id="demo", login="matw-ngo")
    service.apply(
        repo_id="demo",
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        actor="operator:test",
        login="matw-ngo",
    )
    parsed = parse_source(store.read_source_text())
    assert parsed.auth_profiles[0].ssh_endpoint.proof_digest == endpoint.proof_digest
```

- [ ] **Step 2: Run RED migration tests**

Run:

```bash
uv run pytest -q tests/test_auth_migration.py tests/test_auth_profile_config.py -k "ssh or endpoint or fallback or proof"
```

Expected: failures showing current INFO+HTTPS fallback and path-derived fingerprint behavior.

- [ ] **Step 3: Refactor gathering around one endpoint resolver**

Select/narrow the account first, derive provider host from the account, resolve the remote and endpoint once, perform API observation only after endpoint/provider equality, and build findings/changes from that immutable result. Remove `observation.transport_alias`, `_ssh_candidate()`, and accepted-host alias logic from `_remote_findings()`; compare the parsed remote and reviewed endpoint instead.

- [ ] **Step 4: Persist endpoint proof and real key fingerprint**

For SSH profiles set `credential_fingerprint` to a deterministic digest containing the public-key fingerprint, provider host, and endpoint proof version. Do not derive it from the path. For HTTPS preserve the existing named-account token-reference fingerprint.

- [ ] **Step 5: Run full migration GREEN**

Run:

```bash
uv run pytest -q tests/test_auth_migration.py tests/test_auth_profile_config.py tests/test_auth_cli.py
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run Ruff/mypy, then:

```bash
git add src/repoforge/application/auth_migration.py src/repoforge/domain/auth_migration.py src/repoforge/application/configuration/source.py src/repoforge/bootstrap.py tests/test_auth_migration.py tests/test_auth_profile_config.py
git commit -m "fix(identity): make SSH migration proof coherent and fail closed"
```

---

### Task 6: Canonicalize Git execution and revalidate endpoint proof before publication effects

**Files:**
- Modify: `src/repoforge/adapters/git/transport.py`
- Modify: `src/repoforge/domain/git_transport_identity.py`
- Modify: `src/repoforge/ports/git_remote_identity.py`
- Modify: `src/repoforge/adapters/publication.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `tests/test_git_transport_identity.py`
- Modify: `tests/test_publication_adapter.py`
- Modify: `tests/test_service_tools.py`

**Interfaces:**
- Add:
  ```python
  class GitTransportEndpointRevalidator(Protocol):
      def revalidate(
          self,
          *,
          cwd: Path,
          raw_remote_url: str,
          expected: ReviewedSshEndpoint,
      ) -> ReviewedSshEndpoint:
          raise NotImplementedError
  ```
- `GitTransportRouter.__init__` receives `endpoint_revalidator` and `identity_materials`.
- For SSH, `_run()` replaces argv remote argument index `2` with `endpoint.canonical_url()` after revalidation while evidence retains the raw URL digest.

- [ ] **Step 1: Write RED transport tests for the real failure from PR #365**

```python
def test_aliased_remote_executes_canonical_url_with_reviewed_key() -> None:
    router.ls_remote(
        Path("/repo"),
        "git@github-work:cicdata-io/portal-spa.git",
        "refs/heads/main",
        _ssh_spec(endpoint=_portal_endpoint()),
        _context(),
    )
    call = executor.calls[0]
    assert call["argv"][2] == "ssh://git@github.com:22/cicdata-io/portal-spa.git"
    assert "github-work" not in call["argv"][2]
    assert "-F /dev/null" in call["environment"]["GIT_SSH_COMMAND"]
    assert "id_rsa_work" not in call["environment"]["GIT_SSH_COMMAND"]


def test_legacy_path_only_ssh_profile_is_migration_required_before_network() -> None:
    with pytest.raises(RepoForgeError) as failure:
        router.fetch(
            Path("/repo"),
            "git@github-work:cicdata-io/portal-spa.git",
            "+refs/heads/main:refs/remotes/origin/main",
            legacy_spec,
            _context(),
        )
    assert failure.value.code is ErrorCode.GIT_TRANSPORT_MIGRATION_REQUIRED
    assert executor.calls == []
```

Also test changed raw remote, alias block, canonical host, owner/repository, key fingerprint, principal proof expiry, and revalidator failure before command execution.

- [ ] **Step 2: Write RED publication tests**

Prove publication topology still records/digests the raw fetch/push URL, while `transport.ls_remote()` receives and executes through the reviewed endpoint. Reconciliation must use the same revalidation path.

- [ ] **Step 3: Run RED selectors**

Run:

```bash
uv run pytest -q tests/test_git_transport_identity.py tests/test_publication_adapter.py tests/test_service_tools.py -k "alias or canonical or endpoint or legacy or transport"
```

Expected: current `GIT_TRANSPORT_HOST_MISMATCH` for `github-work` and missing endpoint dependencies.

- [ ] **Step 4: Implement canonical SSH execution**

For HTTPS retain current raw URL behavior. For SSH require `spec.ssh_endpoint`, revalidate it, open operation-scoped material, create `GIT_SSH_COMMAND` with the material path and explicit host/user/port policy, replace only the subprocess URL with `canonical_url()`, and close material in `finally`. `GitTransportEvidence.remote_url_digest` remains the digest of the original raw remote; add `canonical_endpoint_digest` to safe evidence.

- [ ] **Step 5: Integrate publication and production bootstrap**

Publication continues to derive exact topology and stable repository IDs from the live raw URLs. It passes raw URL plus `GitTransportSpec` to the router; the router owns endpoint revalidation/canonical execution. Compose real revalidator/material provider in `build_application()` so service tests require no adapter override.

- [ ] **Step 6: Run GREEN, affected tests, and commit**

Run:

```bash
uv run pytest -q tests/test_git_transport_identity.py tests/test_publication_adapter.py tests/test_service_tools.py
uv run python scripts/select_affected_tests.py --run
```

Then Ruff/mypy and:

```bash
git add src/repoforge/adapters/git/transport.py src/repoforge/domain/git_transport_identity.py src/repoforge/ports/git_remote_identity.py src/repoforge/adapters/publication.py src/repoforge/bootstrap.py tests/test_git_transport_identity.py tests/test_publication_adapter.py tests/test_service_tools.py
git commit -m "fix(identity): execute aliased SSH remotes through canonical proofs"
```

---

### Task 7: Legacy UX, documentation, leak scans, and end-to-end acceptance fixture

**Files:**
- Modify: `src/repoforge/application/auth_ux.py`
- Modify: `src/repoforge/domain/errors.py`
- Modify: `src/repoforge/bootstrap.py`
- Modify: `docs/development/REPOSITORY_IDENTITY.md`
- Modify: `tests/test_auth_cli.py`
- Modify: `tests/test_auth_profile_config.py`
- Modify: `tests/test_repository_identity_fault_matrix.py`
- Create: `tests/test_ssh_alias_end_to_end.py`
- Modify: `scripts/verify-production-gate.py` or the maintained identity inventory file only if the new adapters require explicit registration.

**Interfaces:**
- `profile inspect`, `whoami`, and `doctor` report:
  ```json
  {
    "transport_kind": "ssh",
    "ssh_endpoint_status": "verified",
    "ssh_raw_host": "github-work",
    "ssh_canonical_host": "github.com",
    "ssh_key_fingerprint": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "ssh_principal_login": "matw-ngo",
    "ssh_proof_revision": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "blocking": []
  }
  ```
- Legacy profile reports `ssh_endpoint_status="migration_required"` and the safe recovery command `rf auth migrate inspect portal-spa --login matw-ngo`.

- [ ] **Step 1: Write RED UX and end-to-end tests**

Build a temporary repository with raw remote `git@github-work:cicdata-io/portal-spa.git`, temporary constrained SSH config, disposable key fixture, fake bounded GitHub API/principal subprocess responses, real production composition, migration inspect/apply/reload, auth resolve/bind/whoami/doctor, and transport `ls-remote`/push command capture. Assert the raw remote remains unchanged and the executed URL is canonical.

Add a canary private-key body and assert it is absent from serialized config, plans, errors, command captures, receipts, logs, and snapshots.

- [ ] **Step 2: Run RED acceptance tests**

Run:

```bash
uv run pytest -q tests/test_ssh_alias_end_to_end.py tests/test_repository_identity_fault_matrix.py tests/test_auth_cli.py
```

Expected: failures until UX/composition/inventory are complete.

- [ ] **Step 3: Implement UX and typed recovery**

Expose only safe endpoint proof metadata. Add typed migration-required and proof-stale recovery text. Ensure read commands do not require or consume identity selector fields and write paths remain fail closed.

- [ ] **Step 4: Update documentation and maintained inventories**

Document:

```text
selected profile host → constrained alias proof → key fingerprint → principal proof
→ stable repository ID → reviewed endpoint → canonical Git execution
```

Explain that `source_ssh_alias`/`ssh_identity_file` alone are legacy metadata, `ssh -G` is diagnostic-only, and HTTPS conversion is explicit rather than fallback.

- [ ] **Step 5: Run complete verification on exact tree**

Run in order:

```bash
uv run pytest -q tests/test_git_remote_identity.py tests/test_ssh_config_alias.py tests/test_ssh_key_identity.py tests/test_github_ssh_principal.py tests/test_auth_repository_observation.py tests/test_auth_migration.py tests/test_git_transport_identity.py tests/test_publication_adapter.py tests/test_auth_cli.py tests/test_service_tools.py tests/test_ssh_alias_end_to_end.py
uv run python scripts/select_affected_tests.py --full --run
make lint
make typecheck
make build
make production-check
```

If the combined production target times out without a test failure, split and prove each constituent gate exactly as the repository's production workflow does; do not claim the combined command passed.

- [ ] **Step 6: Commit final implementation slice**

```bash
git add src/repoforge/application/auth_ux.py src/repoforge/domain/errors.py src/repoforge/bootstrap.py docs/development/REPOSITORY_IDENTITY.md tests/test_auth_cli.py tests/test_auth_profile_config.py tests/test_repository_identity_fault_matrix.py tests/test_ssh_alias_end_to_end.py scripts/verify-production-gate.py
git commit -m "test(identity): prove SSH alias identity end to end"
```

- [ ] **Step 7: Review, push, and open draft PR**

Review the complete diff against `docs/superpowers/specs/2026-07-31-ssh-transport-identity-proof-design.md`, push with an exact remote-head lock, and open a draft PR that states:

- PR #365's migration-only alias handling was incomplete;
- provider host authority now comes from the selected profile;
- normal authority paths no longer invoke `ssh -G`;
- key/principal proof and canonical execution are revalidated before effects;
- no repository remote or operator SSH config is modified;
- live `portal-spa` acceptance remains an operator step after merge/activation unless a non-mutating live preflight is available.

---

## Plan Self-Review Mapping

- Stable identity/transport separation: Tasks 1 and 4.
- Safe constrained alias parsing and effective-user paths: Task 2.
- Key fingerprint, no-follow proof, and path-race closure: Task 3.
- Provider-host authority and SSH principal binding: Task 4.
- One inspect/apply proof and no SSH→HTTPS fallback: Task 5.
- Canonical transport/publication execution: Task 6.
- Legacy compatibility, UX, docs, leak scans, production composition, and end-to-end acceptance: Task 7.

The implementation will use **Inline Execution** with `superpowers:executing-plans` because the user has explicitly preferred main-agent-only work over subagent dispatch in this project.
