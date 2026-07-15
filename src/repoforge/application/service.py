"""Compatibility facade delegating every public operation to one typed application use case."""

from __future__ import annotations

from typing import Any

from ..bootstrap import AdapterOverrides, Application, build_application
from ..config import AppConfig
from ..ports import (
    AuditSink,
    CommandExecutor,
    IdempotencyStore,
    LockManager,
    MetricsSink,
    OperationGate,
    WorkspaceStore,
)
from .dto import to_data
from .operations.cancel import OperationCancelCommand, OperationCancellationRequester
from .operations.list import OperationListCommand, OperationLister
from .operations.status import OperationStatusCommand, OperationStatusReader
from .repository.commit_read import (
    RepositoryCommitReadCommand,
    RepositoryCommitReader,
)
from .repository.compare import RepositoryCompareCommand, RepositoryComparer
from .repository.context import (
    RepositoryContextCommand,
    RepositoryContextReader,
)
from .repository.doctor import Doctor, DoctorCommand
from .repository.file_read import RepositoryFileReadCommand, RepositoryFileReader
from .repository.files_read import RepositoryFilesReadCommand, RepositoryFilesReader
from .repository.issue_graph import RepositoryIssueGraphCommand, RepositoryIssueGraphReader
from .repository.issue_next import RepositoryIssueNextCommand, RepositoryIssueNextReader
from .repository.issue_read import IssueReadCommand, IssueReader
from .repository.issue_spec import RepositoryIssueSpecCommand, RepositoryIssueSpecReader
from .repository.list import RepositoryListCommand, RepositoryLister
from .repository.pr_read import PullRequestReadCommand, PullRequestReader
from .repository.recent_commits import (
    RecentCommitsCommand,
    RecentCommitsReader,
)
from .repository.search import RepositorySearchCommand, RepositorySearcher
from .repository.status import (
    RepositoryStatusCommand,
    RepositoryStatusReader,
)
from .repository.tree import RepositoryTreeCommand, RepositoryTreeReader
from .workspace.apply_patch import (
    WorkspaceApplyPatchCommand,
    WorkspacePatchApplier,
)
from .workspace.assessment import WorkspaceAssessmentCommand, WorkspaceAssessmentReader
from .workspace.base_status import WorkspaceBaseStatusCommand, WorkspaceBaseStatusReader
from .workspace.commit import WorkspaceCommitCommand, WorkspaceCommitter
from .workspace.create import WorkspaceCreateCommand, WorkspaceCreator
from .workspace.create_draft_pr import (
    DraftPullRequestCreator,
    WorkspaceCreateDraftPrCommand,
)
from .workspace.diff import WorkspaceDiffCommand, WorkspaceDiffReader
from .workspace.file_read import (
    WorkspaceFileReadCommand,
    WorkspaceFileReader,
)
from .workspace.file_write import (
    WorkspaceFileWriteCommand,
    WorkspaceFileWriter,
)
from .workspace.files_read import (
    WorkspaceFilesReadCommand,
    WorkspaceFilesReader,
)
from .workspace.list import WorkspaceListCommand, WorkspaceLister
from .workspace.pr_check_details import (
    WorkspacePrCheckDetailsCommand,
    WorkspacePrCheckDetailsReader,
)
from .workspace.pr_checks import (
    WorkspacePrChecksCommand,
    WorkspacePrChecksReader,
)
from .workspace.pr_failure_evidence import (
    WorkspacePrFailureEvidenceCommand,
    WorkspacePrFailureEvidenceReader,
)
from .workspace.pr_status import (
    WorkspacePrStatusCommand,
    WorkspacePrStatusReader,
)
from .workspace.pr_watch import WorkspacePrWatchCommand
from .workspace.push import WorkspacePushCommand, WorkspacePusher
from .workspace.refresh import WorkspaceRefreshCommand, WorkspaceRefresher
from .workspace.refresh_preview import (
    WorkspaceRefreshPreviewCommand,
    WorkspaceRefreshPreviewer,
)
from .workspace.remove import WorkspaceRemoveCommand, WorkspaceRemover
from .workspace.replace_text import (
    TextEdit,
    WorkspaceReplaceTextCommand,
    WorkspaceTextReplacer,
)
from .workspace.restore_paths import (
    WorkspacePathsRestorer,
    WorkspaceRestorePathsCommand,
)
from .workspace.run_diagnostic import (
    WorkspaceDiagnosticRunner,
    WorkspaceRunDiagnosticCommand,
)
from .workspace.run_profile import (
    WorkspaceProfileRunner,
    WorkspaceRunProfileCommand,
)
from .workspace.search import WorkspaceSearchCommand, WorkspaceSearcher
from .workspace.status import WorkspaceStatusCommand, WorkspaceStatusReader
from .workspace.tree import WorkspaceTreeCommand, WorkspaceTreeReader
from .workspace.update_draft_pr import (
    DraftPullRequestUpdater,
    WorkspaceUpdateDraftPrCommand,
)
from .workspace.verify import WorkspaceVerifier, WorkspaceVerifyCommand


def _result(value: object) -> dict[str, Any]:
    data = to_data(value)
    if isinstance(data, dict) and set(data) == {"payload"} and isinstance(data["payload"], dict):
        return data["payload"]
    if (
        isinstance(data, dict)
        and data.get("payload") is not None
        and isinstance(data["payload"], dict)
    ):
        return data["payload"]
    if not isinstance(data, dict):
        raise TypeError("Application result must serialize to an object")
    data.pop("payload", None)
    return data


class CodingService:
    """Stable facade retained for MCP/CLI callers while application logic lives in use cases."""

    def __init__(
        self,
        config: AppConfig,
        *,
        runner: CommandExecutor | None = None,
        state: WorkspaceStore | None = None,
        audit: AuditSink | None = None,
        locks: LockManager | None = None,
        gate: OperationGate | None = None,
        metrics: MetricsSink | None = None,
        idempotency: IdempotencyStore | None = None,
        application: Application | None = None,
    ):
        self.application = application or build_application(
            config,
            overrides=AdapterOverrides(
                command=runner,
                store=state,
                audit=audit,
                locks=locks,
                gate=gate,
                metrics=metrics,
                idempotency=idempotency,
            ),
        )
        self.config = self.application.context.config
        self.runner = self.application.context.commands
        self.state = self.application.context.store
        self.audit = self.application.context.audit
        self.locks = self.application.context.locks
        self.gate = self.application.context.gate
        self.metrics = self.application.context.metrics
        self.idempotency = self.application.context.idempotency
        self.operations = self.application.operations
        ctx = self.application.context
        self._operation_status = OperationStatusReader(self.operations)
        self._operation_list = OperationLister(self.operations)
        self._operation_cancel = OperationCancellationRequester(self.operations)
        self._repo_list = RepositoryLister(ctx)
        self._repo_status = RepositoryStatusReader(ctx)
        self._repo_context = RepositoryContextReader(ctx)
        self._repo_commit = RepositoryCommitReader(ctx)
        self._repo_compare = RepositoryComparer(ctx)
        self._repo_tree = RepositoryTreeReader(ctx)
        self._repo_read = RepositoryFileReader(ctx)
        self._repo_reads = RepositoryFilesReader(ctx)
        self._repo_search = RepositorySearcher(ctx)
        self._recent = RecentCommitsReader(ctx)
        self._issue = IssueReader(ctx)
        self._issue_graph = RepositoryIssueGraphReader(ctx)
        self._issue_next = RepositoryIssueNextReader(ctx)
        self._issue_spec = RepositoryIssueSpecReader(ctx)
        self._repo_pr = PullRequestReader(ctx)
        self._create = WorkspaceCreator(ctx)
        self._list = WorkspaceLister(ctx)
        self._status = WorkspaceStatusReader(ctx)
        self._assessment = WorkspaceAssessmentReader(ctx)
        self._base_status = WorkspaceBaseStatusReader(ctx)
        self._tree = WorkspaceTreeReader(ctx)
        self._read = WorkspaceFileReader(ctx)
        self._reads = WorkspaceFilesReader(ctx)
        self._search = WorkspaceSearcher(ctx)
        self._write = WorkspaceFileWriter(ctx)
        self._replace = WorkspaceTextReplacer(ctx)
        self._patch = WorkspacePatchApplier(ctx)
        self._restore = WorkspacePathsRestorer(ctx)
        self._refresh_preview = WorkspaceRefreshPreviewer(ctx)
        self._refresh = WorkspaceRefresher(ctx)
        self._diff = WorkspaceDiffReader(ctx)
        self._profile = WorkspaceProfileRunner(
            ctx,
            operations=self.operations,
            background_tasks=self.application.background_tasks,
        )
        self._diagnostic = WorkspaceDiagnosticRunner(ctx)
        self._verify = WorkspaceVerifier(ctx)
        self._commit = WorkspaceCommitter(ctx)
        self._push = WorkspacePusher(ctx)
        self._create_pr = DraftPullRequestCreator(ctx)
        self._update_pr = DraftPullRequestUpdater(ctx)
        self._pr_status = WorkspacePrStatusReader(ctx)
        self._checks = WorkspacePrChecksReader(ctx)
        self._check_details = WorkspacePrCheckDetailsReader(ctx)
        self._failure_evidence = WorkspacePrFailureEvidenceReader(ctx)
        self._pr_watch = self.application.pr_check_watches
        self._remove = WorkspaceRemover(ctx)
        self._doctor = Doctor(ctx)

    def operation_status(self, operation_id: str) -> dict[str, Any]:
        return _result(self._operation_status.execute(OperationStatusCommand(operation_id)))

    def operation_list(
        self,
        scope: str | None = None,
        state: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._operation_list.execute(OperationListCommand(scope, state, limit, cursor))
        )

    def operation_cancel(
        self,
        operation_id: str,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        result = self._operation_cancel.execute(
            OperationCancelCommand(operation_id, expected_updated_at)
        )
        if result.cancellation_requested and result.operation.kind == "workspace_run_profile":
            self._profile.request_live_cancel(operation_id)
        return _result(result)

    def repo_list(self) -> dict[str, Any]:
        return _result(self._repo_list.execute(RepositoryListCommand()))

    def repo_status(self, repo_id: str) -> dict[str, Any]:
        return _result(self._repo_status.execute(RepositoryStatusCommand(repo_id)))

    def repo_context(self, repo_id: str) -> dict[str, Any]:
        return _result(self._repo_context.execute(RepositoryContextCommand(repo_id)))

    def repo_commit_read(
        self,
        repo_id: str,
        ref: str,
        max_files: int = 100,
        include_patch: bool = False,
    ) -> dict[str, Any]:
        return _result(
            self._repo_commit.execute(
                RepositoryCommitReadCommand(repo_id, ref, max_files, include_patch)
            )
        )

    def repo_compare(
        self,
        repo_id: str,
        base_ref: str,
        head_ref: str,
        path_glob: str | None = None,
        max_files: int = 100,
        include_patch: bool = False,
    ) -> dict[str, Any]:
        return _result(
            self._repo_compare.execute(
                RepositoryCompareCommand(
                    repo_id,
                    base_ref,
                    head_ref,
                    path_glob,
                    max_files,
                    include_patch,
                )
            )
        )

    def repo_tree(
        self,
        repo_id: str,
        ref: str | None = None,
        max_entries: int = 2000,
    ) -> dict[str, Any]:
        return _result(self._repo_tree.execute(RepositoryTreeCommand(repo_id, ref, max_entries)))

    def repo_read_file(
        self,
        repo_id: str,
        relative_path: str,
        ref: str | None = None,
        start_line: int = 1,
        end_line: int = 500,
    ) -> dict[str, Any]:
        return _result(
            self._repo_read.execute(
                RepositoryFileReadCommand(repo_id, relative_path, ref, start_line, end_line)
            )
        )

    def repo_read_files(
        self,
        repo_id: str,
        relative_paths: list[str],
        ref: str | None = None,
        start_line: int = 1,
        end_line: int = 500,
    ) -> dict[str, Any]:
        return _result(
            self._repo_reads.execute(
                RepositoryFilesReadCommand(repo_id, relative_paths, ref, start_line, end_line)
            )
        )

    def repo_search(
        self,
        repo_id: str,
        query: str,
        ref: str | None = None,
        path_glob: str | None = None,
        max_results: int = 200,
    ) -> dict[str, Any]:
        return _result(
            self._repo_search.execute(
                RepositorySearchCommand(repo_id, query, ref, path_glob, max_results)
            )
        )

    def repo_recent_commits(self, repo_id: str, limit: int = 20) -> dict[str, Any]:
        return _result(self._recent.execute(RecentCommitsCommand(repo_id, limit)))

    def repo_issue_read(self, repo_id: str, issue_number: int) -> dict[str, Any]:
        return _result(self._issue.execute(IssueReadCommand(repo_id, issue_number)))

    def repo_issue_graph(
        self,
        repo_id: str,
        root_issue: int | None = None,
        status: str | None = None,
        priority: str | None = None,
        initiative: int | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._issue_graph.execute(
                RepositoryIssueGraphCommand(repo_id, root_issue, status, priority, initiative)
            )
        )

    def repo_issue_next(
        self,
        repo_id: str,
        root_issue: int | None = None,
        limit: int = 1,
        p0_wip_limit: int = 2,
        p1_wip_limit: int = 3,
        p2_wip_limit: int = 4,
        p3_wip_limit: int = 4,
        initiative_wip_limit: int = 2,
    ) -> dict[str, Any]:
        return _result(
            self._issue_next.execute(
                RepositoryIssueNextCommand(
                    repo_id,
                    root_issue,
                    limit,
                    p0_wip_limit,
                    p1_wip_limit,
                    p2_wip_limit,
                    p3_wip_limit,
                    initiative_wip_limit,
                )
            )
        )

    def repo_issue_spec(self, repo_id: str, issue_number: int) -> dict[str, Any]:
        return _result(self._issue_spec.execute(RepositoryIssueSpecCommand(repo_id, issue_number)))

    def repo_pr_read(self, repo_id: str, pr_number: int) -> dict[str, Any]:
        return _result(self._repo_pr.execute(PullRequestReadCommand(repo_id, pr_number)))

    def workspace_create(
        self,
        repo_id: str,
        task_slug: str,
        base: str | None = None,
        idempotency_key: str | None = None,
        issue_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return _result(
            self._create.execute(
                WorkspaceCreateCommand(repo_id, task_slug, base, idempotency_key, issue_ids)
            )
        )

    def workspace_list(self) -> dict[str, Any]:
        return _result(self._list.execute(WorkspaceListCommand()))

    def workspace_status(self, workspace_id: str) -> dict[str, Any]:
        return _result(self._status.execute(WorkspaceStatusCommand(workspace_id)))

    def workspace_assessment(self, workspace_id: str) -> dict[str, Any]:
        return _result(self._assessment.execute(WorkspaceAssessmentCommand(workspace_id)))

    def workspace_base_status(self, workspace_id: str) -> dict[str, Any]:
        return _result(self._base_status.execute(WorkspaceBaseStatusCommand(workspace_id)))

    def workspace_tree(self, workspace_id: str, max_entries: int = 2000) -> dict[str, Any]:
        return _result(self._tree.execute(WorkspaceTreeCommand(workspace_id, max_entries)))

    def workspace_read_file(
        self,
        workspace_id: str,
        relative_path: str,
        start_line: int = 1,
        end_line: int = 500,
    ) -> dict[str, Any]:
        return _result(
            self._read.execute(
                WorkspaceFileReadCommand(workspace_id, relative_path, start_line, end_line)
            )
        )

    def workspace_read_files(
        self,
        workspace_id: str,
        relative_paths: list[str],
        start_line: int = 1,
        end_line: int = 500,
    ) -> dict[str, Any]:
        return _result(
            self._reads.execute(
                WorkspaceFilesReadCommand(workspace_id, relative_paths, start_line, end_line)
            )
        )

    def workspace_search(
        self,
        workspace_id: str,
        query: str,
        path_glob: str | None = None,
        max_results: int = 200,
    ) -> dict[str, Any]:
        return _result(
            self._search.execute(
                WorkspaceSearchCommand(workspace_id, query, path_glob, max_results)
            )
        )

    def workspace_write_file(
        self, workspace_id: str, relative_path: str, content: str, expected_sha256: str
    ) -> dict[str, Any]:
        return _result(
            self._write.execute(
                WorkspaceFileWriteCommand(workspace_id, relative_path, content, expected_sha256)
            )
        )

    def workspace_replace_text(
        self,
        workspace_id: str,
        relative_path: str,
        old_text: str | None = None,
        new_text: str | None = None,
        expected_sha256: str = "",
        expected_occurrences: int = 1,
        edits: list[TextEdit] | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._replace.execute(
                WorkspaceReplaceTextCommand(
                    workspace_id=workspace_id,
                    relative_path=relative_path,
                    expected_sha256=expected_sha256,
                    old_text=old_text,
                    new_text=new_text,
                    expected_occurrences=expected_occurrences,
                    edits=tuple(edits) if edits is not None else None,
                )
            )
        )

    def workspace_apply_patch(
        self,
        workspace_id: str,
        patch: str,
        expected_head_sha: str,
        expected_workspace_fingerprint: str,
    ) -> dict[str, Any]:
        return _result(
            self._patch.execute(
                WorkspaceApplyPatchCommand(
                    workspace_id,
                    patch,
                    expected_head_sha,
                    expected_workspace_fingerprint,
                )
            )
        )

    def workspace_restore_paths(
        self,
        workspace_id: str,
        relative_paths: list[str],
        expected_workspace_fingerprint: str,
    ) -> dict[str, Any]:
        return _result(
            self._restore.execute(
                WorkspaceRestorePathsCommand(
                    workspace_id, relative_paths, expected_workspace_fingerprint
                )
            )
        )

    def workspace_refresh_preview(
        self,
        workspace_id: str,
        expected_head_sha: str,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        return _result(
            self._refresh_preview.execute(
                WorkspaceRefreshPreviewCommand(
                    workspace_id,
                    expected_head_sha,
                    expected_fingerprint,
                )
            )
        )

    def workspace_refresh(
        self,
        workspace_id: str,
        preview_id: str,
        expected_head_sha: str,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        return _result(
            self._refresh.execute(
                WorkspaceRefreshCommand(
                    workspace_id,
                    preview_id,
                    expected_head_sha,
                    expected_fingerprint,
                )
            )
        )

    def workspace_diff(self, workspace_id: str, staged: bool = False) -> dict[str, Any]:
        return _result(self._diff.execute(WorkspaceDiffCommand(workspace_id, staged)))

    def workspace_run_profile(
        self, workspace_id: str, profile_name: str, background: bool = False
    ) -> dict[str, Any]:
        return _result(
            self._profile.execute(
                WorkspaceRunProfileCommand(workspace_id, profile_name, background)
            )
        )

    def workspace_run_diagnostic(
        self,
        workspace_id: str,
        diagnostic_id: str,
        selector: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._diagnostic.execute(
                WorkspaceRunDiagnosticCommand(
                    workspace_id,
                    diagnostic_id,
                    selector,
                    expected_fingerprint,
                )
            )
        )

    def workspace_verify(
        self, workspace_id: str, profile_name: str | None = None
    ) -> dict[str, Any]:
        return _result(self._verify.execute(WorkspaceVerifyCommand(workspace_id, profile_name)))

    def workspace_commit(self, workspace_id: str, message: str) -> dict[str, Any]:
        return _result(self._commit.execute(WorkspaceCommitCommand(workspace_id, message)))

    def workspace_push(
        self, workspace_id: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return _result(self._push.execute(WorkspacePushCommand(workspace_id, idempotency_key)))

    def workspace_create_draft_pr(
        self,
        workspace_id: str,
        title: str,
        body: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._create_pr.execute(
                WorkspaceCreateDraftPrCommand(workspace_id, title, body, idempotency_key)
            )
        )

    def workspace_update_draft_pr(
        self,
        workspace_id: str,
        title: str | None = None,
        body: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._update_pr.execute(
                WorkspaceUpdateDraftPrCommand(workspace_id, title, body, idempotency_key)
            )
        )

    def workspace_pr_status(self, workspace_id: str) -> dict[str, Any]:
        return _result(self._pr_status.execute(WorkspacePrStatusCommand(workspace_id)))

    def workspace_pr_checks(self, workspace_id: str, required_only: bool = False) -> dict[str, Any]:
        return _result(self._checks.execute(WorkspacePrChecksCommand(workspace_id, required_only)))

    def workspace_pr_watch(
        self,
        workspace_id: str,
        until: str = "all_completed",
        timeout_seconds: int = 900,
        include_failure_evidence: bool = True,
    ) -> dict[str, Any]:
        return _result(
            self._pr_watch.start(
                WorkspacePrWatchCommand(
                    workspace_id,
                    until,
                    timeout_seconds,
                    include_failure_evidence,
                )
            )
        )

    def workspace_pr_check_details(
        self,
        workspace_id: str,
        check_selector: str,
    ) -> dict[str, Any]:
        return _result(
            self._check_details.execute(
                WorkspacePrCheckDetailsCommand(workspace_id, check_selector)
            )
        )

    def workspace_pr_failure_evidence(
        self,
        workspace_id: str,
        check_selector: str,
        max_excerpt_lines: int = 80,
    ) -> dict[str, Any]:
        return _result(
            self._failure_evidence.execute(
                WorkspacePrFailureEvidenceCommand(
                    workspace_id,
                    check_selector,
                    max_excerpt_lines,
                )
            )
        )

    def workspace_remove(
        self, workspace_id: str, delete_local_branch: bool = False
    ) -> dict[str, Any]:
        return _result(
            self._remove.execute(WorkspaceRemoveCommand(workspace_id, delete_local_branch))
        )

    def doctor(self) -> dict[str, Any]:
        return _result(self._doctor.execute(DoctorCommand()))
