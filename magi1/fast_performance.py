"""Pure reporting projection; restart anchors never rewrite account history."""


def performance_state(state):
    segment = state.get('review_segment')
    if not segment:
        return dict(state, performance_scope='LIFETIME')
    return dict(state, performance_scope='RESTART', started_ms=segment['started_ms'],
                policy=dict(state['policy'], initial=segment['equity']),
                realized=state['realized']-segment['realized'],
                closed=state['closed']-segment['closed'],
                winning=state['winning']-segment['winning'])


def daily_performance(report, state):
    """Rebase pre-deploy daily payloads too; equity itself is always authoritative."""
    current = performance_state(state)
    result = dict(report)
    result['return_pct'] = (report['equity']/current['policy']['initial']-1)*100
    segment = state.get('review_segment')
    if segment and report.get('performance_scope') != 'RESTART':
        result['realized'] = report['realized']-segment['realized']
    return result
