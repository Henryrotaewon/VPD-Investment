from magi2.entry_policy import assess_entry, rank_entry_candidates

CFG = {
    'entry': {
        'min_vpd': 70,
        'min_entry_score': 55,
        'hard_max_giveback_pct_point': 25,
        'hard_max_1d_pct': 20,
        'hard_min_1d_pct': -12,
        'watch_penalty': 10,
    }
}

def row(**kw):
    base = {
        'coin': 'X',
        'Rank': 1,
        'VPD': 80,
        'VPDVelocity': 10,
        'momentum': '↑',
        '1D%': 1,
        '1H%': 0.2,
        'Giveback%p': 2,
        'TodayValue/10': 5,
        'IntraAccel': 5,
        'DistributionRisk': 'CLEAR',
        'SpikeCollapse': False,
        'trigger': '-',
        'Rocket': True,
        'NEW_TOP10': True,
    }
    base.update(kw)
    return base

def test_skr_is_hard_blocked():
    skr = row(
        coin='SKR', Rank=4, VPD=81, VPDVelocity=15, momentum='↑',
        **{'1D%': 0.35, '1H%': 1.4, 'Giveback%p': 27.78,
           'TodayValue/10': 5.33, 'IntraAccel': 4.22,
           'DistributionRisk': 'HIGH'}
    )
    result = assess_entry(skr, CFG)
    assert result['eligible'] is False
    assert 'DISTRIBUTION_HIGH' in result['reasons']
    assert 'GIVEBACK_HARD' in result['reasons']

def test_usde_passes_and_outranks_watch_chase():
    usde = row(
        coin='USDE', Rank=1, VPD=93, VPDVelocity=57, momentum='↑',
        trigger='A',
        **{'1D%': -0.37, '1H%': -0.07, 'Giveback%p': 0.44,
           'TodayValue/10': 7.43, 'IntraAccel': 12.64,
           'DistributionRisk': 'CLEAR'}
    )
    wet = row(
        coin='WET', Rank=6, VPD=78, VPDVelocity=20, momentum='↑',
        **{'1D%': 8.42, '1H%': 0.42, 'Giveback%p': 7.62,
           'TodayValue/10': 30.26, 'IntraAccel': 18.78,
           'DistributionRisk': 'WATCH'}
    )
    ranked = rank_entry_candidates([wet, usde], CFG)
    assert ranked[0][0]['coin'] == 'USDE'
    assert ranked[0][1]['eligible'] is True
    assert ranked[1][1]['eligible'] is False

def test_spike_collapse_never_enters():
    result = assess_entry(row(SpikeCollapse=True), CFG)
    assert result['eligible'] is False
    assert 'SPIKE_COLLAPSE' in result['reasons']


def test_giveback_penalty_starts_after_six():
    six = assess_entry(row(**{'Giveback%p': 6.0}), CFG)
    nine = assess_entry(row(**{'Giveback%p': 9.0}), CFG)
    twelve = assess_entry(row(**{'Giveback%p': 12.0}), CFG)
    fifteen = assess_entry(row(**{'Giveback%p': 15.0}), CFG)
    twenty = assess_entry(row(**{'Giveback%p': 20.0}), CFG)
    assert six['components']['giveback'] == 10.0
    assert nine['components']['giveback'] == 8.0
    assert twelve['components']['giveback'] == 6.0
    assert fifteen['components']['giveback'] == 4.0
    assert twenty['components']['giveback'] == 2.0

def test_giveback_twenty_five_is_hard_block():
    result = assess_entry(row(**{'Giveback%p': 25.0}), CFG)
    assert result['eligible'] is False
    assert 'GIVEBACK_HARD' in result['reasons']
