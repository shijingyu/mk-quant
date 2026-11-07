"""Resolve immutable minute files independently for each requested symbol."""
import hashlib
from datetime import datetime, timezone, timedelta
from . import db, storage


def referenced_ids(config, primary=None):
    return ({primary} if primary else set()) | {p['dataset_id'] for p in (config or {}).get('data_selection',{}).get('parts',[])}


def resolve(market, symbols, windows):
    with db.connect() as con:
        rows=con.execute("SELECT * FROM datasets WHERE market=? AND `interval`='1m' ORDER BY created_at DESC,id DESC",(market,)).fetchall()
    # Synthetic fixtures are only eligible when explicitly requested by internal tests.
    rows=[r for r in rows if r['source'] not in {'synthetic','demo','legacy_archive'}]
    return resolve_rows(rows,symbols,windows)


def resolve_rows(rows,symbols,windows):
    parts=[];used={}
    for symbol in symbols:
        candidates=[]
        for row in rows:
            if symbol not in db.parse(row['symbols'],[]):continue
            artifact=db.parse(row['candles'],{})
            meta=storage.candle_metadata(artifact)
            if meta['start'] is not None:candidates.append((row,artifact,meta))
        for start,end in windows:
            cursor=start
            while cursor<end:
                available=[c for c in candidates if c[2]['start']<=cursor and c[2]['end']>=cursor]
                if not available:
                    stamp=datetime.fromtimestamp(cursor/1000,timezone(timedelta(hours=8))).strftime('%Y/%m/%d %H:%M')
                    raise ValueError(f'{symbol} 缺少 {stamp}（北京时间）起的 1 分钟行情，请先下载或导入覆盖该区间的数据')
                # Stable newest eligible version; overlap never creates duplicate minutes.
                row,artifact,meta=available[0]
                stop=min(end,meta['end']+60000)
                parts.append({'symbol':symbol,'start':cursor,'end':stop,'dataset_id':row['id'],'sha256':row['sha256'],'artifact':artifact})
                used[row['id']]=row
                cursor=stop
    selection={'storage':'symbol_selection','version':1,'parts':parts,'symbols':list(symbols)}
    storage.validate_minute_windows(selection,symbols,windows)
    digest=hashlib.sha256(db.dump(selection).encode()).hexdigest()
    return selection,used,digest


@storage.lifecycle_lock()
def migrate_grouped():
    """Split old groups; keep immutable archived sources for historical jobs."""
    from . import artifact_cleanup
    with db.connect() as con:
        rows=con.execute("SELECT * FROM datasets WHERE source!='legacy_archive'").fetchall()
    migrated=[]
    for row in rows:
        if len(db.parse(row['symbols'],[]))<=1 or row['interval']!='1m':continue
        original=db.parse(row['candles'])
        if not storage.is_manifest(original):original=storage.write_candles(original)
        artifacts=storage.split_candles(original)
        with db.connect() as con:
            current=con.execute('SELECT * FROM datasets WHERE id=? FOR UPDATE',(row['id'],)).fetchone()
            if not current or current['source']=='legacy_archive':continue
            identities=[]
            for item in artifacts:
                symbol=item['symbols'][0]
                identity=hashlib.sha256((row['id']+':'+symbol).encode()).hexdigest()[:32]
                identities.append(identity)
                con.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?)',(identity,symbol+' · 1m',row['market'],'1m',db.dump([symbol]),db.dump(item),item['content_sha256'],row['source'],row['created_at']))
            refs=any(row['id'] in referenced_ids(db.parse(r['config'],{}),r['dataset_id']) for r in con.execute('SELECT dataset_id,config FROM experiments').fetchall())
            con.execute('UPDATE data_download_jobs SET dataset_id=? WHERE dataset_id=?',(identities[0],row['id']))
            if refs:con.execute("UPDATE datasets SET source='legacy_archive' WHERE id=?",(row['id'],))
            else:con.execute('DELETE FROM datasets WHERE id=?',(row['id'],))
            db.audit(con,None,'dataset.split','dataset',row['id'],{'dataset_ids':identities,'archived':refs,'original_source':row['source']})
            cleanup=artifact_cleanup.enqueue(con,None,[original])
        artifact_cleanup.drain(cleanup)
        migrated.append({'id':row['id'],'symbols':db.parse(row['symbols']),'archived':refs})
    return migrated
