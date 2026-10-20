"""Rule-based ChiefAdvisor (doc 11 §7 P1): goal frames, question trees, decision briefs.

The chief advisor drafts, orchestrates, and composes; every general-affecting
mutation still flows through authenticated workspace entry points.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .advisor_core import StaffCouncil
from .models import (
    AdvisoryTopic,
    AdvisorProposal,
    AdvisorRole,
    Assessment,
    ClaimStatus,
    DecisionActor,
    DecisionBrief,
    FeedbackType,
    FeasibilityStatus,
    GENERAL_ENTRY_ID,
    GeneralQuestionItem,
    GoalFrame,
    InformationGap,
    InformationGapStatus,
    InputClass,
    MemoryCandidate,
    Option,
    OptionRow,
    OrchestrationFeedback,
    QuestionNode,
    QuestionTree,
    Review,
    StaffPlan,
    StaffTask,
    StaffTaskStatus,
    StaffWorkType,
)
from .workspace import ScenarioWorkspace


_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_LOW, _CJK_HIGH = "\u4e00", "\u9fff"


def _lexical_tokens(text: str) -> frozenset[str]:
    """Deterministic lexical tokens for relevance scoring.

    ASCII word tokens plus CJK character unigrams and bigrams. Coarse on
    purpose: no third-party dependency, fully offline, run-to-run stable.
    """
    lowered = text.lower()
    tokens: set[str] = set(_ASCII_WORD.findall(lowered))
    cjk_chars = [ch for ch in lowered if _CJK_LOW <= ch <= _CJK_HIGH]
    tokens.update(cjk_chars)
    tokens.update(a + b for a, b in zip(cjk_chars, cjk_chars[1:]))
    return frozenset(tokens)


# 主张键/可逆性的中文展示名（仅展示层，不改写入审计链的内部标识）。
_SUBJECT_CN = {
    "route:R1": "原路线 R1",
    "route:R2": "备选路线 R2",
    "water:W1": "水源 W1",
    "weather:wind": "风力风向",
    "fire:trend": "火势趋势",
    "team:T1": "一分队",
    "team:T2": "预备队",
}
_REVERSIBILITY_CN = {
    "reversible": "可逆",
    "partially_reversible": "部分可逆",
    "irreversible": "不可逆",
}


def _subject_cn(key: str) -> str:
    return _SUBJECT_CN.get(str(key), str(key))


def _rev_cn(value: str) -> str:
    v = str(value).replace("_", " ")
    return _REVERSIBILITY_CN.get(str(value), v)


class ChiefAdvisor:
    """Drafts goal frames and question trees by rule, orchestrates reviews,
    composes the fixed-structure decision brief, and routes classified general input."""

    def __init__(self, council: StaffCouncil, *, situation_scorer=None) -> None:
        self.council = council
        #: Optional deterministic scorer (P8-T2); when bound, its rendered
        #: verdict is appended to every decision brief as a quantitative annex.
        self.situation_scorer = situation_scorer

    # ------------------------------------------------------------------
    # Goal frame

    def open_goal_frame(
        self,
        workspace: ScenarioWorkspace,
        statement: str,
        *,
        constraints: tuple[str, ...] = (),
        success_criteria: tuple[str, ...] = (),
        priority: str = "normal",
    ) -> GoalFrame:
        frame = GoalFrame(
            id=f"goal-{workspace.version + 1:03d}",
            statement=statement,
            constraints=constraints or ("commander retains final decision authority",),
            success_criteria=success_criteria,
            priority=priority,
            source_version=workspace.version,
        )
        return workspace.record_goal_frame(frame)

    def confirm_goal_frame(self, workspace: ScenarioWorkspace, goal_frame_id: str) -> GoalFrame:
        """Confirmation is a general act routed through the authenticated entry."""
        return workspace.confirm_goal_frame(
            goal_frame_id,
            actor_role=DecisionActor.COMMANDER,
            confirmed_by=GENERAL_ENTRY_ID,
        )

    # ------------------------------------------------------------------
    # Question tree

    def build_question_tree(self, workspace: ScenarioWorkspace, goal_frame: GoalFrame) -> QuestionTree:
        nodes: list[QuestionNode] = [
            QuestionNode(
                id="q-root",
                parent_id=None,
                question=f"朝目标「{goal_frame.statement}」推进，下一步有限的动作是什么？",
                kind="root",
            )
        ]
        index = 1
        for gap in workspace.information_gaps.values():
            if gap.status != InformationGapStatus.OPEN:
                continue
            nodes.append(
                QuestionNode(
                    id=f"q-gap-{index:02d}",
                    parent_id="q-root",
                    question=f"需要哪些证据才能补上「{_subject_cn(gap.subject)}」这个缺口？",
                    kind="gap",
                    subject=gap.subject,
                )
            )
            index += 1
        active_risks = self._latest_risks(workspace)
        if active_risks:
            nodes.append(
                QuestionNode(
                    id=f"q-risk-{index:02d}",
                    parent_id="q-root",
                    question=f"在办的 {len(active_risks)} 项风险中，是否有足以推翻现有方案的风险？",
                    kind="risk",
                )
            )
            index += 1
        nodes.append(
            QuestionNode(
                id=f"q-option-{index:02d}",
                parent_id="q-root",
                question="应把哪套有边界的方案呈报指挥员决断？",
                kind="option_selection",
            )
        )
        tree = QuestionTree(
            id=f"tree-{workspace.version + 1:03d}",
            goal_frame_id=goal_frame.id,
            nodes=tuple(nodes),
            source_version=workspace.version,
        )
        workspace.append_event(
            "QuestionTreeBuilt",
            tree_id=tree.id,
            goal_frame_id=goal_frame.id,
            node_count=len(nodes),
        )
        return tree

    # ------------------------------------------------------------------
    # Orchestration and decision brief

    def orchestrate(self, workspace: ScenarioWorkspace, question: str) -> AdvisoryTopic:
        topic = AdvisoryTopic(
            id=f"topic-{workspace.version + 1:03d}",
            objective=self.council.playbook.objective,
            question=question,
            constraints=("commander retains final decision authority", "offline simulation only"),
            source_version=workspace.version,
        )
        self.council.review(workspace, topic)
        return topic

    def compose_decision_brief(
        self,
        workspace: ScenarioWorkspace,
        topic: AdvisoryTopic,
        goal_frame: GoalFrame | None = None,
    ) -> DecisionBrief:
        specialist_count = len(self.council.playbook.specialists)
        latest_assessments = workspace.assessments[-specialist_count:]
        situation_summary = tuple(assessment.summary for assessment in latest_assessments)
        if self.situation_scorer is not None:
            situation_summary = situation_summary + (
                self.situation_scorer.score(workspace.claims).render(),
            )
        key_risks = tuple(
            dict.fromkeys(risk for assessment in latest_assessments for risk in assessment.risks)
        )
        active_options = [
            option for option in workspace.options.values() if option.status == "active"
        ]
        open_gaps = tuple(
            gap.subject
            for gap in workspace.information_gaps.values()
            if gap.status == InformationGapStatus.OPEN
        )

        matrix = [self._build_option_row(workspace, option) for option in active_options]
        question_items = self._detect_escalation_items(workspace, matrix)

        recommendation = self._compose_recommendation(matrix)
        chief_assessment = next(
            (
                assessment
                for assessment in reversed(workspace.assessments)
                if assessment.advisor_role is not None and assessment.advisor_role.value == "chief_advisor"
            ),
            None,
        )
        if recommendation is None:
            recommendation = (
                chief_assessment.summary
                if chief_assessment is not None
                else "尚无首席参谋评估可用。"
            )

        brief = DecisionBrief(
            id=f"brief-{workspace.version + 1:03d}",
            topic_id=topic.id,
            goal_frame_id=goal_frame.id if goal_frame is not None else None,
            question=topic.question,
            situation_summary=situation_summary,
            option_ids=tuple(option.id for option in active_options),
            key_risks=key_risks,
            disagreement_ids=tuple(disagreement.id for disagreement in workspace.disagreements),
            open_gap_subjects=open_gaps,
            recommendation=recommendation,
            general_questions=(topic.question,)
            + tuple(item.text for item in question_items),
            source_version=workspace.version,
            option_matrix=tuple(matrix),
            question_items=tuple(question_items),
            relevant_experiences=tuple(
                candidate.content for candidate in self.retrieve_relevant_experiences(workspace)
            ),
        )
        return workspace.record_decision_brief(brief)

    def _build_option_row(self, workspace: ScenarioWorkspace, option) -> OptionRow:
        pre_unmet = tuple(
            subject
            for subject in option.preconditions
            if subject not in workspace.claims
            or workspace.claims[subject].status != ClaimStatus.VERIFIED
        )
        pre_met = tuple(
            subject for subject in option.preconditions if subject not in pre_unmet
        )
        no_go_triggered = any(
            subject in workspace.claims
            and workspace.claims[subject].status in {ClaimStatus.CHALLENGED, ClaimStatus.SUPERSEDED}
            for subject in option.no_go_conditions
        )
        verdict = self._latest_verdict(workspace, option.id)
        if no_go_triggered:
            feasibility = FeasibilityStatus.INFEASIBLE
            blocking = tuple(option.no_go_conditions)
        elif verdict is not None:
            feasibility = verdict.status
            blocking = verdict.blocking_conditions
        else:
            feasibility = FeasibilityStatus.UNKNOWN if pre_unmet else FeasibilityStatus.FEASIBLE
            blocking = pre_unmet
        return OptionRow(
            option_id=option.id,
            preconditions_met=pre_met,
            preconditions_unmet=pre_unmet,
            cost_estimate=option.cost_estimate,
            time_estimate=option.time_estimate,
            reversibility=option.reversibility.value,
            no_go_triggered=no_go_triggered,
            feasibility_status=feasibility.value,
            blocking_conditions=blocking,
            key_risks=tuple(option.risks),
        )

    @staticmethod
    def _latest_verdict(workspace: ScenarioWorkspace, option_id: str):
        candidates = [
            verdict
            for verdict in workspace.feasibility_verdicts.values()
            if verdict.option_id == option_id
        ]
        return max(candidates, key=lambda verdict: verdict.source_version, default=None)

    @staticmethod
    def _compose_recommendation(matrix) -> str | None:
        """Pick a recommendation without ever sounding pseudo-certain (doc 11 §7 P3).

        Clean options are recommended plainly; options resting on unverified
        premises only earn a conditional recommendation; nothing viable means an
        explicit insufficient-evidence statement.
        """
        viable = [row for row in matrix if not row.no_go_triggered and row.feasibility_status != "infeasible"]
        fully_clean = [
            row
            for row in viable
            if not row.preconditions_unmet and row.feasibility_status != "unknown"
        ]
        if fully_clean:
            chosen = fully_clean[0]
            return f"推荐「{chosen.option_id}」：前提已核验，{ _rev_cn(chosen.reversibility) }。"
        if viable:
            chosen = viable[0]
            outstanding = tuple(dict.fromkeys(chosen.preconditions_unmet + chosen.blocking_conditions))
            return (
                f"有条件推荐「{chosen.option_id}」：须先解决"
                f"{'、'.join(_subject_cn(s) for s in outstanding)}；在此之前不得投入。"
            )
        return None

    def _detect_escalation_items(self, workspace: ScenarioWorkspace, matrix) -> list[GeneralQuestionItem]:
        """Three escalation triggers of doc 11 §7 P3: value trade-off, insufficient
        evidence, and key disagreement."""
        items: list[GeneralQuestionItem] = []
        index = 1

        viable = [row for row in matrix if not row.no_go_triggered and row.feasibility_status != "infeasible"]
        for row in viable:
            if row.preconditions_unmet or row.feasibility_status == "unknown":
                items.append(
                    GeneralQuestionItem(
                        id=f"gq-{index:02d}",
                        kind="insufficient_evidence",
                        text=(
                            f"Option '{row.option_id}' rests on unverified premises"
                            f" ({', '.join(dict.fromkeys(row.preconditions_unmet + row.blocking_conditions))});"
                            " request supplementary research before any commitment."
                        ),
                        subject=row.option_id,
                    )
                )
                index += 1

        clean = [row for row in viable if row.feasibility_status == "feasible" and not row.preconditions_unmet]
        if len(clean) >= 2 and len({row.reversibility for row in clean}) >= 2:
            named = "、".join(f"{row.option_id}（{_rev_cn(row.reversibility)}）" for row in clean)
            items.append(
                GeneralQuestionItem(
                    id=f"gq-{index:02d}",
                    kind="value_tradeoff",
                    text=(
                        f"可行方案在可逆性与其他收益上存在取舍：{named}。"
                        "速度与可逆之间的权衡属于指挥员的价值判断。"
                    ),
                )
            )
            index += 1

        active_subjects = {
            subject
            for option in workspace.options.values()
            if option.status == "active"
            for subject in option.claim_subjects
        }
        claim_subject_by_id = {claim.id: claim.subject for claim in workspace.claims.values()}
        escalated_topics: set[str] = set()
        for disagreement in workspace.disagreements:
            if disagreement.topic in escalated_topics:
                continue
            overlap = {
                claim_subject_by_id.get(evidence_id)
                for evidence_id in disagreement.evidence_ids
            } & active_subjects
            if overlap:
                items.append(
                    GeneralQuestionItem(
                        id=f"gq-{index:02d}",
                        kind="key_disagreement",
                        text=(
                            f"参谋部分歧「{disagreement.topic}」仍未解决，且已通过"
                            f"{'、'.join(_subject_cn(s) for s in sorted(subject for subject in overlap if subject))}"
                            "牵连在议方案；已上报指挥员裁断。"
                        ),
                        subject=disagreement.topic,
                    )
                )
                escalated_topics.add(disagreement.topic)
                index += 1
        return items

    # ------------------------------------------------------------------
    # Single-staff control path (P2 comparison group)

    def solo_review(self, workspace: ScenarioWorkspace, question: str) -> AdvisoryTopic:
        """Single-staff path: the chief advises alone, no specialist posts convened.

        No dedicated research enumeration, no analysis pass, no red-team challenge:
        this is the control group of the P2 staffing comparison, not a recommended mode.
        """
        topic = AdvisoryTopic(
            id=f"topic-{workspace.version + 1:03d}",
            objective=self.council.playbook.objective,
            question=question,
            constraints=("commander retains final decision authority", "offline simulation only"),
            source_version=workspace.version,
        )
        workspace.record_topic(topic)
        workspace.append_event("SituationConferenceStarted", topic_id=topic.id)

        assessment = Assessment(
            id=f"solo-chief-{workspace.version + 1:03d}",
            agent="ChiefAdvisor",
            summary="Single-staff surface review without specialist challenge passes.",
            evidence_ids=tuple(claim.id for claim in workspace.claims.values()),
            risks=StaffCouncil._feedback_risks(workspace),
            advisor_role=AdvisorRole.CHIEF_ADVISOR,
            topic_id=topic.id,
        )
        workspace.record_assessment(assessment)

        for option in self._solo_options(workspace):
            if option.id not in workspace.options:
                workspace.add_option(option)
        available = [
            option
            for option in workspace.options.values()
            if option.status != "rejected"
        ]
        proposal = AdvisorProposal(
            id=f"proposal-{workspace.version + 1:03d}",
            topic_id=topic.id,
            advisor_role=AdvisorRole.OPTION,
            summary="Single-staff proposal assembled without specialist challenge.",
            option_ids=tuple(option.id for option in available),
            evidence_ids=tuple(dict.fromkeys(
                evidence_id
                for option in available
                for evidence_id in option.evidence_ids
            )),
            source_version=workspace.version,
        )
        self.council.core.submit_proposal(workspace, proposal)
        return topic

    def _solo_options(self, workspace: ScenarioWorkspace) -> list[Option]:
        """The playbook's option space stripped of challenge-derived risks:
        domain structure is knowledge, challenge content is specialist cognition."""
        return [
            replace(option, risks=())
            for option in self.council.playbook.build_options(workspace, ())
        ]

    # ------------------------------------------------------------------
    # Execution anomaly interlock (doc 11 §7 P4)

    _ANOMALY_STATUSES = frozenset({"failed", "timeout", "permission_denied", "unknown_outcome"})

    def handle_execution_report(self, workspace: ScenarioWorkspace, order_id: str, report) -> dict:
        """Freeze on execution anomalies and hand the decision back to the general.

        The interlock pauses the affected order and raises a GeneralQuestion;
        it never changes strategy and never retries blindly (doc 11 §3.5).
        """
        if report.status.value not in self._ANOMALY_STATUSES:
            return {"paused": False, "question_id": None}
        workspace.pause_order_for_anomaly(
            order_id,
            reason=f"execution ended in {report.status.value}",
            report_id=report.id,
        )
        from .models import GeneralQuestion

        question = workspace.raise_general_question(
            GeneralQuestion(
                id=f"gq-anomaly-{workspace.version + 1:03d}",
                kind="execution_anomaly",
                subject=order_id,
                question=(
                    f"Order {order_id} ended in {report.status.value}."
                    " Resume after re-checks, issue a new decision, or cancel?"
                ),
                context=report.observations,
                source_version=workspace.version,
            )
        )
        return {"paused": True, "question_id": question.id}

    # ------------------------------------------------------------------
    # After-action review and controlled memory (doc 11 §7 P5)

    def conduct_review(self, workspace: ScenarioWorkspace, *, scope: str = "situation", topic_id: str | None = None) -> Review:
        """Compose a deterministic after-action review from the event timeline
        and propose experience candidates (proposed, not yet influential)."""
        entries = workspace.timeline()
        findings: list[str] = []
        kinds = [entry["kind"] for entry in entries]
        if "ExecutionSimulated" in kinds and "VerificationFinished" in kinds:
            verification_failed = any(
                entry["kind"] == "VerificationFinished"
                and entry["payload"].get("verification", {}).get("passed") is False
                for entry in entries
            )
            if verification_failed:
                findings.append(
                    "业务核验未通过仍继续执行；不能把执行成功当作充分依据。"
                )
        if "OrderAnomalyPaused" in kinds:
            findings.append(
                "异常已暂停命令；重试前先核验条件。"
            )
        research_unknowns: dict[str, int] = {}
        for assessment in workspace.assessments:
            if assessment.agent == "ResearchAgent":
                for unknown in assessment.unknowns:
                    research_unknowns[unknown] = research_unknowns.get(unknown, 0) + 1
        repeated = tuple(sorted(subject for subject, count in research_unknowns.items() if count >= 3))
        if repeated:
            findings.append(
                f"侦察反复列出同一批未知项、无进展：{'、'.join(_subject_cn(s) for s in repeated)}。"
            )
        if not any(assessment.advisor_role is not None and assessment.advisor_role.value == "red_team" for assessment in workspace.assessments):
            findings.append("本轮范围内未召集反方审查。")
        if not findings:
            findings.append("未发现偏离既定决断与执行模式的异常。")

        review = Review(
            id=f"review-{workspace.version + 1:03d}",
            topic_id=topic_id,
            scope=scope,
            findings=tuple(findings),
            entries_replayed=len(entries),
            created_by="ChiefAdvisor",
            source_version=workspace.version,
        )
        workspace.record_review(review)
        domain_tag = self.council.playbook.name
        for index, finding in enumerate(findings, start=1):
            workspace.propose_memory_candidate(
                MemoryCandidate(
                    id=f"mem-{review.id}-{index:02d}",
                    content=finding,
                    source_review_id=review.id,
                    applicability_boundary=(domain_tag,),
                    source_version=workspace.version,
                )
            )
        return review

    def confirm_memory_candidate(self, workspace: ScenarioWorkspace, candidate_id: str, *, reject: bool = False):
        """Confirmation is a general act routed through the authenticated entry."""
        return workspace.confirm_memory_candidate(
            candidate_id,
            actor_role=DecisionActor.COMMANDER,
            confirmed_by=GENERAL_ENTRY_ID,
            reject=reject,
        )

    def retrieve_relevant_experiences(self, workspace: ScenarioWorkspace) -> tuple[MemoryCandidate, ...]:
        """Confirmed experiences relevant to the live situation, ranked.

        Inclusion rule is unchanged: the candidate's applicability boundary
        must intersect the playbook tag or the claim domains behind active
        options -- boundary-disjoint candidates never surface. Ordering is a
        deterministic two-tier lexical score: boundary hits dominate, then
        overlap between the candidate text and the active situation
        vocabulary (option titles/summaries/risks plus full claim subjects);
        equal scores break by candidate id so repeated calls return the same
        sequence. This is deliberately coarse lexical matching; semantic
        retrieval stays future work until a local embedding base exists.
        """
        context_tags = {self.council.playbook.name}
        context_parts: list[str] = [self.council.playbook.name]
        for option in workspace.options.values():
            if option.status != "active":
                continue
            context_parts.extend((option.title, option.summary))
            context_parts.extend(option.risks)
            for subject in option.claim_subjects:
                if ":" in subject:
                    context_tags.add(subject.split(":", 1)[0])
                context_parts.append(subject)
        context_tokens = _lexical_tokens(" ".join(context_parts))
        scored: list[tuple[int, int, str, MemoryCandidate]] = []
        for candidate in workspace.memory_candidates.values():
            if candidate.status.value != "confirmed":
                continue
            boundary_hits = len(set(candidate.applicability_boundary) & context_tags)
            if boundary_hits == 0:
                continue
            lexical_hits = len(_lexical_tokens(candidate.content) & context_tokens)
            scored.append((-boundary_hits, -lexical_hits, candidate.id, candidate))
        scored.sort(key=lambda item: item[:3])
        return tuple(item[3] for item in scored)

    def review_orchestration_quality(
        self,
        workspace: ScenarioWorkspace,
        *,
        max_tasks_per_goal: int = 20,
    ) -> tuple[OrchestrationFeedback, ...]:
        """Rule-based staffing-quality signals over the current workspace."""
        feedbacks: list[OrchestrationFeedback] = []
        version = workspace.version

        research_unknown_counts: dict[str, int] = {}
        for assessment in workspace.assessments:
            if assessment.agent == "ResearchAgent":
                for unknown in assessment.unknowns:
                    research_unknown_counts[unknown] = research_unknown_counts.get(unknown, 0) + 1
        repeated = tuple(sorted(s for s, n in research_unknown_counts.items() if n >= 3))
        if repeated:
            feedbacks.append(
                OrchestrationFeedback(
                    id=f"ofb-{version}-repeated-research",
                    kind="repeated_research",
                    detail=f"同一批未知项被重复侦察、无进展：{'、'.join(_subject_cn(s) for s in repeated)}。",
                    source_version=version,
                )
            )

        has_red_team = any(
            assessment.advisor_role is not None and assessment.advisor_role.value == "red_team"
            for assessment in workspace.assessments
        )
        if workspace.assessments and not has_red_team:
            feedbacks.append(
                OrchestrationFeedback(
                    id=f"ofb-{version}-missing-red-team",
                    kind="missing_red_team",
                    detail="已有评估但缺少反方质疑环节。",
                    source_version=version,
                )
            )

        executions = sum(1 for event in workspace.events if event.kind == "ExecutionSimulated")
        escalations = [q for q in workspace.general_questions.values() if q.kind == "execution_anomaly"]
        non_anomaly_escalations = [
            q
            for q in workspace.general_questions.values()
            if q.kind != "execution_anomaly" and q.status == "open"
        ]
        if non_anomaly_escalations and executions == 0:
            feedbacks.append(
                OrchestrationFeedback(
                    id=f"ofb-{version}-premature-escalation",
                    kind="premature_escalation",
                    detail="A question was escalated before any execution attempt.",
                    source_version=version,
                )
            )
        del escalations

        if len(workspace.staff_tasks) > max_tasks_per_goal:
            feedbacks.append(
                OrchestrationFeedback(
                    id=f"ofb-{version}-overwork",
                    kind="overwork",
                    detail=(
                        f"{len(workspace.staff_tasks)} staff tasks exceed the budget of"
                        f" {max_tasks_per_goal} for this goal."
                    ),
                    source_version=version,
                )
            )

        for feedback in feedbacks:
            workspace.record_orchestration_feedback(feedback)
        return tuple(feedbacks)

    # ------------------------------------------------------------------
    # Targeted re-staffing after a question is answered (doc 11 §7 P7-T4)

    _KIND_TASKING = {
        "value_tradeoff": (StaffWorkType.OPTION_DESIGN, "OptionAgent", AdvisorRole.OPTION),
        "insufficient_evidence": (StaffWorkType.FACT_GAP, "ResearchAgent", AdvisorRole.RESEARCH),
        "key_disagreement": (StaffWorkType.RISK_CHALLENGE, "RedTeamAgent", AdvisorRole.RED_TEAM),
    }

    def handle_question_answer(
        self,
        workspace: ScenarioWorkspace,
        question_id: str,
        answer: str,
        *,
        actor_role: DecisionActor,
        answered_by: str,
    ) -> dict:
        """Answer a question, then re-staff ONLY the work its kind implies.

        No blanket invalidation: unrelated proposals and tasks stay untouched.
        Each kind maps to exactly one staff task whose completion produces a
        targeted assessment; everything stays on the audit trail.
        """
        answered = workspace.answer_general_question(
            question_id, answer, actor_role=actor_role, answered_by=answered_by
        )
        result = {
            "question_id": question_id,
            "kind": answered.kind,
            "plan_id": None,
            "task_id": None,
            "assessment_id": None,
        }

        tasking = self._KIND_TASKING.get(answered.kind)
        if tasking is None:
            workspace.append_event(
                "QuestionAnswerApplied", question_id=question_id, rerouted=False
            )
            return result

        work_type, owner_agent, owner_role = tasking
        plan_id = f"plan-qa-{workspace.version + 1:03d}"
        task_id = f"task-qa-{workspace.version + 1:03d}"
        task = StaffTask(
            id=task_id,
            plan_id=plan_id,
            owner_agent=owner_agent,
            owner_role=owner_role,
            work_type=work_type,
            question_to_answer=f"处理指挥员对 {question_id} 的答复：{answer}",
            expected_deliverable="针对性跟进评估",
            dependencies=(),
            status=StaffTaskStatus.PENDING,
        )
        plan = StaffPlan(
            id=plan_id,
            topic_id=None,
            created_by="ChiefAdvisor",
            question=f"针对已答复请示 {question_id}（{answered.kind}）的跟进",
            rationale_enabled="指挥员答复后的针对性重新调度",
            tasks=(task,),
            source_version=workspace.version,
        )
        workspace.record_staff_plan(plan)

        open_subjects = tuple(
            gap.subject
            for gap in workspace.information_gaps.values()
            if gap.status == InformationGapStatus.OPEN
        )
        if answered.kind == "insufficient_evidence":
            unknowns = open_subjects or (question_id,)
            risks: tuple[str, ...] = ()
        elif answered.kind == "key_disagreement":
            unknowns = ()
            risks = tuple(
                f"Re-examine disagreement on '{d.topic}' after commander guidance."
                for d in workspace.disagreements
            ) or (f"Re-examine the challenged assumption behind {question_id}.",)
        else:  # value_tradeoff
            unknowns = ()
            risks = ()

        suffix = f"{workspace.version + 1:03d}"
        assessment = Assessment(
            id=f"qa-{owner_role.value}-{suffix}",
            agent=owner_agent,
            summary=(
                f"Targeted {work_type.value} follow-up after the commander answered"
                f" {question_id}: {answer}"
            ),
            evidence_ids=(),
            risks=risks,
            unknowns=unknowns,
            advisor_role=owner_role,
            topic_id=None,
            staff_task_id=task_id,
        )
        workspace.record_assessment(assessment)
        workspace.complete_staff_task(task_id, (assessment.id,))
        workspace.append_event(
            "QuestionAnswerApplied",
            question_id=question_id,
            rerouted=True,
            plan_id=plan_id,
            task_id=task_id,
            assessment_id=assessment.id,
        )
        result.update(
            plan_id=plan_id, task_id=task_id, assessment_id=assessment.id
        )
        return result

    # ------------------------------------------------------------------
    # Classified general input routing (doc 11 §4.3)

    def apply_general_input(self, workspace: ScenarioWorkspace, feedback, proposed_option=None) -> dict:
        """Record an unrecorded CommanderFeedback and route it by its input class.

        Formal corrections invalidate downstream drafts and re-orchestrate;
        clarifications answer questions without invalidating anything;
        casual talk is recorded and nothing else. A PROPOSE input carries the
        general's own option, which becomes a first-class option (P3 acceptance).
        """
        if feedback.feedback_type == FeedbackType.PROPOSE and feedback.input_class != InputClass.FORMAL_CORRECTION:
            raise ValueError("只有正式更正才能承载指挥员自提方案。")
        workspace.record_feedback(feedback)
        result = {
            "feedback_id": feedback.id,
            "input_class": feedback.input_class.value,
            "invalidated": (),
            "reorchestrated": False,
        }
        if feedback.input_class == InputClass.FORMAL_CORRECTION:
            result["invalidated"] = workspace.invalidate_downstream(feedback.message)
            if feedback.feedback_type == FeedbackType.PROPOSE:
                if proposed_option is None:
                    raise ValueError("自提方案输入必须携带指挥员自己的方案。")
                workspace.add_commander_option(
                    proposed_option,
                    actor_role=feedback.actor_role,
                    decided_by=feedback.decided_by,
                )
                result["commander_option_id"] = proposed_option.id
            self.orchestrate(workspace, f"指挥员更正后的重新研判：{feedback.message}")
            result["reorchestrated"] = True
        elif feedback.feedback_type == FeedbackType.REQUEST_RESEARCH:
            workspace.record_information_gap(
                InformationGap(
                    id=f"gap-research-{feedback.id}",
                    subject=f"research-request:{feedback.option_id or feedback.topic_id or 'general'}",
                    reason=feedback.message,
                    discovered_by="GeneralRequest",
                    source_version=workspace.version,
                )
            )
        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _latest_risks(workspace: ScenarioWorkspace) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                risk
                for assessment in workspace.assessments[-8:]
                for risk in assessment.risks
            )
        )
