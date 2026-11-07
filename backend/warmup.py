"""Preload closed indicator history from public minute bars, without replaying trades.

Run with the application's environment. Only a flat simulated account is eligible.
Uses the same minute aggregation and fences any concurrent tick before committing.
"""
import time
from backend import db
from backend.binance import BinanceClient
from backend.timeframes import MinuteFrames, INTERVAL_MINUTES, normalize_timeframes


def build_frames(manifest, symbols, count, end, client, progress=None):
    frames=MinuteFrames(symbols,manifest.get('timeframes'),manifest['history_window'])
    width=max(INTERVAL_MINUTES[t] for t in frames.intervals)*60000
    # UTC bucket alignment also keeps every requested primary bar complete.
    offset=4*86400000 if width==INTERVAL_MINUTES["1w"]*60000 else 0
    start=((end-offset)//width-count-1)*width+offset
    def rows(symbol):
        for page in client.iter_klines(symbol,'1m',start,end,on_wait=(lambda event:progress(None,None)) if progress else None):yield from page
    streams={symbol:iter(rows(symbol)) for symbol in symbols}
    for stamp in range(start,end,60000):
        bars={symbol:next(stream,None) for symbol,stream in streams.items()}
        if any(not bar or bar['timestamp']!=stamp for bar in bars.values()):
            raise ValueError('预热行情缺口，未修改账户状态')
        frames.push(bars)
        if progress and ((stamp-start)//60000)%1000==0:progress(stamp-start,end-start)
    return frames


def flat_snapshot(snapshot):
    if snapshot.get('model')=='maker_grid_v1':
        book=snapshot.get('book',{})
        if book.get('flattening') or any(o.get('status') in {'NEW','PENDING_TRIGGER'} for o in book.get('orders',{}).values()):return False
        if any(float(lot.get('remaining_quantity',0)) for lot in book.get('lots',{}).values()):return False
        ledger=book.get('ledger',{})
    elif snapshot.get('model')=='market_next_open_v1':
        if snapshot.get('pending'):return False
        ledger=snapshot
    else:return False
    return not any(float(p.get('quantity',0)) for p in ledger.get('positions',{}).values())


def warmup(account_id,count=None,progress=None):
    with db.connect() as con:
        account=con.execute('SELECT * FROM accounts WHERE id=?',(account_id,)).fetchone()
        if not account or account['mode']!='paper':raise ValueError('只支持已有模拟账户')
        strategy=con.execute('SELECT * FROM strategies WHERE id=?',(account['strategy_id'],)).fetchone()
        instance=con.execute('SELECT * FROM instances WHERE account_id=?',(account_id,)).fetchone()
    manifest=db.parse(strategy['manifest']);symbols=db.parse(account['symbols'])
    count=count or manifest['history_window']
    if not 2<=count<=manifest['history_window']:raise ValueError('预热根数超出策略历史窗口')
    timeframes=normalize_timeframes(manifest.get('timeframes'))
    # Historical bars only. Execution still waits for the next fresh signal bar.
    end=int(time.time()//60)*60000
    prepared=build_frames(manifest,symbols,count,end,BinanceClient(market=manifest['market']),progress)
    with db.connect() as con:
        current=db.lock_instance(con,account_id)
        latest=con.execute('SELECT * FROM accounts WHERE id=? FOR UPDATE',(account_id,)).fetchone()
        source=con.execute('SELECT source_hash FROM strategies WHERE id=?',(latest['strategy_id'],)).fetchone()
        if current['desired_state'] not in {'running','paused'} or current['config_revision']!=instance['config_revision'] or latest['strategy_id']!=account['strategy_id'] or source['source_hash']!=strategy['source_hash']:
            raise ValueError('预热期间配置已更改，未写入')
        row=con.execute("SELECT * FROM ledger WHERE account_id=? AND kind='snapshot' FOR UPDATE",(account_id,)).fetchone()
        snapshot=db.parse(row['payload']) if row else {}
        if not flat_snapshot(snapshot):
            raise ValueError('账户已有仓位或待执行意图，未修改状态')
        saved=snapshot.get('frames')
        if not saved or saved['symbols']!=sorted(symbols) or saved['timeframes']!=timeframes:
            raise ValueError('执行端周期状态尚未就绪，未修改状态')
        frames=MinuteFrames.load(saved)
        for interval in frames.intervals:
            for symbol in symbols:
                combined={bar['timestamp']:bar for bar in prepared.history[interval][symbol] if bar['close_time']<=snapshot['last_timestamp']+60000}
                combined.update({bar['timestamp']:bar for bar in frames.history[interval][symbol]})
                frames.history[interval][symbol]=[combined[t] for t in sorted(combined)][-manifest['history_window']:]
        available=min(len(frames.history[timeframes['primary']][s]) for s in symbols)
        if available<count:raise ValueError('已闭合历史不足，未修改状态')
        if not current['owner'] or current['lease_until']<=time.time():
            raise ValueError('执行租约已失效，预热未覆盖账户状态，等待执行端恢复')
        snapshot['frames']=frames.dump()
        con.execute('UPDATE ledger SET payload=?,created_at=? WHERE id=?',(db.dump(snapshot),db.now(),row['id']))
        # Fence any tick that read the old snapshot, but retain the active
        # executor and its real lease/heartbeat. Revocation (lease=0) here made
        # the watchdog mistake a healthy warmup handoff for an expired actor.
        con.execute('UPDATE instances SET fence=fence+1 WHERE account_id=?',(account_id,))
        metrics=db.parse(latest['metrics'],{});metrics['display']={'stage':'历史预热完成','reason':'等待下一根完整主周期 K 线进行信号判断','warmup_available':available,'warmup_required':count}
        con.execute('UPDATE accounts SET metrics=? WHERE id=?',(db.dump(metrics),account_id))
        report={'primary':timeframes['primary'],'available':available,'requested':count,'orders_replayed':0}
        db.audit(con,None,'account.history.warmup','account',account_id,report)
    db.log('debug','历史预热完成；未回放历史订单，等待下一根完整主周期信号',account_id=account_id,strategy_id=account['strategy_id'],details=report)
    return report


def needs_warmup(manifest,snapshot):
    timeframes=normalize_timeframes(manifest.get('timeframes'))
    intervals=[timeframes['primary'],*timeframes['additional']]
    if all(t=='1m' for t in intervals):return False
    history=(snapshot.get('frames') or {}).get('history',{})
    symbols=(snapshot.get('frames') or {}).get('symbols',[])
    if not symbols:return True
    count=manifest['history_window']
    for interval in intervals:
        for symbol in symbols:
            rows=history.get(interval,{}).get(symbol,[])[-count:]
            width=INTERVAL_MINUTES[interval]*60000
            if len(rows)<count or any(b['timestamp']-a['timestamp']!=width for a,b in zip(rows,rows[1:])):return True
    return False


def save_status(account_id,value):
    with db.connect() as con:
        con.execute("INSERT INTO ledger VALUES(?,?,?,?,?,?) ON DUPLICATE KEY UPDATE payload=VALUES(payload),created_at=VALUES(created_at)",(db.uid(),account_id,'warmup','warmup:'+account_id,db.dump(value),db.now()))


def run_pending():
    """One isolated warmer; MySQL advisory lock fences duplicate supervisors."""
    import hashlib
    import os
    key='mk-warmup-'+hashlib.sha256(os.environ['MK_DB_NAME'].encode()).hexdigest()[:32]
    with db.connect() as lock:
        if lock.execute('SELECT GET_LOCK(?,0)',(key,)).fetchone()[0]!=1:return
        try:
            with db.connect() as con:
                rows=con.execute("SELECT a.id,a.strategy_id,s.manifest,s.source_hash,i.config_revision FROM accounts a JOIN instances i ON a.id=i.account_id JOIN strategies s ON s.id=a.strategy_id WHERE a.mode='paper' AND a.source!='demo' AND i.desired_state IN ('running','paused')").fetchall()
            for row in rows:
                aid=row['id'];manifest=db.parse(row['manifest'])
                with db.connect() as con:
                    records={r['kind']:db.parse(r['payload']) for r in con.execute("SELECT kind,payload FROM ledger WHERE account_id=? AND kind IN ('snapshot','warmup')",(aid,)).fetchall()}
                snapshot=records.get('snapshot',{});previous=records.get('warmup',{})
                if not snapshot.get('frames') or not needs_warmup(manifest,snapshot):continue
                if not flat_snapshot(snapshot):continue
                signature=hashlib.sha256((row['source_hash']+str(row['config_revision'])).encode()).hexdigest()
                if previous.get('signature')==signature and previous.get('status')=='failed' and time.time()-previous.get('updated_epoch',0)<900:continue
                status={'status':'running','progress':0,'signature':signature,'updated_epoch':time.time(),'intervals':normalize_timeframes(manifest.get('timeframes')),'bars_per_interval':manifest['history_window']}
                save_status(aid,status)
                db.log('debug','框架开始历史预热：从 1 分钟行情合成策略周期',account_id=aid,strategy_id=row['strategy_id'],details=status)
                last_update=0
                def progress(processed,total):
                    nonlocal last_update
                    if time.monotonic()-last_update<5:return
                    with db.connect() as con:
                        current=con.execute('SELECT i.desired_state,i.config_revision,s.source_hash FROM instances i JOIN accounts a ON a.id=i.account_id JOIN strategies s ON s.id=a.strategy_id WHERE i.account_id=?',(aid,)).fetchone()
                    if not current or current['desired_state'] not in {'running','paused'} or current['config_revision']!=row['config_revision'] or current['source_hash']!=row['source_hash']:
                        raise ValueError('账户状态或策略配置已改变，取消预热')
                    if total:status['progress']=min(99.9,processed/total*100)
                    status['updated_epoch']=time.time();save_status(aid,status)
                    last_update=time.monotonic()
                try:
                    report=warmup(aid,progress=progress)
                    status.update(status='completed',progress=100,result=report,error=None)
                except Exception as exc:
                    status.update(status='failed',error=str(exc)[:1000])
                    db.log('error','框架历史预热未完成，将在 15 分钟后重试',account_id=aid,strategy_id=row['strategy_id'],details={'error':status['error']})
                status['updated_epoch']=time.time();save_status(aid,status)
        finally:lock.execute('SELECT RELEASE_LOCK(?)',(key,))


def service_loop(once=False):
    while True:
        try:run_pending()
        except Exception as exc:
            db.log('error','历史预热服务异常，稍后重试',details={'error':str(exc)[:500]})
        if once:return
        time.sleep(30)
