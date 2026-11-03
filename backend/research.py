"""Private, compressed research scratch space; never a business database.

Full records remain available for final Parquet publication. Only a small tail
and one decoded block live in memory. Files are removed when the job exits.
"""
from collections.abc import Sequence
from collections import OrderedDict
from contextlib import ExitStack
import os
import shutil
import pickle
import struct
import tempfile
import zlib

from . import storage, db


class DiskSequence(Sequence):
    BLOCK = 64

    def __init__(self, directory, on_write=lambda size: None):
        self.on_write=on_write
        self.data = tempfile.TemporaryFile(dir=directory)
        self.index = tempfile.TemporaryFile(dir=directory)
        self.tail = []
        self.blocks = 0
        self.cached_index = None
        self.cached = None

    def append(self, value):
        if len(self.tail) == self.BLOCK:
            payload = zlib.compress(pickle.dumps(self.tail, protocol=5), level=1)
            self.on_write(len(payload)+16)
            self.data.seek(0, 2)
            position = self.data.tell()
            self.data.write(struct.pack('<Q', len(payload)) + payload)
            self.index.seek(0, 2)
            self.index.write(struct.pack('<Q', position))
            self.blocks += 1
            self.tail = []
        self.tail.append(value)

    def extend(self, values):
        for value in values:
            self.append(value)

    def __len__(self):
        return self.blocks * self.BLOCK + len(self.tail)

    def _block(self, index):
        if index == self.blocks:
            return self.tail
        if index != self.cached_index:
            self.index.seek(index * 8)
            position = struct.unpack('<Q', self.index.read(8))[0]
            self.data.seek(position)
            size = struct.unpack('<Q', self.data.read(8))[0]
            self.cached = pickle.loads(zlib.decompress(self.data.read(size)))
            self.cached_index = index
        return self.cached

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self))) ]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return self._block(index // self.BLOCK)[index % self.BLOCK]

    def __iter__(self):
        for i in range(self.blocks):
            yield from self._block(i)
        yield from self.tail

    def close(self):
        self.data.close()
        self.index.close()
        self.tail.clear()
        self.cached = None


class ResearchSpace:
    def __init__(self, root):
        self.stack = ExitStack()
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix=f'.research-{os.getpid()}-', dir=root))
        self.written_since_check=8*1024*1024
        self.check_space(0)

    def sequence(self):
        sequence = DiskSequence(self.directory,self.check_space)
        self.stack.callback(sequence.close)
        return sequence

    def check_space(self, size):
        self.written_since_check+=size
        if self.written_since_check>=8*1024*1024:
            self.written_since_check=0
            if shutil.disk_usage(self.directory).free<512*1024*1024:
                raise storage.ArtifactError("回测临时存储空间不足，已停止任务以保护磁盘余量")

    def mapping(self, active=None):
        archive=DiskMap(self.directory,self.check_space)
        self.stack.callback(archive.close)
        return ActiveMap(archive,active) if active else archive

    def close(self):
        self.stack.close()


class RecordView:
    def __init__(self, sources, transform=lambda row, tag: row, predicate=lambda row: True):
        self.sources, self.transform, self.predicate = sources, transform, predicate

    def __iter__(self):
        for tag, source in self.sources:
            for row in source:
                if self.predicate(row):
                    yield self.transform(row, tag)

    def __len__(self):
        return sum(1 for _ in self)


class CandleWindow:
    """One bounded Parquet cursor per symbol, accessed in minute order."""
    def __init__(self, value, symbols, start, end):
        self.value, self.symbols, self.start, self.end = db.parse(value,{}) if isinstance(value,str) else value, sorted(symbols), start, end
        self.cursors = []

    def prepare(self):
        if not storage.is_manifest(self.value):
            from .engine import _prepare_candles
            return _prepare_candles(storage.read_candles(self.value,symbols=self.symbols,start=self.start,end=self.end))
        state = storage.validate_minute_windows(self.value, self.symbols, [(self.start, self.end)])[0]
        first, last, _ = state[self.symbols[0]]
        timestamps = range(first, last + 60000, 60000)
        path = storage._resolve(self.value)
        prepared = {}
        for symbol in self.symbols:
            cursor = _SymbolCursor(path, symbol, first, last + 60000, len(timestamps))
            self.cursors.append(cursor)
            prepared[symbol] = cursor
        return prepared, timestamps

    def close(self):
        for cursor in self.cursors:
            cursor.rows.close()
        self.cursors.clear()


class _SymbolCursor:
    def __init__(self, path, symbol, start, end, count):
        self.count = count
        self.rows = self._rows(path, symbol, start, end)
        self.last = None

    def __len__(self):
        return self.count

    def __getitem__(self, timestamp):
        if self.last is None or self.last['timestamp'] != timestamp:
            self.last = next(self.rows, None)
        if self.last is None or self.last['timestamp'] != timestamp:
            raise storage.ArtifactError('流式分钟行情不连续或读取顺序错误')
        return self.last

    @staticmethod
    def _rows(path, symbol, start, end):
        _, pq = storage._arrow()
        with pq.ParquetFile(path) as parquet:
            groups = storage._candle_row_groups(parquet, [symbol], start, end)
            columns = ['symbol', 'timestamp', 'open', 'high', 'low', 'close', 'volume']
            for batch in parquet.iter_batches(batch_size=512, row_groups=groups, columns=columns, use_threads=False):
                for row in batch.to_pylist():
                    if row['symbol'] == symbol and start <= row['timestamp'] < end:
                        yield {key: value for key, value in row.items() if key != 'symbol'}


class ExtremaPreview:
    """Known-length bucket min/max; bounded running preview with real points."""
    def __init__(self, total, budget=600):
        import math
        self.width = max(1, math.ceil(max(0, total - 2) / max(1, (budget - 2) // 2)))
        self.first = self.last = None
        self.buckets = {}
        self.count = 0

    def extend(self, rows):
        for row in rows:
            point = (self.count, row)
            self.first = self.first or point
            self.last = point
            key = max(0, self.count - 1) // self.width
            low, high = self.buckets.get(key, (point, point))
            if row['equity'] < low[1]['equity']:
                low = point
            if row['equity'] > high[1]['equity']:
                high = point
            self.buckets[key] = (low, high)
            self.count += 1

    def points(self):
        if self.first is None:
            return []
        points = {point[0]: point[1] for pair in self.buckets.values() for point in pair}
        points[self.first[0]], points[self.last[0]] = self.first[1], self.last[1]
        return [points[key] for key in sorted(points)]


class DiskMap:
    """Append-only hash chains with a fixed on-disk bucket table, no SQL engine."""
    BUCKETS = 1 << 18

    def __init__(self, directory, on_write=lambda size: None):
        self.on_write=on_write
        import mmap
        self.data = tempfile.TemporaryFile(dir=directory)
        self.data.write(b'\0')
        self.index_file = tempfile.TemporaryFile(dir=directory)
        self.index_file.truncate(self.BUCKETS * 8)
        self.heads = mmap.mmap(self.index_file.fileno(), self.BUCKETS * 8)
        self.count = 0
        self.cache = OrderedDict()

    def _remember(self,key,value):
        self.cache[key]=value
        self.cache.move_to_end(key)
        if len(self.cache)>256:
            self.cache.popitem(last=False)
        return value

    def _bucket(self, key):
        import hashlib
        return (int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), 'little') & (self.BUCKETS-1))*8

    def __getitem__(self, key):
        if not isinstance(key,str):
            raise KeyError(key)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        position = struct.unpack_from('<Q',self.heads,self._bucket(key))[0]
        while position:
            self.data.seek(position)
            previous,size=struct.unpack('<QI',self.data.read(12))
            saved,value=pickle.loads(zlib.decompress(self.data.read(size)))
            if saved==key:
                return self._remember(key,value)
            position=previous
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        try:
            self[key]
            return True
        except KeyError:
            return False

    def __setitem__(self, key, value):
        bucket=self._bucket(key)
        previous=struct.unpack_from('<Q',self.heads,bucket)[0]
        if key not in self:
            self.count+=1
        payload=zlib.compress(pickle.dumps((key,value),protocol=5),level=1)
        self.data.seek(0,2)
        position=self.data.tell()
        self.on_write(len(payload)+12)
        self.data.write(struct.pack('<QI',previous,len(payload))+payload)
        # Flush before another read switches this buffered file's position.
        self.data.flush()
        struct.pack_into('<Q',self.heads,bucket,position)
        self._remember(key,value)

    def __len__(self):
        return self.count

    def close(self):
        self.cache.clear()
        self.heads.close()
        self.index_file.close()
        self.data.close()


class ActiveMap:
    """Keep mutable live state in RAM; preserve terminal IDs on disk."""
    def __init__(self, archive, active):
        self.live, self.archive, self.active = {}, archive, active

    def __getitem__(self, key):
        return self.live[key] if key in self.live else self.archive[key]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        return key in self.live or key in self.archive

    def __setitem__(self, key, value):
        if self.active(value):
            self.live[key]=value
        else:
            self.archive[key]=value
            self.live.pop(key,None)

    def __len__(self):
        return len(self.live)+len(self.archive)
