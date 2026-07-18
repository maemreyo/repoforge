"""Compatibility facade delegating every public operation to one typed application use case."""

from __future__ import annotations

from typing import Any

from ..bootstrap import AdapterOverrides, Application, build_application
from ..config import AppConfig
from ..domain.egress import EgressDestination, sanitize_egress_data
from ..domain.ticket_sync import TicketProjectOwnerType
from ..ports import (
    AuditSink,
    CommandExecutor,
    IdempotencyStore,
    LockManager,
    MetricsSink,
    OperationGate,
    TicketGraphGateway,
    TicketProjectGateway,
    WorkspaceStore,
)
from .dto import to_data
from .operations.cancel import OperationCancelCommand, OperationCancellationRequester
from .operations.composite import OperationCommand, OperationCoordinator
from .operations.list import OperationListCommand, OperationLister
from .operations.status import OperationStatusCommand, OperationStatusReader
from .read_batch import FileReadRequest
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
from .repository.family_v2 import (
    RepositoryHistoryV2,
    RepositoryHistoryV2Command,
    RepositoryIssueV2,
    RepositoryIssueV2Command,
    RepositoryListV2,
    RepositoryListV2Command,
    RepositoryPrReadV2,
    RepositoryPrReadV2Command,
    RepositoryTaskContextV2,
    RepositoryTaskContextV2Command,
)
from .repository.file_read import RepositoryFileReadCommand, RepositoryFileReader
from .repository.files_read import RepositoryFilesReadCommand, RepositoryFilesReader
from .repository.issue_graph import RepositoryIssueGraphCommand, RepositoryIssueGraphReader
from .repository.issue_next import RepositoryIssueNextCommand, RepositoryIssueNextReader
from .repository.issue_read import IssueReadCommand, IssueReader
from .repository.issue_spec import RepositoryIssueSpecCommand, RepositoryIssueSpecReader
from .repository.list import RepositoryListCommand, RepositoryLister
from .repository.pr_read import PullRequestReadCommand, PullRequestReader
from .repository.read import RepositoryReadCommand, RepositoryReader
from .repository.recent_commits import (
    RecentCommitsCommand,
    RecentCommitsReader,
)
from .repository.retrieval import (
    RepositoryRetrieval,
    RepositorySearchV2Command,
    RepositoryTreeV2Command,
)
from .repository.search import RepositorySearchCommand, RepositorySearcher
from .repository.status import (
    RepositoryStatusCommand,
    RepositoryStatusReader,
)
from .repository.task_context import RepoTaskContextCommand, RepoTaskContextReader
from .repository.tree import RepositoryTreeCommand, RepositoryTreeReader
from .retrieval import SearchMode
from .tickets.project_sync import (
    TicketProjectSyncCommand,
    TicketProjectSyncer,
)
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
from .workspace.edit import (
    FileEdit,
    WorkspaceEditCommand,
    WorkspaceEditor,
)
from .workspace.family_v2 import (
    WorkspaceChangedFormatterV2,
    WorkspaceCreateV2Command,
    WorkspaceCreatorV2,
    WorkspaceFormatChangedV2Command,
    WorkspaceListerV2,
    WorkspaceListV2Command,
    WorkspaceRemoverV2,
    WorkspaceRemoveV2Command,
    WorkspaceStatusV2,
    WorkspaceStatusV2Command,
)
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
from .workspace.format_changed import (
    WorkspaceChangedFormatter,
    WorkspaceFormatChangedCommand,
)
from .workspace.hygiene_status import (
    WorkspaceHygieneStatusCommand,
    WorkspaceHygieneStatusReader,
)
from .workspace.list import WorkspaceListCommand, WorkspaceLister
from .workspace.mutate_enhanced import (
    WorkspaceMutateCommand,
    WorkspaceMutation,
    WorkspaceMutator,
)
from .workspace.pr import WorkspacePrCommand, WorkspacePrCoordinator
from .workspace.pr_check_details import (
    WorkspacePrCheckDetailsCommand,
    WorkspacePrCheckDetailsReader,
)
from .workspace.pr_checks import (
    WorkspacePrChecksCommand,
    WorkspacePrChecksReader,
)
from .workspace.pr_evidence import (
    WorkspacePrEvidenceCommand,
    WorkspacePrEvidenceReader,
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
from .workspace.read import WorkspaceReadCommand, WorkspaceReader
from .workspace.refresh import WorkspaceRefreshCommand, WorkspaceRefresher
from .workspace.refresh_preview import (
    WorkspaceRefreshPreviewCommand,
    WorkspaceRefreshPreviewer,
)
from .workspace.refresh_v2 import (
    RefreshResolution,
    WorkspaceRefreshV2,
    WorkspaceRefreshV2Command,
)
from .workspace.remove import WorkspaceRemoveCommand, WorkspaceRemover
from .workspace.restore_paths import (
    WorkspacePathsRestorer,
    WorkspaceRestorePathsCommand,
)
from .workspace.retrieval import (
    WorkspaceDiffV2Command,
    WorkspaceRetrieval,
    WorkspaceSearchV2Command,
    WorkspaceTreeV2Command,
)
from .workspace.run_adhoc import (
    WorkspaceAdhocRunner,
    WorkspaceRunAdhocCommand,
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
    if not isinstance(data, dict):
        raise TypeError("Application result must serialize to an object")
    payload_value = data.get("payload")
    if isinstance(payload_value, dict):
        payload = payload_value
    else:
        data.pop("payload", None)
        payload = data
    sanitized = sanitize_egress_data(payload, destination=EgressDestination.MODEL)
    if not isinstance(sanitized, dict):
        raise TypeError("Sanitized application result must remain an object")
    return sanitized


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
        ticket_graphs: TicketGraphGateway | None = None,
        ticket_projects: TicketProjectGateway | None = None,
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
                ticket_graphs=ticket_graphs,
                ticket_projects=ticket_projects,
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
        self._repo_history_v2 = RepositoryHistoryV2(ctx)
        self._repo_list_v2 = RepositoryListV2(ctx)
        self._repo_issue_v2 = RepositoryIssueV2(ctx)
        self._repo_pr_v2 = RepositoryPrReadV2(ctx)
        self._task_context_v2 = RepositoryTaskContextV2(ctx)
        self._repo_tree = RepositoryTreeReader(ctx)
        self._repo_read = RepositoryFileReader(ctx)
        self._repo_reads = RepositoryFilesReader(ctx)
        self._repo_read_v2 = RepositoryReader(ctx)
        self._repo_retrieval = RepositoryRetrieval(ctx)
        self._repo_search = RepositorySearcher(ctx)
        self._recent = RecentCommitsReader(ctx)
        self._issue = IssueReader(ctx)
        self._issue_graph = RepositoryIssueGraphReader(ctx)
        self._issue_next = RepositoryIssueNextReader(ctx)
        self._issue_spec = RepositoryIssueSpecReader(ctx)
        self._repo_pr = PullRequestReader(ctx)
        self._task_context = RepoTaskContextReader(ctx)
        self._ticket_project_sync = TicketProjectSyncer(ctx)
        self._create = WorkspaceCreator(ctx)
        self._create_v2 = WorkspaceCreatorV2(ctx)
        self._list = WorkspaceLister(ctx)
        self._list_v2 = WorkspaceListerV2(ctx)
        self._status = WorkspaceStatusReader(ctx)
        self._status_v2 = WorkspaceStatusV2(ctx)
        self._assessment = WorkspaceAssessmentReader(ctx)
        self._base_status = WorkspaceBaseStatusReader(ctx)
        self._tree = WorkspaceTreeReader(ctx)
        self._read = WorkspaceFileReader(ctx)
        self._reads = WorkspaceFilesReader(ctx)
        self._read_v2 = WorkspaceReader(ctx)
        self._workspace_retrieval = WorkspaceRetrieval(ctx)
        self._search = WorkspaceSearcher(ctx)
        self._write = WorkspaceFileWriter(ctx)
        self._edit = WorkspaceEditor(ctx)
        self._mutate = WorkspaceMutator(ctx)
        self._patch = WorkspacePatchApplier(ctx)
        self._restore = WorkspacePathsRestorer(ctx)
        self._refresh_preview = WorkspaceRefreshPreviewer(ctx)
        self._refresh = WorkspaceRefresher(ctx)
        self._refresh_v2 = WorkspaceRefreshV2(ctx)
        self._diff = WorkspaceDiffReader(ctx)
        self._profile = WorkspaceProfileRunner(
            ctx,
            operations=self.operations,
            background_tasks=self.application.background_tasks,
        )
        self._diagnostic = WorkspaceDiagnosticRunner(ctx)
        self._hygiene_status = WorkspaceHygieneStatusReader(ctx)
        self._format_changed = WorkspaceChangedFormatter(ctx)
        self._format_changed_v2 = WorkspaceChangedFormatterV2(ctx)
        self._adhoc = WorkspaceAdhocRunner(
            ctx,
            operations=self.operations,
            background_tasks=self.application.background_tasks,
        )
        self._operation = OperationCoordinator(
            status=self._operation_status,
            lister=self._operation_list,
            cancel=self._operation_cancel,
            request_live_cancel=self._request_live_operation_cancel,
        )
        self._verify = WorkspaceVerifier(
            ctx,
            assessment=self._assessment,
            profile=self._profile,
            diagnostic=self._diagnostic,
            adhoc=self._adhoc,
        )
        self._commit = WorkspaceCommitter(ctx)
        self._push = WorkspacePusher(ctx)
        self._create_pr = DraftPullRequestCreator(ctx)
        self._update_pr = DraftPullRequestUpdater(ctx)
        self._pr_status = WorkspacePrStatusReader(ctx)
        self._checks = WorkspacePrChecksReader(ctx)
        self._check_details = WorkspacePrCheckDetailsReader(ctx)
        self._failure_evidence = WorkspacePrFailureEvidenceReader(ctx)
        self._pr_watch = self.application.pr_check_watches
        self._pr = WorkspacePrCoordinator(
            ctx,
            creator=self._create_pr,
            updater=self._update_pr,
            watch=self._pr_watch,
        )
        self._pr_evidence = WorkspacePrEvidenceReader(
            ctx,
            status=self._pr_status,
            checks=self._checks,
            details=self._check_details,
            failure=self._failure_evidence,
        )
        self._remove = WorkspaceRemover(ctx)
        self._remove_v2 = WorkspaceRemoverV2(ctx)
        self._doctor = Doctor(ctx)

    def _request_live_operation_cancel(self, kind: str, operation_id: str) -> bool:
        if kind == "workspace_run_profile":
            return self._profile.request_live_cancel(operation_id)
        if kind == "workspace_run_adhoc":
            return self._adhoc.request_live_cancel(operation_id)
        return False

    def operation(
        self,
        action: str,
        operation_id: str | None = None,
        scope: str | None = None,
        state: str | None = None,
        expected_updated_at: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._operation.execute(
                OperationCommand(
                    action=action,
                    operation_id=operation_id,
                    scope=scope,
                    state=state,
                    expected_updated_at=expected_updated_at,
                    limit=limit,
                    cursor=cursor,
                )
            )
        )

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
        if result.cancellation_requested:
            self._request_live_operation_cancel(result.operation.kind, operation_id)
        return _result(result)

    def repo_list(self) -> dict[str, Any]:
        return _result(self._repo_list.execute(RepositoryListCommand()))

    def repo_list_v2(
        self,
        detail: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return _result(self._repo_list_v2.execute(RepositoryListV2Command(detail, cursor, limit)))

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

    def repo_read(
        self,
        repo_id: str,
        files: list[FileReadRequest],
        ref: str | None = None,
        byte_budget: int = 60_000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._repo_read_v2.execute(
                RepositoryReadCommand(repo_id, tuple(files), ref, byte_budget, cursor)
            )
        )

    def repo_search(
        self,
        repo_id: str,
        query: str,
        ref: str | None = None,
        path_glob: str | None = None,
        max_results: int = 200,
        context_lines: int = 0,
    ) -> dict[str, Any]:
        return _result(
            self._repo_search.execute(
                RepositorySearchCommand(repo_id, query, ref, path_glob, max_results, context_lines)
            )
        )

    def repo_search_v2(
        self,
        repo_id: str,
        query: str,
        mode: SearchMode = SearchMode.LITERAL,
        ref: str | None = None,
        path_glob: str | None = None,
        max_results: int = 100,
        context_lines: int = 0,
        byte_budget: int = 60_000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._repo_retrieval.search(
                RepositorySearchV2Command(
                    repo_id,
                    query,
                    mode,
                    ref,
                    path_glob,
                    max_results,
                    context_lines,
                    byte_budget,
                    cursor,
                )
            )
        )

    def repo_tree_v2(
        self,
        repo_id: str,
        ref: str | None = None,
        subtree: str | None = None,
        max_entries: int = 500,
        byte_budget: int = 60_000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._repo_retrieval.tree(
                RepositoryTreeV2Command(
                    repo_id,
                    ref,
                    subtree,
                    max_entries,
                    byte_budget,
                    cursor,
                )
            )
        )

    def repo_recent_commits(self, repo_id: str, limit: int = 20) -> dict[str, Any]:
        return _result(self._recent.execute(RecentCommitsCommand(repo_id, limit)))

    def repo_history_v2(
        self,
        repo_id: str,
        mode: str,
        ref: str | None = None,
        base_ref: str | None = None,
        head_ref: str | None = None,
        path_glob: str | None = None,
        limit: int = 20,
        include_patch: bool = False,
        byte_budget: int = 60_000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._repo_history_v2.execute(
                RepositoryHistoryV2Command(
                    repo_id,
                    mode,
                    ref,
                    base_ref,
                    head_ref,
                    path_glob,
                    limit,
                    include_patch,
                    byte_budget,
                    cursor,
                )
            )
        )

    def repo_issue_read(
        self, repo_id: str, issue_number: int, fresh: bool = False
    ) -> dict[str, Any]:
        return _result(self._issue.execute(IssueReadCommand(repo_id, issue_number, fresh)))

    def repo_issue_graph(
        self,
        repo_id: str,
        root_issue: int | None = None,
        status: str | None = None,
        priority: str | None = None,
        initiative: int | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        return _result(
            self._issue_graph.execute(
                RepositoryIssueGraphCommand(
                    repo_id, root_issue, status, priority, initiative, fresh
                )
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
        fresh: bool = False,
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
                    fresh,
                )
            )
        )

    def repo_issue_spec(
        self, repo_id: str, issue_number: int, fresh: bool = False
    ) -> dict[str, Any]:
        return _result(
            self._issue_spec.execute(RepositoryIssueSpecCommand(repo_id, issue_number, fresh))
        )

    def repo_issue_v2(
        self,
        repo_id: str,
        mode: str,
        issue_number: int | None = None,
        root_issue: int | None = None,
        status: str | None = None,
        priority: str | None = None,
        initiative: int | None = None,
        limit: int = 10,
        fresh: bool = False,
        cursor: str | None = None,
        body: str | None = None,
        title: str | None = None,
        evidence_ref: str | None = None,
        target_issue: int | None = None,
        link_type: str | None = None,
        idempotency_key: str | None = None,
        approval_request_id: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._repo_issue_v2.execute(
                RepositoryIssueV2Command(
                    repo_id=repo_id,
                    mode=mode,
                    issue_number=issue_number,
                    root_issue=root_issue,
                    status=status,
                    priority=priority,
                    initiative=initiative,
                    limit=limit,
                    fresh=fresh,
                    cursor=cursor,
                    body=body,
                    title=title,
                    evidence_ref=evidence_ref,
                    target_issue=target_issue,
                    link_type=link_type,
                    idempotency_key=idempotency_key,
                    approval_request_id=approval_request_id,
                )
            )
        )

    def repo_pr_read(self, repo_id: str, pr_number: int, fresh: bool = False) -> dict[str, Any]:
        return _result(self._repo_pr.execute(PullRequestReadCommand(repo_id, pr_number, fresh)))

    def repo_pr_read_v2(
        self,
        repo_id: str,
        pr_number: int,
        fresh: bool = False,
        detail: str = "overview",
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._repo_pr_v2.execute(
                RepositoryPrReadV2Command(repo_id, pr_number, fresh, detail, cursor)
            )
        )

    def repo_task_context(
        self,
        repo_id: str,
        issue_number: int | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._task_context.execute(RepoTaskContextCommand(repo_id, issue_number, workspace_id))
        )

    def repo_task_context_v2(
        self,
        repo_id: str,
        issue_number: int | None = None,
        workspace_id: str | None = None,
        sections: list[str] | tuple[str, ...] = (
            "repository",
            "status",
            "ticket",
            "workspace",
            "recent_commits",
        ),
        byte_budget: int = 96_000,
    ) -> dict[str, Any]:
        return _result(
            self._task_context_v2.execute(
                RepositoryTaskContextV2Command(
                    repo_id,
                    issue_number,
                    workspace_id,
                    tuple(sections),
                    byte_budget,
                )
            )
        )

    def ticket_project_sync(
        self,
        *,
        repo_id: str,
        owner: str,
        project_number: int,
        owner_type: str = "organization",
        apply: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._ticket_project_sync.execute(
                TicketProjectSyncCommand(
                    repo_id=repo_id,
                    owner=owner,
                    project_number=project_number,
                    owner_type=TicketProjectOwnerType(owner_type),
                    apply=apply,
                    idempotency_key=idempotency_key,
                )
            )
        )

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

    def workspace_create_v2(
        self,
        repo_id: str,
        task_slug: str,
        base: str | None = None,
        idempotency_key: str | None = None,
        issue_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return _result(
            self._create_v2.execute(
                WorkspaceCreateV2Command(repo_id, task_slug, base, idempotency_key, issue_ids)
            )
        )

    def workspace_list(self) -> dict[str, Any]:
        return _result(self._list.execute(WorkspaceListCommand()))

    def workspace_list_v2(
        self,
        exists: bool | None = True,
        lifecycle: str | None = None,
        repo_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._list_v2.execute(WorkspaceListV2Command(exists, lifecycle, repo_id, limit, cursor))
        )

    def workspace_status(self, workspace_id: str) -> dict[str, Any]:
        return _result(self._status.execute(WorkspaceStatusCommand(workspace_id)))

    def workspace_status_v2(
        self,
        workspace_id: str,
        sections: tuple[str, ...] = ("local",),
        byte_budget: int = 60_000,
    ) -> dict[str, Any]:
        return _result(
            self._status_v2.execute(WorkspaceStatusV2Command(workspace_id, sections, byte_budget))
        )

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

    def workspace_read(
        self,
        workspace_id: str,
        files: list[FileReadRequest],
        byte_budget: int = 60_000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._read_v2.execute(
                WorkspaceReadCommand(workspace_id, tuple(files), byte_budget, cursor)
            )
        )

    def workspace_search(
        self,
        workspace_id: str,
        query: str,
        path_glob: str | None = None,
        max_results: int = 200,
        context_lines: int = 0,
    ) -> dict[str, Any]:
        return _result(
            self._search.execute(
                WorkspaceSearchCommand(workspace_id, query, path_glob, max_results, context_lines)
            )
        )

    def workspace_search_v2(
        self,
        workspace_id: str,
        query: str,
        mode: SearchMode = SearchMode.LITERAL,
        path_glob: str | None = None,
        max_results: int = 100,
        context_lines: int = 0,
        byte_budget: int = 60_000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._workspace_retrieval.search(
                WorkspaceSearchV2Command(
                    workspace_id,
                    query,
                    mode,
                    path_glob,
                    max_results,
                    context_lines,
                    byte_budget,
                    cursor,
                )
            )
        )

    def workspace_tree_v2(
        self,
        workspace_id: str,
        subtree: str | None = None,
        max_entries: int = 500,
        byte_budget: int = 60_000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._workspace_retrieval.tree(
                WorkspaceTreeV2Command(
                    workspace_id,
                    subtree,
                    max_entries,
                    byte_budget,
                    cursor,
                )
            )
        )

    def workspace_diff_v2(
        self,
        workspace_id: str,
        staged: bool = False,
        path_glob: str | None = None,
        max_files: int = 100,
        byte_budget: int = 120_000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._workspace_retrieval.diff(
                WorkspaceDiffV2Command(
                    workspace_id,
                    staged,
                    path_glob,
                    max_files,
                    byte_budget,
                    cursor,
                )
            )
        )

    def workspace_write_file(
        self,
        workspace_id: str,
        relative_path: str,
        content: str,
        expected_sha256: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._write.execute(
                WorkspaceFileWriteCommand(
                    workspace_id,
                    relative_path,
                    content,
                    expected_sha256,
                    idempotency_key,
                )
            )
        )

    def workspace_edit(
        self,
        workspace_id: str,
        files: list[FileEdit],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._edit.execute(WorkspaceEditCommand(workspace_id, tuple(files), idempotency_key))
        )

    def workspace_mutate(
        self,
        workspace_id: str,
        operations: list[WorkspaceMutation],
        expected_workspace_fingerprint: str,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._mutate.execute(
                WorkspaceMutateCommand(
                    workspace_id,
                    tuple(operations),
                    expected_workspace_fingerprint,
                    dry_run,
                    idempotency_key,
                )
            )
        )

    def workspace_apply_patch(
        self,
        workspace_id: str,
        patch: str,
        expected_head_sha: str,
        expected_workspace_fingerprint: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._patch.execute(
                WorkspaceApplyPatchCommand(
                    workspace_id,
                    patch,
                    expected_head_sha,
                    expected_workspace_fingerprint,
                    idempotency_key,
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

    def workspace_refresh_v2(
        self,
        workspace_id: str,
        *,
        action: str,
        expected_head_sha: str,
        expected_fingerprint: str,
        plan_token: str | None = None,
        resolutions: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        normalized = tuple(
            RefreshResolution(item["path"], item["content"]) for item in (resolutions or [])
        )
        return _result(
            self._refresh_v2.execute(
                WorkspaceRefreshV2Command(
                    workspace_id,
                    action,
                    expected_head_sha,
                    expected_fingerprint,
                    plan_token,
                    normalized,
                )
            )
        )

    def workspace_diff(self, workspace_id: str, staged: bool = False) -> dict[str, Any]:
        return _result(self._diff.execute(WorkspaceDiffCommand(workspace_id, staged)))

    def workspace_run_profile(
        self,
        workspace_id: str,
        profile_name: str | None = None,
        background: bool = False,
        force_rerun: bool = False,
    ) -> dict[str, Any]:
        return _result(
            self._profile.execute(
                WorkspaceRunProfileCommand(
                    workspace_id,
                    profile_name,
                    background,
                    force_rerun,
                )
            )
        )

    def workspace_run_diagnostic(
        self,
        workspace_id: str,
        diagnostic_id: str,
        selector: str | list[str] | None = None,
        expected_fingerprint: str | None = None,
        intent: str | None = None,
        expectation: str | None = None,
        expected_failure_class: str | None = None,
        selector2: str | list[str] | None = None,
        force_rerun: bool = False,
    ) -> dict[str, Any]:
        return _result(
            self._diagnostic.execute(
                WorkspaceRunDiagnosticCommand(
                    workspace_id,
                    diagnostic_id,
                    selector,
                    expected_fingerprint,
                    intent,
                    expectation,
                    expected_failure_class,
                    selector2,
                    force_rerun,
                )
            )
        )

    def workspace_run_adhoc(
        self,
        workspace_id: str,
        argv: list[str],
        working_directory: str | None = None,
        background: bool = False,
    ) -> dict[str, Any]:
        return _result(
            self._adhoc.execute(
                WorkspaceRunAdhocCommand(
                    workspace_id,
                    tuple(argv) if isinstance(argv, list) else argv,
                    working_directory,
                    background,
                )
            )
        )

    def workspace_hygiene_status(
        self,
        workspace_id: str,
        formatter_id: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._hygiene_status.execute(WorkspaceHygieneStatusCommand(workspace_id, formatter_id))
        )

    def workspace_format_changed(
        self,
        workspace_id: str,
        expected_fingerprint: str,
        formatter_id: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._format_changed.execute(
                WorkspaceFormatChangedCommand(
                    workspace_id,
                    expected_fingerprint,
                    formatter_id,
                )
            )
        )

    def workspace_format_changed_v2(
        self,
        workspace_id: str,
        expected_fingerprint: str,
        formatter_id: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._format_changed_v2.execute(
                WorkspaceFormatChangedV2Command(
                    workspace_id,
                    expected_fingerprint,
                    formatter_id,
                )
            )
        )

    def workspace_verify(
        self,
        workspace_id: str,
        mode: str = "auto",
        diagnostic_id: str | None = None,
        selector: str | list[str] | None = None,
        selector2: str | list[str] | None = None,
        profile_name: str | None = None,
        argv: tuple[str, ...] | None = None,
        working_directory: str | None = None,
        expected_fingerprint: str | None = None,
        background: bool = False,
        intent: str | None = None,
        expectation: str | None = None,
        expected_failure_class: str | None = None,
        force_rerun: bool = False,
        impact_paths: tuple[str, ...] = (),
        artifact_output_path: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._verify.execute(
                WorkspaceVerifyCommand(
                    workspace_id=workspace_id,
                    mode=mode,  # type: ignore[arg-type]
                    diagnostic_id=diagnostic_id,
                    selector=selector,
                    selector2=selector2,
                    profile_name=profile_name,
                    argv=argv,
                    working_directory=working_directory,
                    expected_fingerprint=expected_fingerprint,
                    background=background,
                    intent=intent,
                    expectation=expectation,
                    expected_failure_class=expected_failure_class,
                    force_rerun=force_rerun,
                    impact_paths=impact_paths,
                    artifact_output_path=artifact_output_path,
                )
            )
        )

    def workspace_commit(
        self,
        workspace_id: str,
        message: str,
        expected_head_sha: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._commit.execute(
                WorkspaceCommitCommand(
                    workspace_id,
                    message,
                    expected_head_sha,
                    expected_fingerprint,
                )
            )
        )

    def workspace_push(
        self,
        workspace_id: str,
        idempotency_key: str | None = None,
        expected_remote_head: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._push.execute(
                WorkspacePushCommand(workspace_id, idempotency_key, expected_remote_head)
            )
        )

    def workspace_pr(
        self,
        workspace_id: str,
        action: str,
        title: str | None = None,
        body: str | None = None,
        evidence_ref: str | None = None,
        review_comment_id: int | None = None,
        idempotency_key: str | None = None,
        expected_remote_version: str | None = None,
        until: str = "all_completed",
        timeout_seconds: int = 900,
        event_cursor: str | None = None,
    ) -> dict[str, Any]:
        return _result(
            self._pr.execute(
                WorkspacePrCommand(
                    workspace_id=workspace_id,
                    action=action,
                    title=title,
                    body=body,
                    evidence_ref=evidence_ref,
                    review_comment_id=review_comment_id,
                    idempotency_key=idempotency_key,
                    expected_remote_version=expected_remote_version,
                    until=until,
                    timeout_seconds=timeout_seconds,
                    event_cursor=event_cursor,
                )
            )
        )

    def workspace_pr_evidence(
        self,
        workspace_id: str,
        detail: str = "overview",
        check_selector: str | None = None,
        since: str | None = None,
        max_excerpt_lines: int = 80,
    ) -> dict[str, Any]:
        return _result(
            self._pr_evidence.execute(
                WorkspacePrEvidenceCommand(
                    workspace_id,
                    detail,
                    check_selector,
                    since,
                    max_excerpt_lines,
                )
            )
        )

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

    def workspace_remove_v2(
        self, workspace_id: str, delete_local_branch: bool = False
    ) -> dict[str, Any]:
        return _result(
            self._remove_v2.execute(WorkspaceRemoveV2Command(workspace_id, delete_local_branch))
        )

    def doctor(self) -> dict[str, Any]:
        return _result(self._doctor.execute(DoctorCommand()))
