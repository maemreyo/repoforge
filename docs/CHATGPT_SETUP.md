# Kết nối RepoForge với ChatGPT bằng Secure MCP Tunnel

## 1. Cài source

```bash
unzip repoforge-2.0.0.zip
cd repoforge
./scripts/bootstrap-macos.sh
```

Repository mặc định đã được đặt thành:

```text
/Users/trung.ngo/Documents/zaob-dev/work-frontier
```

Config mặc định:

```text
~/.config/repoforge/config.toml
```

## 2. Kiểm tra local

```bash
gh auth login
gh auth setup-git
./.venv/bin/rf doctor --fix
./.venv/bin/rf smoke-test --repo-id work-frontier
```

`smoke-test` tạo worktree/branch local tạm thời rồi xóa chúng, không sửa file và không push.

Có thể kiểm tra raw MCP contract bằng:

```bash
./scripts/inspect-mcp.sh
```

## 3. Chạy tunnel

Đặt `tunnel-client` vào `PATH`, rồi chạy:

```bash
export CONTROL_PLANE_API_KEY="sk-..."
export TUNNEL_ID="tunnel_..."
./scripts/run-tunnel.sh
```

Script sẽ chạy RepoForge doctor, cấu hình profile tunnel, kiểm tra tunnel và giữ tunnel hoạt động.

Có thể xem lệnh trước khi chạy:

```bash
./.venv/bin/rf tunnel-command --tunnel-id tunnel_...
```

## 4. Tạo Plugin trong ChatGPT

Trong form **New Plugin**:

```text
Icon: plugin-icon.png
Name: RepoForge
Description: Safely inspect and modify allowlisted local Git repositories in isolated worktrees,
run predefined verification profiles, push AI branches, and create draft pull requests.
Connection: Tunnel
Available tunnels: chọn tunnel của bạn
Authentication: No Authentication
```

Tick xác nhận rủi ro rồi nhấn **Create**.

RepoForge không kết nối `api.githubcopilot.com`; nó gọi `gh` đã đăng nhập trên máy Mac, vì vậy
không đi qua GitHub Copilot OAuth/Dynamic Client Registration từng gây lỗi RFC 7591.

## 5. Golden prompt đầu tiên

```text
Use RepoForge.
Repository: work-frontier.

Do not change code yet.
1. Inspect repository status and context.
2. Read the relevant plan and architecture files.
3. Explain the implementation approach and likely verification profile.
4. Stop for approval.
```

Sau khi duyệt plan:

```text
Continue with RepoForge.
Create an isolated workspace from main, implement the approved plan in small changes, inspect the
final diff, run the default full verification profile, and stop before commit. Never merge,
force-push, or modify denied paths.
```

Sau khi duyệt diff:

```text
Commit the verified changes, push the ai/* branch, and create a draft PR. Then report the PR URL and
CI check summary. Do not mark the PR ready and do not merge it.
```

Xem thêm prompt tại `docs/STARTER_PROMPTS.md` và regression cases tại
`docs/PLUGIN_TEST_CASES.md`.
