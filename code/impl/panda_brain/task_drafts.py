"""P06 plan-to-task drafting. No publication, delivery, or execution authority."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone

COMMON_LABELS = {
    'location': '地点范围', 'time_requirement': '时间要求',
    'feedback_requirement': '反馈要求', 'field_constraints': '现场约束',
    'safety_measures': '安全事项', 'firefighting_method': '扑救方式',
    'command_relationship': '指挥关系', 'coordination': '协同关系',
    'support_measures': '保障措施',
}

TASK_LABELS = {
    'title': '任务名称', 'objective': '目标', 'completion_standard': '完成标准',
    'responsible_group_id': '主责组', 'verifier_id': '核验负责人',
}


@dataclass(frozen=True)
class TaskCommonContent:
    location: str = ''
    time_requirement: str = ''
    feedback_requirement: str = ''
    field_constraints: str = ''
    safety_measures: str = ''
    firefighting_method: str = ''
    command_relationship: str = ''
    coordination: str = ''
    support_measures: str = ''

    def __post_init__(self):
        if any(not isinstance(getattr(self, f.name), str) for f in fields(self)):
            raise ValueError('任务内容必须为文字；未知内容留空。')


@dataclass(frozen=True)
class TaskDraftSpec:
    title: str = ''
    objective: str = ''
    completion_standard: str = ''
    responsible_group_id: str = ''
    verifier_id: str = ''
    content: TaskCommonContent = TaskCommonContent()
    # Explicit references, not blank-field fallback. Empty means no inheritance.
    common_fields: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ('title', 'objective', 'completion_standard',
                     'responsible_group_id', 'verifier_id'):
            if not isinstance(getattr(self, name), str):
                raise ValueError('每项任务只能填写一个主责组；任务字段必须为文字。')
        if not isinstance(self.content, TaskCommonContent):
            raise ValueError('任务内容格式不正确。')
        allowed = {f.name for f in fields(TaskCommonContent)}
        if (not isinstance(self.common_fields, tuple)
                or any(not isinstance(x, str) or x not in allowed for x in self.common_fields)
                or len(set(self.common_fields)) != len(self.common_fields)):
            raise ValueError('共同内容引用字段不正确。')
        if any(getattr(self.content, name) for name in self.common_fields):
            raise ValueError('同一字段不能同时填写本任务内容和引用共同内容。')


@dataclass(frozen=True)
class PlanTaskBlueprint:
    main: TaskDraftSpec = TaskDraftSpec()
    # Only independently executed/reported assistance belongs here.
    children: tuple[TaskDraftSpec, ...] = ()
    common: TaskCommonContent = TaskCommonContent()

    def __post_init__(self):
        if (not isinstance(self.main, TaskDraftSpec)
                or not isinstance(self.children, tuple)
                or any(not isinstance(x, TaskDraftSpec) for x in self.children)
                or not isinstance(self.common, TaskCommonContent)):
            raise ValueError('方案任务编制格式不正确。')


@dataclass(frozen=True)
class TaskDraft:
    id: str
    parent_id: str
    spec: TaskDraftSpec


@dataclass(frozen=True)
class TaskDraftBundle:
    id: str
    event_id: str
    decision_id: str
    option_id: str
    source_version: int
    created_by: str
    recorded_at: str
    # Immutable source, including risks, estimates, evidence IDs and simulated
    # geometry. None of these is silently promoted to a verified task field.
    source_option_json: str
    common: TaskCommonContent
    tasks: tuple[TaskDraft, ...]
    status: str = 'draft'
    version: int = 1
    updated_at: str = ''

    def resolved_content(self, task_id: str) -> TaskCommonContent:
        task = next((t for t in self.tasks if t.id == task_id), None)
        if task is None:
            raise ValueError('任务不属于本草案。')
        values = asdict(task.spec.content)
        for name in task.spec.common_fields:
            values[name] = getattr(self.common, name)
        return TaskCommonContent(**values)


def build_task_drafts(option, decision, event_id: str) -> TaskDraftBundle:
    """Build entirely before mutating a workspace; no inference from prose/maps."""
    if not event_id or decision.decision_type != 'adopt' or decision.option_id != option.id:
        raise ValueError('任务草案必须关联本事件已采纳的方案。')
    blueprint = option.task_blueprint
    if blueprint is None:
        blueprint = PlanTaskBlueprint(main=TaskDraftSpec(
            title=option.title, objective=option.summary))
    if not isinstance(blueprint, PlanTaskBlueprint):
        raise ValueError('方案任务编制格式不正确。')
    bundle_id = f'task-draft-{decision.id}'
    main_id = f'{bundle_id}-main'
    tasks = (TaskDraft(main_id, '', blueprint.main),) + tuple(
        TaskDraft(f'{bundle_id}-child-{i}', main_id, spec)
        for i, spec in enumerate(blueprint.children, 1))
    return TaskDraftBundle(
        id=bundle_id, event_id=event_id, decision_id=decision.id,
        option_id=option.id, source_version=decision.source_version,
        created_by=decision.decided_by,
        recorded_at=datetime.now(timezone.utc).isoformat(),
        source_option_json=json.dumps(asdict(option), ensure_ascii=False,
                                      sort_keys=True, allow_nan=False),
        common=blueprint.common, tasks=tasks)
