import pytest
from scripts.warmup_account import build_frames

class PublicMinutes:
    def __init__(self,gap=False):self.gap=gap;self.calls=[]
    def iter_klines(self,symbol,interval,start,end,**kwargs):
        self.calls.append((symbol,interval,start,end))
        for begin in range(start,end,60*60000):
            yield [{'timestamp':t,'open':100,'high':102,'low':99,'close':101,'volume':1}
                   for t in range(begin,min(end,begin+60*60000),60000) if not(self.gap and t==start+60000)]

def test_warmup_aggregates_only_closed_primary_bars_from_minutes():
    client=PublicMinutes();end=1767225600000+10*3600000+20*60000
    frames=build_frames({'history_window':5,'timeframes':{'primary':'1h','additional':[]}},['BTCUSDC','ETHUSDC'],3,end,client)
    assert len(frames.history['1h']['BTCUSDC'])==4
    assert frames.history['1h']['BTCUSDC'][-1]['close_time']==end-20*60000
    assert frames.pending['1h']['count']==20
    assert all(call[1]=='1m' for call in client.calls)
    assert frames.last_timestamp==end-60000

def test_warmup_fails_on_missing_minutes():
    with pytest.raises(ValueError,match='预热行情缺口'):
        build_frames({'history_window':5,'timeframes':{'primary':'1h','additional':[]}},['BTCUSDC'],3,1767225600000,PublicMinutes(gap=True))

from test_maker_runtime import maker, feed, command, read, MANIFEST, SOURCE
from backend import db
from backend import warmup as warmer
from backend.worker import runtime_tick
import hashlib

@pytest.mark.parametrize('model',['maker_grid_v1','market_next_open_v1'])
def test_automatic_warmup_preserves_book_and_fences_old_ticks(maker,monkeypatch,model):
    maker['start']=maker['start']//180000*180000
    manifest={**MANIFEST,'timeframes':{'primary':'3m','additional':['15m']}}
    if model=='market_next_open_v1':manifest.pop('execution')
    source=SOURCE.replace(repr(MANIFEST),repr(manifest))
    with db.connect() as con:
        con.execute('UPDATE strategies SET manifest=?,source=?,source_hash=? WHERE id=?',(db.dump(manifest),source,hashlib.sha256(source.encode()).hexdigest(),MANIFEST['id']))
    command(maker,'resume');feed(maker,0);runtime_tick(maker['id'],'actor')
    before=read(maker)['snapshot']
    with db.connect() as con:lease_before=con.execute('SELECT * FROM instances WHERE account_id=?',(maker['id'],)).fetchone()
    fence=lease_before['fence']
    monkeypatch.setattr(warmer,'BinanceClient',lambda **kwargs:PublicMinutes())
    warmer.run_pending()
    after=read(maker)['snapshot']
    for key in before:
        if key!='frames':assert after[key]==before[key],key
    for interval in ['3m','15m']:
        assert len(after['frames']['history'][interval]['BTCUSDC'])>=4
    with db.connect() as con:
        assert con.execute('SELECT fence FROM instances WHERE account_id=?',(maker['id'],)).fetchone()[0]==fence+1
        status=db.parse(con.execute("SELECT payload FROM ledger WHERE account_id=? AND kind='warmup'",(maker['id'],)).fetchone()[0])
    assert status['status']=='completed' and status['progress']==100
    with db.connect() as con:lease_after=con.execute('SELECT * FROM instances WHERE account_id=?',(maker['id'],)).fetchone()
    for key in ['owner','lease_until','last_heartbeat']:
        assert lease_after[key]==lease_before[key],key
    assert not db.execution_authorized(maker['id'],'actor',fence)
    assert db.execution_authorized(maker['id'],'actor',fence+1)
    from backend.worker import account_heartbeat_expired
    assert not account_heartbeat_expired(lease_after,0,now=maker['clock']['now'],monotonic_now=100)

    warmer.run_pending()
    with db.connect() as con:assert con.execute('SELECT fence FROM instances WHERE account_id=?',(maker['id'],)).fetchone()[0]==fence+1
    assert runtime_tick(maker['id'],'actor')
    assert read(maker)['snapshot']['frames']==after['frames']

@pytest.mark.parametrize('snapshot',[
    {'model':'market_next_open_v1','pending':[{}]},
    {'model':'market_next_open_v1','positions':{'BTC:LONG':{'quantity':'1'}}},
    {'model':'maker_grid_v1','book':{'orders':{'id':{'status':'NEW'}}}},
    {'model':'maker_grid_v1','book':{'lots':{'id':{'remaining_quantity':'1'}}}},
])
def test_active_positions_and_pending_orders_cannot_be_overwritten(snapshot):
    assert not warmer.flat_snapshot(snapshot)

def test_warmup_failure_is_visible_and_rate_limited(maker,monkeypatch):
    maker['start']=maker['start']//180000*180000
    manifest={**MANIFEST,'timeframes':{'primary':'3m','additional':[]}}
    source=SOURCE.replace(repr(MANIFEST),repr(manifest))
    with db.connect() as con:
        con.execute('UPDATE strategies SET manifest=?,source=?,source_hash=? WHERE id=?',(db.dump(manifest),source,hashlib.sha256(source.encode()).hexdigest(),MANIFEST['id']))
    command(maker,'resume');feed(maker,0);runtime_tick(maker['id'],'actor')
    before=read(maker)['snapshot'];calls=[]
    def client(**kwargs):calls.append(1);return PublicMinutes(gap=True)
    monkeypatch.setattr(warmer,'BinanceClient',client)
    warmer.run_pending();warmer.run_pending()
    assert len(calls)==1 and read(maker)['snapshot']==before
    with db.connect() as con:
        state=db.parse(con.execute("SELECT payload FROM ledger WHERE account_id=? AND kind='warmup'",(maker['id'],)).fetchone()[0])
    assert state['status']=='failed' and '缺口' in state['error']
