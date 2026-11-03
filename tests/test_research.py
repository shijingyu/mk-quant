import copy
import math
from pathlib import Path

import pytest

from backend import storage
from backend.engine import run_backtest
from backend.research import CandleWindow, DiskSequence, ExtremaPreview, ResearchSpace


def test_disk_sequence_tail_updates_across_blocks(tmp_path):
    seq = DiskSequence(tmp_path)
    expected = []
    for i in range(1000):
        row = {'i': i, 'nested': {'risk': []}}
        seq.append(row)
        seq[-1]['nested']['risk'].append(i)
        expected.append(copy.deepcopy(row))
    assert len(seq) == 1000
    assert list(seq) == expected
    assert seq[55:135] == expected[55:135]
    assert seq[0] == expected[0]
    assert len(seq.tail) <= seq.BLOCK
    seq.close()


@pytest.mark.parametrize('strategy,symbols,params', [
    ('momentum', ['BTCUSDT','ETHUSDT'], {'fast_period':2,'slow_period':4}),
    ('spot_dca', ['BTCUSDT'], {}),
    ('usdc_hedge_grid', ['BTCUSDC','ETHUSDC','SOLUSDC','BNBUSDC','XRPUSDC'], {}),
])
def test_streamed_execution_matches_in_memory_every_record(tmp_path, monkeypatch, strategy, symbols, params):
    monkeypatch.setenv('MK_ARTIFACT_ROOT', str(tmp_path / 'artifacts'))
    source = (Path(__file__).parents[1] / 'backend/strategies' / (strategy + '.py')).read_text()
    start = 1767225600000
    candles = {}
    # Several block boundaries, signals, closing trades and daily Sharpe samples.
    for offset, symbol in enumerate(symbols):
        candles[symbol] = []
        for i in range(3*1440):
            price=100*(offset+1)*(1+.07*math.sin(i/47))
            candles[symbol].append({'timestamp':start+i*60000,'open':price,'high':price*1.015,'low':price*.985,'close':price*1.001,'volume':100})
    rules = {s: {'tick_size':'.01','step_size':'.001','min_qty':'.001','min_notional':'5'} for s in symbols}
    options = dict(market='spot' if strategy=='spot_dca' else 'futures', symbol_filters=rules)
    dense = run_backtest(source, params, candles, 10000, .0002, 0, **options)
    manifest = storage.write_candles(candles)
    feed = CandleWindow(manifest,symbols,start,start+3*1440*60000)
    space = ResearchSpace(storage.artifact_root())
    try:
        streamed = run_backtest(source, params, feed, 10000, .0002, 0, research_space=space, **options)
        assert streamed['metrics'] == dense['metrics']
        assert streamed['account'] == dense['account']
        for key in ('equity','orders','decisions'):
            assert list(streamed[key]) == dense[key], key
        for symbol in symbols:
            assert streamed['symbol_results'][symbol]['metrics'] == dense['symbol_results'][symbol]['metrics']
            assert list(streamed['symbol_results'][symbol]['equity']) == dense['symbol_results'][symbol]['equity']
        rows = streamed['decisions']
        saved = storage.write_records('decisions', iter(rows))
        assert storage.read_records(saved) == dense['decisions']
    finally:
        feed.close()
        space.close()
    assert not list(storage.artifact_root().glob('.research-*'))


def test_running_preview_is_bounded_and_preserves_extrema():
    preview = ExtremaPreview(100000)
    for i in range(100000):
        preview.extend([{'timestamp':i,'equity':float(i % 113)-50}])
    points=preview.points()
    assert len(points) <= 604
    assert points[0]['timestamp']==0 and points[-1]['timestamp']==99999
    assert min(p['equity'] for p in points)==-50
    assert max(p['equity'] for p in points)==62


def test_terminal_order_ids_remain_idempotent_on_disk(tmp_path):
    from backend.engine import Ledger, MakerOrderBook
    from decimal import Decimal
    space=ResearchSpace(tmp_path)
    try:
        book=MakerOrderBook(Ledger(1000,'futures',1,0,position_mode='hedge'))
        book.orders=space.mapping(active=lambda o:o['status'] in book.OPEN)
        book.lots=space.mapping(active=lambda lot:Decimal(lot['remaining_quantity'])>0)
        book._orders_by_lot=space.mapping()
        original={'symbol':'BTCUSDT','client_order_id':'rejected-0','side':'buy','position_side':'LONG','type':'MARKET','quantity':1}
        first=book.submit(original,0,{'BTCUSDT':100})
        for i in range(5000):
            book.submit(dict(original,client_order_id='rejected-'+str(i)),i,{'BTCUSDT':100})
        assert not book.orders.live
        assert len(book.orders)==5000
        assert book.submit(original,6000,{'BTCUSDT':100})==first
        assert book.lots.get(None) is None
        assert len(book._lot_orders('rejected-0'))==1
    finally:
        space.close()


def test_disk_map_collision_chain_and_replacement(tmp_path, monkeypatch):
    from backend.research import DiskMap
    monkeypatch.setattr(DiskMap, 'BUCKETS', 2)
    index=DiskMap(tmp_path)
    try:
        for i in range(300):
            index[str(i)]={'value':i}
        index['0']={'value':-1}
        index.cache.clear()
        assert len(index)==300
        assert index['0']=={'value':-1}
        assert all(index[str(i)]=={'value':i} for i in range(1,300))
        assert index.get('missing') is None
        assert len(index.cache)<=256
    finally:
        index.close()


def test_cancel_hashing_does_not_publish_untracked_file(tmp_path, monkeypatch):
    monkeypatch.setenv('MK_ARTIFACT_ROOT',str(tmp_path))
    def cancel(*args, **kwargs):
        raise RuntimeError('cancelled')
    monkeypatch.setattr(storage,'_digest_file',cancel)
    with pytest.raises(RuntimeError,match='cancelled'):
        storage.write_records('decisions',[{'timestamp':1,'reason':'test'}])
    assert not list(tmp_path.rglob('*.parquet'))
