"""Browser draft namespace and opt-in replies; never grants business permissions."""
import hashlib
import json


DRAFT_ACTIONS = frozenset({
    '/sand/ask', '/sand/goal', '/sand/intent', '/sand/restaff-sand',
    '/sand/self-option', '/sand/observe', '/sand/annotate',
    '/sand/measure', '/sand/image-upload',
})


# Existing-object actions retain inputs only in the live page, never restore stale IDs.
FEEDBACK_ACTIONS = DRAFT_ACTIONS | frozenset({
    '/sand/image-status', '/sand/image-analyze', '/sand/image-decision',
    '/sand/annotate-geo', '/sand/annotate-status', '/sand/annotate-edit',
    '/sand/conflict-resolve', '/sand/candidate', '/sand/ai-candidate',
    '/sand/draft-confirm', '/sand/draft-discard', '/sand/review-current',
    '/sand/change-action', '/sand/gap-defer', '/sand/plan-decision',
    '/sand/task-draft',
})


def draft_context(state, role, token):
    if role not in {'commander', 'member'} or not token:
        return '', ''
    owner = hashlib.sha256(('sand-draft-v1\0' + role + '\0' + token).encode()).hexdigest()
    identity = [getattr(state, 'active_event_id', ''), state.workspace.scenario_id]
    event = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return owner, event
