"""Read-only real Windows service/source checks; reports metadata counts only."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8765'


def get(path):
    with urlopen(BASE+path,timeout=25) as response:
        return json.load(response)


def run():
    report=dict(read_only=True, tracks=[], status_latencies_ms=[], platforms='Windows service only')
    queries=[dict(title='七里香',artist='周杰倫',album='七里香',duration=299),
             dict(title='I Want to Live',artist='Borislav Slavov',album="Baldur's Gate 3 (Original Game Soundtrack)",duration=233),
             dict(title='PICK SIDES',artist='The Kid LAROI',album='THE FIRST TIME (DELUXE VERSION)',duration=172),
             dict(title='No Such Recording 937fab123',artist='No Such Artist',album='No Such Album',duration=123)]
    for query in queries:
        path='/lyrics?'+urlencode(query)
        with ThreadPoolExecutor(max_workers=3) as pool:
            started=time.monotonic()
            first=pool.submit(get,path)
            second=pool.submit(get,path)
            time.sleep(.1)
            latency=time.monotonic()
            status=get('/status')
            report['status_latencies_ms'].append(round((time.monotonic()-latency)*1000,2))
            assert isinstance(status['ok'],bool)
            result=first.result()
            other=second.result()
        assert result['state']==other['state']
        assert result.get('lyrics_id')==other.get('lyrics_id')
        item=dict(title=query['title'],elapsed_seconds=round(time.monotonic()-started,2),
                  source=result.get('source'),state=result['state'],error=result.get('error'),
                  source_status=result.get('source_status'),lines=len(result.get('lines',[])),
                  candidates=len(result.get('candidates',[])),cached=get(path).get('cached'),switches=[])
        item['manual_unloaded_sources']=[]
        seen=set()
        for candidate in result.get('candidates',[]):
            if candidate.get('state')!='unloaded' or candidate['source'] in seen:
                continue
            seen.add(candidate['source'])
            switched=get(path+'&candidate='+candidate['id'])
            item['manual_unloaded_sources'].append(dict(source=candidate['source'],state=switched['state'],
                                                       error=switched.get('error'),lines=len(switched.get('lines',[]))))
        assert item['cached'] is True
        if result['state']=='synced':
            assert len(result['display_rows'])==len(result['lines'])
            times=[r['time'] for r in result['lines']]
            assert times==sorted(times)
            assert len(set(r['id'] for r in result['display_rows']))==len(times)
            for candidate in result.get('candidates',[]):
                if candidate.get('state') not in ('synced','plain','instrumental'):
                    continue
                switched=get(path+'&candidate='+candidate['id'])
                assert switched['state']==candidate['state']
                assert switched['source']==candidate['source']
                if switched['state']=='synced':
                    assert len(switched['display_rows'])==len(switched['lines'])
                elif switched['state']=='plain':
                    assert len(switched['display_rows'])==len(switched['plain'].split('\n'))
                item['switches'].append(dict(source=switched['source'],state=switched['state'],lines=len(switched['lines']),
                                             same_content=switched.get('content_hash')==result['content_hash']))
            item['word_timing_retained']=any(w['start'] is not None for line in result.get('raw_lyrics',{}).get('orig',[])
                                           for w in line['words'])
        report['tracks'].append(item)
    # A sampled timeline should advance between reads even if Apple Music has
    # not emitted a new position. Do not alter the user's playing track.
    a=get('/status');time.sleep(.35);b=get('/status')
    report['playback_clock']=dict(playing=a.get('playing'),same_track=a.get('title')==b.get('title'),
        same_smtc_sample=a.get('timeline_sample')==b.get('timeline_sample'),position_delta_seconds=round(b.get('pos',0)-a.get('pos',0),3))
    target=ROOT/'tests/artifacts/lddc-live-results.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__':
    run()
