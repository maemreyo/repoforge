# RepoForge tool reference

RepoForge đăng ký 27 MCP tools. Mỗi tool làm một nhiệm vụ rõ ràng; read và write được tách riêng.

## Repository

| Tool | Hành vi |
|---|---|
| `repo_list` | Danh sách repo, profile, base/branch policy, PR defaults và change limits. |
| `repo_status` | Git status, remotes và `gh auth status`. |
| `repo_context` | Manifest, scripts, engines, root files và preview instruction files. |
| `repo_recent_commits` | Commit history local, tối đa 100 commit. |
| `repo_issue_read` | Issue GitHub qua `gh`, output được giới hạn. |
| `repo_pr_read` | PR GitHub, files/commits/checks/reviews qua `gh`. |

## Workspace lifecycle

| Tool | Hành vi |
|---|---|
| `workspace_create` | Tạo worktree và branch `ai/*` duy nhất từ base allowlist. |
| `workspace_list` | Liệt kê workspace đang được registry quản lý. |
| `workspace_status` | HEAD, branch, status, fingerprint, verification và change metrics. |
| `workspace_remove` | Xóa clean local worktree; không xóa remote branch. |

## Read/search/edit

| Tool | Hành vi |
|---|---|
| `workspace_tree` | Danh sách tracked/untracked paths hợp lệ. |
| `workspace_read_file` | Đọc bounded UTF-8 lines và trả SHA-256. |
| `workspace_read_files` | Batch read tối đa `max_batch_files`. |
| `workspace_search` | Literal `git grep`, có optional path glob. |
| `workspace_write_file` | Tạo/replace full UTF-8 file bằng optimistic SHA locking. |
| `workspace_replace_text` | Exact replacement với SHA và expected occurrence count. |
| `workspace_apply_patch` | Unified patch với expected HEAD và workspace fingerprint. |
| `workspace_restore_paths` | Restore selected tracked paths hoặc xóa selected untracked files. |
| `workspace_diff` | Diff, stat, untracked patch và change metrics. |

## Verification/publication

| Tool | Hành vi |
|---|---|
| `workspace_run_profile` | Chạy profile allowlist; profile có thể không phải verification. |
| `workspace_verify` | Chạy default hoặc explicit verification profile và lưu receipt. |
| `workspace_commit` | Commit đúng tree đã verify; enforce path và change budget. |
| `workspace_push` | Non-force push branch workspace và ghi pushed SHA. |
| `workspace_create_draft_pr` | Tạo draft PR, hỗ trợ labels/reviewers/no-maintainer-edit. |
| `workspace_update_draft_pr` | Sửa title/body của PR hiện có, không đổi draft state. |
| `workspace_pr_status` | Draft state, mergeability, review decision và check rollup. |
| `workspace_pr_checks` | CI buckets `pass/fail/pending/skipping/cancel`, optional required only. |

## Cố ý không có

- arbitrary shell / arbitrary filesystem;
- merge hoặc mark-ready;
- force-push;
- ghi protected branch;
- secrets, branch protection, repository admin, releases;
- GitHub Actions workflow modification.
