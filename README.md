# RepoForge

**RepoForge** là MCP server local dành cho ChatGPT web: đọc và sửa repository trong Git worktree
cô lập, chạy verification profile được allowlist, commit, push nhánh `ai/*`, rồi tạo draft pull
request bằng GitHub CLI `gh`.

RepoForge không expose terminal tổng quát. Không có tool merge PR, force-push, ghi trực tiếp vào
protected branch, sửa secrets hoặc GitHub Actions workflow.

## Vì sao tên RepoForge?

Tên ngắn, dễ gọi trong prompt và mô tả đúng workflow: tạo một workspace an toàn để “forge” thay
đổi code thành một draft PR. Package Python là `repoforge-mcp`; CLI chính là `repoforge`, alias
ngắn là `rf`; tên Plugin nên dùng `RepoForge`.

## Kiến trúc

```text
ChatGPT web
  -> Secure MCP Tunnel
  -> RepoForge MCP (stdio trên máy Mac)
  -> git + gh + command profiles
  -> isolated worktree -> ai/* branch -> draft PR
```

## DevExperience trong v2

- `rf init`: tự nhận diện package manager, Makefile/scripts, base branch và tạo config.
- `rf scan-repos`: scan an toàn các root local được chỉ định và preview multi-repo config.
- `rf inspect-repo`: preview ecosystem, instruction files và profile được detect.
- `rf doctor --fix`: kiểm tra executable, `gh` auth, remote/base, version Node/pnpm, profile và
  quyền ghi state/workspace; có thể chạy `gh auth setup-git`.
- `rf smoke-test`: tạo rồi xóa worktree thật mà không sửa code.
- `rf tunnel-command`: sinh chính xác lệnh cấu hình Secure MCP Tunnel.
- Config riêng cho Work Frontier và RepoForge nằm tại `config.work-frontier.toml` và `config.repoforge.toml`.
- `uv.lock` và `uv sync --extra dev` giúp môi trường phát triển lặp lại được.
- 27 tool nhỏ, tách read/write, có annotations và structured output.
- Batch read, repository context, default verification, restore path, change budget, PR labels /
  reviewers, update draft PR và CI check buckets.
- Audit log local, optimistic SHA locking, workspace fingerprint và verification receipt.

## Yêu cầu

- macOS hoặc Linux;
- Python 3.10+;
- Git;
- GitHub CLI `gh`, đã đăng nhập bằng `gh auth login`;
- `tunnel-client` nếu kết nối ChatGPT web qua Secure MCP Tunnel;
- `uv` được khuyến nghị, nhưng bootstrap có fallback sang `venv + pip`.

## Cài nhanh cho Work Frontier

```bash
unzip repoforge-2.0.1.zip
cd repoforge
./scripts/bootstrap-macos.sh
```

Bootstrap mặc định dùng repository:

```text
/Users/trung.ngo/Documents/zaob-dev/work-frontier
```

Config được tạo tại:

```text
~/.config/repoforge/config.toml
```

Sau đó:

```bash
gh auth login
gh auth setup-git
./.venv/bin/rf doctor --fix
./.venv/bin/rf smoke-test --repo-id work-frontier
```

Nếu config đã tồn tại và muốn dùng bản đã chuẩn bị sẵn:

```bash
mkdir -p ~/.config/repoforge
cp config.work-frontier.toml ~/.config/repoforge/config.toml
```

## Scan repository và tự tạo multi-repo config

Preview toàn bộ repository dưới một root local, không sửa config:

```bash
rf scan-repos /Users/trung.ngo/Documents/zaob-dev --max-depth 2
```

Sau khi review command đã detect, tạo một config chứa nhiều repository:

```bash
rf --config ~/.config/repoforge/config.toml init \
  --scan-root /Users/trung.ngo/Documents/zaob-dev \
  --max-depth 2
```

Scanner không follow symlink, bỏ qua dependency/build/cache directories, giới hạn depth/count và
không tạo worktree, branch hay PR. Xem [`docs/REPOSITORY_DISCOVERY.md`](docs/REPOSITORY_DISCOVERY.md).

## Profile của Work Frontier

Config đã review dùng các Make target canonical của repository:

```text
setup         -> make bootstrap
fix           -> make fix
quick         -> make check
test          -> make test
preflight     -> make check-preflight
architecture  -> make check-architecture
contracts     -> make check-contracts
registry      -> make check-harness-registry
full          -> make verify
recertify     -> make recertify-foundation
```

`full` cần Docker vì `make verify` chạy PostgreSQL migration và MinIO storage smokes. Review
`config.work-frontier.toml` trước lần chạy đầu tiên, đặc biệt là profile có thể sửa file như `fix`.

## Profile của RepoForge

`config.repoforge.toml` dùng chính Makefile của RepoForge:

```text
setup  -> make setup
quick  -> make lint + make typecheck
test   -> make test
build  -> make build
full   -> make check
```

## Chạy local MCP

```bash
./.venv/bin/rf serve
```

`serve` dùng stdio; stdout được dành riêng cho MCP JSON-RPC.

## Chạy Secure MCP Tunnel

```bash
export CONTROL_PLANE_API_KEY="sk-..."
export TUNNEL_ID="tunnel_..."
./scripts/run-tunnel.sh
```

Trong ChatGPT Plugin:

```text
Name: RepoForge
Connection: Tunnel
Authentication: No Authentication
```

Xem hướng dẫn chi tiết tại [`docs/CHATGPT_SETUP.md`](docs/CHATGPT_SETUP.md).

## Workflow được khuyến nghị

1. `repo_list`, `repo_status`, `repo_context`.
2. `workspace_create` từ `main`.
3. Đọc/search trước khi sửa.
4. Sửa bằng exact replacement hoặc patch nhỏ.
5. Xem `workspace_diff` và change metrics.
6. `workspace_verify` chạy profile mặc định `full`.
7. Dừng để người dùng review.
8. Sau khi được duyệt: commit, push, tạo draft PR.
9. Dùng `workspace_pr_checks` để theo dõi CI.

## Các lớp bảo vệ

- Repository allowlist và path canonicalization.
- Worktree riêng, branch bắt buộc `ai/*`, protected branches bị từ chối.
- Denied paths mặc định: `.env`, keys, secret/credential patterns và `.github/workflows/**`.
- Không cho thay đổi symlink/submodule/gitlink.
- Không có arbitrary shell; command chỉ đến từ profile TOML.
- Write file dùng SHA-256; patch/restore dùng exact workspace fingerprint.
- Verification receipt bị vô hiệu nếu tree thay đổi sau test.
- Change budget giới hạn số file, số dòng diff và tổng bytes.
- Push luôn không force; PR luôn draft.
- Audit JSONL không lưu patch, file body, PR body hoặc toàn bộ environment.

## Development

```bash
uv sync --extra dev
./scripts/test-all.sh
```

Hoặc:

```bash
make check
```

Test suite hiện gồm unit, negative/security, local Git worktree integration, fake-`gh` PR lifecycle,
CLI/discovery và in-memory MCP protocol tests. Xem [`docs/TESTING.md`](docs/TESTING.md).

## Tài liệu

- [`docs/CHATGPT_SETUP.md`](docs/CHATGPT_SETUP.md)
- [`docs/TOOL_REFERENCE.md`](docs/TOOL_REFERENCE.md)
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)
- [`docs/TESTING.md`](docs/TESTING.md)
- [`docs/STARTER_PROMPTS.md`](docs/STARTER_PROMPTS.md)
- [`docs/PLUGIN_TEST_CASES.md`](docs/PLUGIN_TEST_CASES.md)
- [`docs/REPOSITORY_DISCOVERY.md`](docs/REPOSITORY_DISCOVERY.md)
- [`docs/FULL_FLOW_TESTING.md`](docs/FULL_FLOW_TESTING.md)
- [`SECURITY.md`](SECURITY.md)
