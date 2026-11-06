"""Export an MK-compatible pair strategy or run the real MK minute executor.

Run from the repository root with ``python -m scripts.backtest_pair_ratio``.
Only public, native 1m OHLCV is accepted. No exchange account or database is used.
Historical experiments in the web app use the same backend.engine.run_backtest.
"""
from __future__ import annotations
import argparse, ast, csv, gzip, hashlib, json, pprint
from datetime import datetime, timezone
from pathlib import Path

from backend.engine import run_backtest
from backend.strategy import validate_strategy_source

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'backend/strategies/btc_eth_usdc_ratio.py'

def export_source(interval='1h',lookback=168,entry_z=3.0,exit_z=.25):
    if interval not in {'15m','1h','4h','1d'} or not 2<=lookback<=2880 or not 0<exit_z<entry_z<=10:
        raise ValueError('Require supported interval, lookback 2..2880, and 0 < exit_z < entry_z <= 10')
    source=SOURCE.read_text()
    tree=ast.parse(source)
    node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='MANIFEST' for t in n.targets))
    manifest=ast.literal_eval(node.value)
    manifest.update(id=f'btc_eth_usdc_ratio_{interval}',name=f'BTC/ETH 比值回归 · {interval} · USDC',
        description=f'BTCUSDC/ETHUSDC原始比率；{interval}闭合信号，默认{lookback}/{entry_z}/{exit_z}；下一分钟逐腿执行。账户杠杆2–100倍、目标gross1x；模拟费率/滑点0。MK不计资金费，研究排名已使用两段数据。',
        history_window=max(720,lookback),timeframes={'primary':interval,'additional':[]})
    props=manifest['parameters']['properties']
    props['lookback'].update(default=lookback,maximum=max(720,lookback))
    props['entry_z']['default']=entry_z
    props['exit_z']['default']=exit_z
    lines=source.splitlines(keepends=True)
    replacement='MANIFEST = '+pprint.pformat(manifest,width=110,sort_dicts=False)+'\n'
    result=''.join(lines[:node.lineno-1])+replacement+''.join(lines[node.end_lineno:])
    validate_strategy_source(result)
    return result

def read_minutes(path,start=None,end=None):
    opener=gzip.open if path.suffix=='.gz' else open
    rows=[]
    with opener(path,'rt',newline='') as f:
        for row in csv.DictReader(f):
            t=int(row['timestamp'])
            if start is not None and t<start or end is not None and t>=end: continue
            rows.append({'timestamp':t,**{k:float(row[k]) for k in ['open','high','low','close','volume']}})
    return rows

def date_ms(s):
    if not s:return None
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
    return int(d.timestamp()*1000)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interval',choices=['15m','1h','4h','1d'],default='1h')
    parser.add_argument('--lookback',type=int,default=168)
    parser.add_argument('--entry-z',type=float,default=3.0)
    parser.add_argument('--exit-z',type=float,default=.25)
    parser.add_argument('--capital',type=float,default=10000)
    parser.add_argument('--leverage',type=int,choices=range(2,101),metavar='2..100',default=2)
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--filters',type=Path)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--export-only',action='store_true')
    parser.add_argument('--start');parser.add_argument('--end')
    args=parser.parse_args()
    source=export_source(args.interval,args.lookback,args.entry_z,args.exit_z)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    source_path=args.output_dir/f'btc_eth_usdc_ratio_{args.interval}.py'
    source_path.write_text(source)
    if args.export_only:
        print(f'Validated MK upload source: {source_path}')
        return
    if args.data_dir is None:parser.error('--data-dir is required unless --export-only')
    filter_path=args.filters or args.data_dir/'exchange_info.json'
    info=json.loads(filter_path.read_text())
    # Public REST uses a filter list inside each symbol. The MK executor takes
    # that list directly (or its client's normalized dictionary), not the raw
    # exchangeInfo symbol wrapper.
    filters={s['symbol']:s['filters'] if isinstance(s.get('filters'),list) else s for s in info['symbols']}
    candles={s:read_minutes(args.data_dir/f'{s}-1m.csv.gz',date_ms(args.start),date_ms(args.end)) for s in ['BTCUSDC','ETHUSDC']}
    if any(not rows for rows in candles.values()):raise ValueError('Empty minute data')
    params={'lookback':args.lookback,'entry_z':args.entry_z,'exit_z':args.exit_z}
    output=run_backtest(source,params,candles,args.capital,0.0,0.0,market='futures',leverage=args.leverage,symbol_filters=filters,base_interval='1m')
    with gzip.open(args.output_dir/'equity.csv.gz','wt',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=output['equity'][0].keys());writer.writeheader();writer.writerows(output['equity'])
    (args.output_dir/'orders.json').write_text(json.dumps(output['orders'],ensure_ascii=False,indent=2))
    with gzip.open(args.output_dir/'decisions.json.gz','wt') as f:json.dump(output['decisions'],f,ensure_ascii=False)
    rejected=[r for d in output['decisions'] for r in d['risk'] if r['status']=='rejected']
    summary={k:output[k] for k in ['metrics','positions','account','engine_version','status','parameters','assumptions']}
    summary.update(source_sha256=hashlib.sha256(source.encode()).hexdigest(),minute_rows_per_symbol={s:len(rows) for s,rows in candles.items()},
        first_open_ms=candles['BTCUSDC'][0]['timestamp'],end_ms_exclusive=candles['BTCUSDC'][-1]['timestamp']+60000,
        rejection_count=len(rejected),rejections=rejected,
        note='Framework semantics: cold indicator warmup; quantity at signal close; sequential next-minute fills; funding excluded; terminal positions retained.')
    (args.output_dir/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False))
    print(json.dumps({'metrics':summary['metrics'],'rejection_count':len(rejected),'orders':len(output['orders']),'output_dir':str(args.output_dir)},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
