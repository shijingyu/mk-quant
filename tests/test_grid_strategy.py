import copy
import json
from pathlib import Path

import pytest

from backend.strategy import load_strategy, validate_parameters, validate_strategy_source, validate_symbols

SOURCE = (Path(__file__).parents[1] / 'backend/strategies/usdc_hedge_grid.py').read_text()
MANIFEST = validate_strategy_source(SOURCE)
SYMBOLS = MANIFEST['symbols']['values']


def context(equity=10000):
    prices = dict(zip(SYMBOLS, (90000, 3000, 150, 600, 1.5)))
    return {'params':validate_parameters(MANIFEST, {}), 'candles':{s:{'close':p} for s,p in prices.items()},
            'positions':{}, 'open_orders':[], 'lots':[],
            'account':{'position_mode':'hedge','equity':equity,'margin_used':0,'pending_margin':0,'leverage':1},
            'emit':lambda **fields:None,'log':lambda *args:None}


def test_manifest_has_five_usdc_symbols_and_rejects_incompatible_models():
    validate_symbols(MANIFEST, ['BTCUSDC','XRPUSDC'])
    assert MANIFEST['account']['position_modes'] == ['hedge']
    assert MANIFEST['execution']['model'] == 'maker_grid_v1'
    with pytest.raises(ValueError): validate_symbols(MANIFEST,['BTCUSDT'])
    with pytest.raises(ValueError): validate_parameters(MANIFEST, {'levels_per_side':True})
    with pytest.raises(ValueError): validate_strategy_source(SOURCE.replace('["hedge"]','["one_way"]'))


def test_equal_ratio_grids_make_both_legs_for_all_coins_with_bound_brackets():
    ctx=context(); orders=load_strategy(SOURCE).on_bar(ctx)
    assert len(orders)==40
    for symbol in SYMBOLS:
        rows=[o for o in orders if o['symbol']==symbol]
        longs=[o for o in rows if o['position_side']=='LONG']; shorts=[o for o in rows if o['position_side']=='SHORT']
        assert len(longs)==len(shorts)==4
        assert longs[0]['price']/longs[1]['price']==pytest.approx(1.006)
        assert shorts[1]['price']/shorts[0]['price']==pytest.approx(1.006)
        for o in rows:
            b=o['bracket'];p=o['price']
            assert o['type']=='LIMIT' and o['time_in_force']=='GTX'
            assert o['quantity']*p==pytest.approx(100)
            if o['position_side']=='LONG':
                assert b['stop_price']<b['stop_limit_price']<p<b['take_profit_price']
            else:
                assert b['take_profit_price']<p<b['stop_limit_price']<b['stop_price']


def test_limited_budget_is_shared_across_symbols_and_legs():
    orders=load_strategy(SOURCE).on_bar(context(2500))
    assert len(orders)==10
    assert {(o['symbol'],o['position_side']) for o in orders} == {(s,side) for s in SYMBOLS for side in ('LONG','SHORT')}
    assert sum(o['price']*o['quantity'] for o in orders)<=1250


def test_active_grid_levels_are_not_duplicated_and_state_survives_restore():
    strategy=load_strategy(SOURCE);ctx=context();orders=strategy.on_bar(ctx)
    ctx['open_orders']=[dict(o,id=o['client_order_id'],role='entry') for o in orders]
    assert strategy.on_bar(ctx)==[]
    restored=load_strategy(SOURCE);restored.__dict__.update(json.loads(json.dumps(strategy.__dict__)))
    # One closed lot frees just its corresponding level, with a new stable ID.
    ctx['open_orders']=ctx['open_orders'][1:]
    repeated=restored.on_bar(copy.deepcopy(ctx))
    assert len(repeated)==1 and repeated[0]['client_order_id']!=orders[0]['client_order_id']
    assert repeated[0]['client_order_id'].rsplit('-',1)[0]==orders[0]['client_order_id'].rsplit('-',1)[0]


def test_unprotected_lot_cancels_only_entries_and_does_not_fake_a_fill():
    ctx=context();ctx['lots']=[{'id':'lot','symbol':'BTCUSDC','position_side':'LONG','remaining_quantity':.001,'protection_status':'stop_rejected'}]
    ctx['open_orders']=[{'id':'entry','symbol':'BTCUSDC','role':'entry'},{'id':'tp','symbol':'BTCUSDC','role':'tp'}]
    intents=load_strategy(SOURCE).on_bar(ctx)
    assert len(intents)==1 and intents[0]['action']=='cancel' and intents[0]['order_id']=='entry'
    assert ctx['lots'][0]['remaining_quantity']==.001


def test_stop_limit_offset_cannot_cross_back_over_entry_price():
    ctx=context()
    ctx['params'].update(grid_spacing_pct=.05, stop_loss_steps=1, stop_limit_offset_bps=100)
    with pytest.raises(ValueError, match='偏移必须小于止损距离'):
        load_strategy(SOURCE).on_bar(ctx)
