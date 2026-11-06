"""Public USD-M perpetual research snapshots. No trading credentials or orders."""
import math
import threading
import time
from statistics import fmean
from .binance import BinanceClient

ASSETS = 'BTC ETH BNB SOL XRP ADA DOGE TRX AVAX LINK DOT LTC BCH XLM SUI NEAR UNI ATOM HBAR 1000SHIB'.split()
WINDOWS = {'24h':('24 小时','15m',900000,96),'7d':('7 天','1h',3600000,168),'30d':('30 天','4h',14400000,180),'90d':('90 天','1d',86400000,90)}


def covariance(x,y):
    mx,my=fmean(x),fmean(y)
    return sum((a-mx)*(b-my) for a,b in zip(x,y))


def pair_metrics(x,y):
    if len(x)!=len(y) or len(x)<3 or any(not math.isfinite(v) or v<=0 for v in [*x,*y]):
        raise ValueError('价格样本无效')
    lx,ly=[math.log(v) for v in x],[math.log(v) for v in y]
    rx,ry=[b-a for a,b in zip(lx,lx[1:])],[b-a for a,b in zip(ly,ly[1:])]
    xx,yy=covariance(rx,rx),covariance(ry,ry)
    corr=max(-1.,min(1.,covariance(rx,ry)/math.sqrt(xx*yy))) if xx>1e-24 and yy>1e-24 else None
    vy=covariance(ly,ly)
    beta=covariance(lx,ly)/vy if vy>1e-24 else None
    spread=[a-beta*b for a,b in zip(lx,ly)] if beta is not None else []
    mean=fmean(spread) if spread else 0
    sd=math.sqrt(covariance(spread,spread)/len(spread)) if spread else 0
    z=(spread[-1]-mean)/sd if sd>1e-12 else None
    lag=spread[:-1];delta=[b-a for a,b in zip(spread,spread[1:])]
    vl=covariance(lag,lag) if lag else 0
    slope=covariance(lag,delta)/vl if vl>1e-24 else None
    phi=1+slope if slope is not None else None
    half=-math.log(2)/math.log(phi) if phi is not None and 0<phi<1 else None
    return {'correlation':corr,'beta':beta,'zscore':z,'half_life':half,'samples':len(rx),'status':'数据不足' if z is None else '显著偏离' if abs(z)>=2 else '偏离' if abs(z)>=1 else '接近均值'}


def closed_prices(rows,end,step,count):
    expected=list(range(end-count*step,end,step))
    indexed={}
    for row in rows:
        stamp=int(row[0]);price=float(row[4])
        if stamp in indexed:raise ValueError('行情时间重复')
        if stamp in expected and int(row[6])<end and math.isfinite(price) and price>0:indexed[stamp]=price
    if any(t not in indexed for t in expected):raise ValueError('已收盘行情不足或存在缺口')
    return [indexed[t] for t in expected]


def build_window(client,key,server_ms,tradable):
    label,interval,step,count=WINDOWS[key];end=server_ms//step*step
    prices={};missing={};symbols=[a+'USDT' for a in ASSETS]
    for symbol in symbols:
        if symbol not in tradable:
            missing[symbol]='当前非可交易 USDT 永续合约';continue
        time.sleep(.5)
        rows=client._request('GET','/fapi/v1/klines',{'symbol':symbol,'interval':interval,'startTime':end-count*step,'endTime':end-1,'limit':count})
        try:prices[symbol]=closed_prices(rows,end,step,count)
        except (ValueError,TypeError,IndexError):missing[symbol]='已收盘行情不足或存在缺口'
    if len(prices)<2:raise ValueError("完整行情不足，无法计算相关性矩阵")
    matrix=[];pairs=[]
    for i,a in enumerate(symbols):
        line=[]
        for j,b in enumerate(symbols):
            if a not in prices or b not in prices:line.append(None);continue
            if i==j:line.append(pair_metrics(prices[a],prices[a])['correlation']);continue
            if j<i:line.append(matrix[j][i]);continue
            result=pair_metrics(prices[a],prices[b]);line.append(result['correlation'])
            pairs.append({'a':a,'b':b,**result})
        matrix.append(line)
    pairs.sort(key=lambda p:p['correlation'] if p['correlation'] is not None else -2,reverse=True)
    return {'label':label,'interval':interval,'bars':count,'returns':count-1,'start':end-count*step,'end':end,'symbols':symbols,'matrix':matrix,'pairs':pairs,'missing':missing}


_lock=threading.Lock()
_state={'windows':{},'updated_at':None,'refreshing':False,'error':None,'next_refresh':0}


def _refresh():
    client=BinanceClient(market='futures',timeout=8,max_retries=1)
    try:
        server_ms=int(client._request('GET','/fapi/v1/time')['serverTime'])
        exchange=client._request('GET','/fapi/v1/exchangeInfo')
        tradable={s['symbol'] for s in exchange['symbols'] if s.get('contractType')=='PERPETUAL' and s.get('status')=='TRADING' and s.get('quoteAsset')=='USDT'}
        windows={key:build_window(client,key,server_ms,tradable) for key in WINDOWS}
        with _lock:_state.update(windows=windows,updated_at=int(time.time()*1000),error=None,next_refresh=time.time()+900)
    except Exception as exc:
        with _lock:_state.update(error='公开行情更新失败：'+str(exc)[:200],next_refresh=max(time.time()+60,getattr(client,'_cooldown_until',0)))
    finally:
        with _lock:_state['refreshing']=False


def snapshot():
    with _lock:
        if not _state['refreshing'] and time.time()>=_state['next_refresh']:
            _state['refreshing']=True
            threading.Thread(target=_refresh,daemon=True,name='public-correlation').start()
        return {**_state,'stale':bool(_state['error']) or not _state['updated_at'] or time.time()*1000-_state['updated_at']>1200000,'next_refresh':int(_state['next_refresh']*1000)}
