"""Hedge adapters preserve per-leg exposure and never hide opposing positions."""
from pathlib import Path
import pytest
from backend.strategy import load_strategy,validate_strategy_source

ROOT=Path(__file__).parents[1]/'backend/strategies'
FILES=['btc_eth_usdc_ratio','doge_shib_usdc_ols_15m','sol_ada_usdc_ols_15m']

@pytest.mark.parametrize('name',FILES)
@pytest.mark.parametrize('direction',[1,-1])
def test_pair_adapter_translates_entry_and_exit_legs(name,direction):
    source=(ROOT/(name+'.py')).read_text();m=validate_strategy_source(source)
    assert m['account']['position_modes']==['hedge'] and m['account']['default_position_mode']=='hedge'
    s=load_strategy(source);a,b=m['symbols']['values'];captured=[]
    def decision(ctx):
        captured.append(ctx['positions'])
        if ctx['positions']:
            return [{'symbol':sym,'side':'sell' if p['quantity']>0 else 'buy','quantity':abs(p['quantity']),'reduce_only':True} for sym,p in ctx['positions'].items()]
        return [{'symbol':a,'side':'buy' if direction>0 else 'sell','quantity':2}, {'symbol':b,'side':'sell' if direction>0 else 'buy','quantity':3}]
    s.pair_decision=decision
    ctx={'positions':{},'account':{'position_mode':'hedge'},'emit':lambda **kw:None}
    entries=s.on_bar(ctx)
    assert [o['position_side'] for o in entries]==(['LONG','SHORT'] if direction>0 else ['SHORT','LONG'])
    ctx['positions']={a+(':'+entries[0]['position_side']):{'quantity':direction*2},b+(':'+entries[1]['position_side']):{'quantity':-direction*3}}
    exits=s.on_bar(ctx)
    assert all(o['reduce_only'] for o in exits)
    assert [o['position_side'] for o in exits]==[o['position_side'] for o in entries]
    assert set(captured[-1])=={a,b}
    assert all(':' in k for k in ctx['positions']) # input snapshot not mutated

@pytest.mark.parametrize('name',FILES)
def test_overlap_closes_each_actual_leg_instead_of_hiding_net_zero(name):
    source=(ROOT/(name+'.py')).read_text();m=validate_strategy_source(source);s=load_strategy(source)
    a,b=m['symbols']['values']
    s.pair_decision=lambda ctx:pytest.fail('overlapping exposure must not enter signal core')
    intents=s.on_bar({'positions':{a+':LONG':{'quantity':2},a+':SHORT':{'quantity':-2},b+':SHORT':{'quantity':-3}},'account':{'position_mode':'hedge'},'emit':lambda **kw:None})
    assert len(intents)==3 and all(o['reduce_only'] for o in intents)
    assert {(o['symbol'],o['position_side']) for o in intents}=={(a,'LONG'),(a,'SHORT'),(b,'SHORT')}
