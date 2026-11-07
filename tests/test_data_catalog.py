import pytest
from fastapi import HTTPException
from backend import db, storage, data_catalog
from backend.app import store_dataset, enqueue_experiment, delete_dataset, list_datasets
from backend.research import CandleWindow
from backend.worker import run_experiment

START=1767225600000

def bars(first=0,last=12):
    return [{'timestamp':START+i*60000,'open':100+i,'high':102+i,'low':99+i,'close':101+i,'volume':10} for i in range(first,last)]

def put(symbols,first=0,last=12):
    return store_dataset({'market':'futures','interval':'1m','symbols':symbols,'candles':{s:bars(first,last) for s in symbols}},'import')

def request(symbols):
    return {'strategy_id':'momentum','symbols':symbols,'initial_capital':10000,
            'train_start':START,'train_end':START+6*60000,'validation_start':START+6*60000,'validation_end':START+12*60000}

def test_cross_download_symbols_are_pinned_and_streamed(mysql_database):
    a=put(['ADAUSDC','DOGEUSDC']);b=put(['SOLUSDC','BTCUSDC'])
    assert len(a['datasets'])==len(b['datasets'])==2
    assert all(len(d['symbols'])==1 for d in list_datasets())
    selected,used,_=data_catalog.resolve('futures',['ADAUSDC','SOLUSDC'],[(START,START+12*60000)])
    assert len(used)==2
    feed=CandleWindow(selected,['ADAUSDC','SOLUSDC'],START,START+12*60000)
    values,times=feed.prepare()
    assert len(times)==12
    for t in times:assert values['ADAUSDC'][t]==values['SOLUSDC'][t]
    feed.close()
    rules={s:{'tick_size':'.01','step_size':'.001','min_qty':'.001','min_notional':'5'} for s in ['ADAUSDC','SOLUSDC']}
    job=enqueue_experiment(request(list(rules)),rules_snapshot=(rules,'test'))
    with db.connect() as con:
        row=con.execute('SELECT * FROM experiments WHERE id=?',(job['id'],)).fetchone()
    config=db.parse(row['config']);ids=data_catalog.referenced_ids(config,row['dataset_id'])
    assert len(ids)==2
    secondary=next(i for i in ids if i!=row['dataset_id'])
    with pytest.raises(HTTPException) as error:delete_dataset(secondary,current={'id':None})
    assert error.value.status_code==409
    assert next(d for d in list_datasets() if d['id']==secondary)['experiment_count']==1
    run_experiment(job['id'])
    with db.connect() as con:
        finished=con.execute('SELECT status,error,result FROM experiments WHERE id=?',(job['id'],)).fetchone()
    assert finished['status']=='completed',finished['error']
    assert set(db.parse(finished['result'])['symbol_results'])==set(rules)

def test_chunks_overlap_and_gaps(mysql_database):
    put(['ADAUSDC'],0,6);put(['ADAUSDC'],6,12);put(['ADAUSDC'],3,8)
    selection,_,_=data_catalog.resolve('futures',['ADAUSDC'],[(START,START+12*60000)])
    assert storage.read_candles(selection)['ADAUSDC']==bars()
    with pytest.raises(ValueError,match='ADAUSDC 缺少'):data_catalog.resolve('futures',['ADAUSDC'],[(START,START+13*60000)])
    with pytest.raises(ValueError,match='ADAUSDC 缺少'):data_catalog.resolve('spot',['ADAUSDC'],[(START,START+12*60000)])
    with pytest.raises(ValueError,match='SOLUSDC 缺少'):data_catalog.resolve('futures',['SOLUSDC'],[(START,START+12*60000)])

def test_legacy_groups_split_without_breaking_old_jobs(mysql_database):
    original=storage.write_candles({'ADAUSDC':bars(),'SOLUSDC':bars()})
    with db.connect() as con:
        con.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?)',('legacy','group','futures','1m',db.dump(original['symbols']),db.dump(original),original['content_sha256'],'import',db.now()))
        con.execute('INSERT INTO experiments(id,name,strategy_id,dataset_id,status,config,provenance,created_at) VALUES(?,?,?,?,?,?,?,?)',('old','old','momentum','legacy','completed','{}','{}',db.now()))
    assert data_catalog.migrate_grouped()[0]['archived']
    assert data_catalog.migrate_grouped()==[]
    assert len(list_datasets())==2
    assert len(storage.read_candles(original))==2
    selection,used,_=data_catalog.resolve('futures',['ADAUSDC','SOLUSDC'],[(START,START+12*60000)])
    assert 'legacy' not in used and len(used)==2
