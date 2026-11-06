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
    monkeypatch.setattr(c,'_state',{'windows':{'old':True},'updated_at':123,'refreshing':True})
    class Broken:
        def __init__(self,**kwargs):pass
        def _request(self,*args):raise ValueError('network')
    monkeypatch.setattr(c,'BinanceClient',Broken)
    c._refresh()
    assert c._state['windows']=={'old':True}
    assert c._state['updated_at']==123 and c._state['error'] and not c._state['refreshing']
