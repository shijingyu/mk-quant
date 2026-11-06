import math
import pytest
from backend import correlation as c


def test_log_returns_correlation_and_ols_direction():
    y=[math.exp(2+i*.01+math.sin(i)*.1) for i in range(100)]
    x=[3*v**2 for v in y]
    p=c.pair_metrics(x,y)
    assert p['correlation']==pytest.approx(1)
    assert p['beta']==pytest.approx(2)
    assert p['zscore'] is None and p['half_life'] is None
    assert c.pair_metrics([1/v for v in y],y)['correlation']==pytest.approx(-1)
    assert c.pair_metrics([1]*100,y)['correlation'] is None


def test_gaps_and_unclosed_bars_are_not_fabricated():
    rows=[[i*60000,0,0,0,str(i+1),0,(i+1)*60000-1] for i in range(5)]
    assert c.closed_prices(rows,240000,60000,4)==[1,2,3,4]
    with pytest.raises(ValueError):c.closed_prices(rows[1:],240000,60000,4)
    with pytest.raises(ValueError):c.closed_prices(rows+[rows[0]],240000,60000,4)


def test_window_uses_closed_aligned_prices_and_unique_pairs(monkeypatch):
    monkeypatch.setattr(c,'ASSETS',['BTC','ETH','XRP'])
    monkeypatch.setattr(c.time,'sleep',lambda _:None)
    class Client:
        def _request(self,method,path,params):
            assert method=='GET' and path=='/fapi/v1/klines'
            assert params['limit']==168 and params['interval']=='1h'
            start=params['startTime']
            return [[start+i*3600000,0,0,0,100+math.sin(i/3)+i,0,start+(i+1)*3600000-1] for i in range(168)]
    result=c.build_window(Client(),'7d',1800000000000,{'BTCUSDT','ETHUSDT'})
    assert result['returns']==167 and len(result['pairs'])==1
    assert result['matrix'][0][1]==result['matrix'][1][0]==pytest.approx(1)
    assert result['matrix'][2][0] is None and 'XRPUSDT' in result['missing']


def test_failed_update_retains_snapshot_but_reports_error(monkeypatch):
    monkeypatch.setattr(c,'_states',{'USDT':{'windows':{'old':True},'updated_at':123,'refreshing':True}})
    class Broken:
        def __init__(self,**kwargs):pass
        def _request(self,*args):raise ValueError('network')
    monkeypatch.setattr(c,'BinanceClient',Broken)
    c._refresh()
    assert c._states['USDT']['windows']=={'old':True}
    assert c._states['USDT']['updated_at']==123 and c._states['USDT']['error'] and not c._states['USDT']['refreshing']


def test_usdc_removes_unlisted_contracts_and_returns_aligned_series(monkeypatch):
    monkeypatch.setattr(c,'ASSETS',['BTC','ETH','XRP'])
    monkeypatch.setattr(c.time,'sleep',lambda _:None)
    class Client:
        def _request(self,method,path,params):
            assert params['symbol'] in {'BTCUSDC','ETHUSDC'}
            step=c.WINDOWS[next(k for k,v in c.WINDOWS.items() if v[1]==params['interval'])][2]
            return [[params['startTime']+i*step,0,0,0,100+math.sin(i/3)+(i if params['symbol']=='BTCUSDC' else i*.8),0,params['startTime']+(i+1)*step-1] for i in range(params['limit'])]
    for frame in c.WINDOWS:
        w=c.build_window(Client(),frame,1800000000000,{'BTCUSDC','ETHUSDC'},'USDC')
        assert w['symbols']==['BTCUSDC','ETHUSDC'] and not w['missing']
        d=c.pair_series(w,'BTCUSDC','ETHUSDC')
        assert d['interval']==c.WINDOWS[frame][1] and len(d['points'])==c.WINDOWS[frame][3]
        assert d['points'][0]['a']==d['points'][0]['b']==100
        assert d['points'][-1]['z']==pytest.approx(w['pairs'][0]['zscore'])
        assert d['half_life']==w['pairs'][0]['half_life']
        assert d['points'][-1]['time']<w['end']
        with pytest.raises(ValueError):c.pair_series(w,'BTCUSDT','ETHUSDT')


def test_quote_snapshots_are_isolated_and_hide_price_storage(monkeypatch):
    monkeypatch.setattr(c,'_states',{q:{'windows':{'7d':{'_prices':{'secret':[]},'symbols':[q]}},'updated_at':1,'refreshing':False,'error':None,'next_refresh':float('inf')} for q in ['USDT','USDC']})
    # Finite deadline for JSON-safe serialization.
    for v in c._states.values():v['next_refresh']=c.time.time()+900
    assert c.snapshot('USDC')['windows']['7d']=={'symbols':['USDC']}
    assert c.snapshot()['windows']['7d']=={'symbols':['USDT']}
    with pytest.raises(ValueError):c.snapshot('USD')


def test_funding_interval_is_observed_not_hardcoded(monkeypatch):
    monkeypatch.setattr(c.time,'sleep',lambda _:None)
    class Client:
        def _request(self,method,path,params):
            if params['symbol']=='BAD':raise ValueError('unavailable')
            return [{'fundingTime':3600000,'fundingRate':'-.001'},{'fundingTime':18000000,'fundingRate':'.0002'}]
    result=c.funding_snapshot(Client(),['BTCUSDC','BAD'],20000000)
    assert result['BTCUSDC']=={'rate':.0002,'time':18000000,'interval_hours':4}
    assert result['BAD']['rate'] is None
