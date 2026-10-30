"""Offline contracts against real LDDC models and matching functions."""
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from lyrics_matching import (approximate_score, artists_overlap, choose_candidate, core_title, score_record,
                             search_terms, track_key, versions)
from lyrics_multisource import MultiSourceLyrics, run_worker
from lyrics_translation import TranslationService
from lddc_worker import serialize_lyrics
from LDDC.common.models import Lyrics, LyricsLine, LyricsWord, SongInfo, Source

TRACK = dict(title='Song', artist='Artist', album='Album', duration=180)


def candidate(source='QM', score=115, album='Album', state='synced', text='Hello'):
    return dict(id=source+'-'+'a'*24, source=source,
                record=dict(trackName='Song',artistName='Artist',albumName=album,duration=180),
                score=score, descriptor=dict(source=source,id='1'),
                payload=dict(state=state, lines=[dict(time=1,text=text)] if state=='synced' else [], plain=text))


class MultiSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'cache.sqlite3'

    def test_matching_recording_gates_and_chinese_scripts(self):
        r = candidate()['record']
        self.assertGreater(score_record(r,TRACK),100)
        for title in ('Song (Live)','Song (Remix)','Song (Demo)'):
            self.assertIsNone(score_record(dict(r,trackName=title),TRACK))
        # A remaster is the same recording, whatever its year.
        self.assertGreater(score_record(dict(r,trackName='Song (Remastered 2020)'),TRACK),100)
        self.assertIsNone(score_record(dict(r,artistName='Wrong Artist'),TRACK))
        self.assertIsNotNone(approximate_score(dict(r,artistName='Wrong Artist'),TRACK))
        self.assertIsNone(score_record(dict(r,duration=189),TRACK))
        self.assertIsNone(score_record(dict(r,duration=float('nan')),TRACK))
        live = dict(TRACK,title='Song (Live)')
        self.assertIsNotNone(score_record(dict(r,trackName=live['title']),live))
        remaster=dict(TRACK,title='Song (Remastered 2020)')
        self.assertIsNotNone(score_record(dict(r,trackName='Song (Remastered 2010)'),remaster))
        cn = dict(title='七里香',artist='周杰倫',album='七里香',duration=299)
        self.assertGreater(score_record(dict(trackName='七里香',artistName='周杰伦',albumName='七里香',duration=299),cn),100)

    def test_metadata_confidence_precedes_granularity_and_ambiguity(self):
        strong=candidate(state='plain')
        weaker=candidate('KG',score=90,state='synced')
        self.assertEqual(choose_candidate([weaker,strong]),strong)
        # Another album (Deluxe, Explicit, Single...) only ranks lower.
        self.assertEqual(choose_candidate([candidate(),candidate('NE',score=114,album='Other')]),candidate())
        self.assertIsNotNone(choose_candidate([candidate(),candidate('NE',score=114)]))
        self.assertIsNone(choose_candidate([candidate(),candidate('NE',score=114,state='instrumental')]))

    def test_title_suffixes_and_live_guard(self):
        rather=dict(title='RATHER BE (feat. Lithe)',artist='The Kid LAROI',album='BEFORE I FORGET',duration=264)
        plain=dict(trackName='RATHER BE',artistName='The Kid LAROI',albumName='BEFORE I FORGET',duration=264.2)
        self.assertGreater(score_record(plain,rather,fuzzy=False),100)
        self.assertIsNone(score_record(dict(plain,trackName='RATHER BE (feat. Lithe) (Paused)'),rather))
        theme=dict(title='任性 (電視劇《難哄》主題曲)',artist='五月天',album='任性 (電視劇《難哄》主題曲) - Single',duration=265)
        self.assertIsNotNone(score_record(dict(trackName='任性',artistName='五月天',albumName='任性',duration=265.4),theme,fuzzy=False))
        self.assertEqual(versions(core_title('Song (From "Live Forever")')),set())
        self.assertEqual(versions(core_title('Song (Live from Wembley)')),{'live'})
        sunny=dict(title='晴天 (Live)',artist='周杰伦',album='2004 无与伦比演唱会 Live 全记录',duration=270)
        self.assertIsNone(score_record(dict(trackName='晴天',artistName='周杰伦',albumName='叶惠美',duration=270),sunny))
        self.assertIsNotNone(score_record(dict(trackName='晴天 (Live)',artistName='周杰伦',albumName='2004 无与伦比演唱会',duration=271),sunny))
        studio=dict(sunny,title='晴天',album='叶惠美')
        self.assertIsNone(score_record(dict(trackName='晴天',artistName='周杰伦',albumName='2004 无与伦比演唱会',duration=270),studio))

    def test_artists_compared_per_person(self):
        for a,b in (('Juice WRLD & Justin Bieber','Juice WRLD/Justin Bieber'),
                    ('The Kid LAROI, Future & BabyDrill','The Kid LAROI/Future/Babydrill'),('G.E.M.','G.E.M. 邓紫棋')):
            self.assertTrue(artists_overlap(a,b) and artists_overlap(b,a),(a,b))
        for a,b in (('Artist','Wrong Artist'),('Future','Future Islands'),('Mayday','五月天')):
            self.assertFalse(artists_overlap(a,b),(a,b))
        move=dict(title="WHAT'S THE MOVE?",artist='The Kid LAROI, Future & BabyDrill',album="WHAT'S THE MOVE? - Single",duration=199)
        self.assertIsNotNone(score_record(dict(trackName="WHAT'S THE MOVE?",artistName='The Kid LAROI/Future/Babydrill',
                                               albumName="WHAT'S THE MOVE?",duration=199.598),move,fuzzy=False))

    def test_cached_album_ambiguities_choose_automatically(self):
        # Candidate groups the old album rule left to a manual choice
        # (lyrics_sources.sqlite3, 2026-10-29): one recording, several editions.
        groups={'A LITTLE MUCH':[('网易云音乐',79.53,'CYCLES'),('网易云音乐',79.53,'CYCLES'),('酷狗音乐',80.0,'CYCLES (Explicit)'),
                                  ('LRCLIB',79.53,'CYCLES'),('LRCLIB',78.0,'Unlisted Iann Dior')],
                'Wandered To LA':[('网易云音乐',90.592,'Fighting Demons (Digital Deluxe)'),
                                   ('网易云音乐',89.038,'Fighting Demons (Digital Deluxe)[Explicit]'),
                                   ('酷狗音乐',83.824,'Fighting Demons (Digital Deluxe) [Clean]'),('LRCLIB',84.087,'Wandered to LA'),
                                   ('LRCLIB',82.528,'Wandered To LA - Single')],
                'Wanderlust':[('QQ音乐',89.474,'Kiss Land (Deluxe MENA Version)'),('网易云音乐',90.909,'Kiss Land (Deluxe Edition)'),
                               ('酷狗音乐',89.474,'Kiss Land (Deluxe MENA Version)'),('LRCLIB',86.316,'Wanderlust')],
                'SugarCrash!':[('网易云音乐',94.529,'SugarCrash!'),('酷狗音乐',92.286,'Sugar Crash'),('LRCLIB',94.815,'SugarCrash!')]}
        for title,rows in groups.items():
            group=[dict(candidate(score=score,album=album),id=f'{source}-{i:024d}',source=source) for i,(source,score,album) in enumerate(rows)]
            chosen=choose_candidate(group)
            self.assertIsNotNone(chosen,title)
            self.assertEqual(chosen['score'],max(row[1] for row in rows),title)

    def test_store_alias_names_match_exactly(self):
        own=dict(title='Love Certificate (Remastered)',artist='Yanzi Sun',album='Sun Yan-Zi (Remastered)',duration=255)
        track=dict(own,aliases=[dict(title='愛情證書 (Remastered)',artist='孫燕姿',album='同名專輯 (Remastered)')])
        record=dict(trackName='爱情证书',artistName='孙燕姿',albumName='孙燕姿同名专辑',duration=255.7)
        self.assertIsNotNone(score_record(record,track,fuzzy=False))
        self.assertIsNone(score_record(record,own,fuzzy=False))
        self.assertEqual(search_terms(track),(['Yanzi Sun Love Certificate','孙燕姿 爱情证书'],['Love Certificate','爱情证书']))
        self.assertNotEqual(track_key(track),track_key(own))

    def test_approximate_tier(self):
        track=dict(title='乾杯',artist='Mayday',album='步步自選作品輯 1999-2013',duration=285)
        near=dict(trackName='干杯',artistName='五月天',albumName='步步自选作品辑 1999-2013',duration=286.5)
        self.assertIsNone(score_record(near,track))
        self.assertIsNotNone(approximate_score(near,track))
        for changes in (dict(duration=288),dict(trackName='知足'),dict(albumName='五月天 2009 演唱会')):
            self.assertIsNone(approximate_score(dict(near,**changes),track),changes)
        one=dict(title='愛',artist='Mayday',album='',duration=200)
        self.assertIsNone(approximate_score(dict(trackName='爱',artistName='五月天',albumName='',duration=200),one))
        # Any exact candidate beats the approximate tier, even with a lower score.
        exact=candidate(score=60)
        approx=dict(candidate('NE',score=78),approximate=True)
        self.assertEqual(choose_candidate([approx,exact]),exact)
        self.assertEqual(choose_candidate([approx]),approx)

    def test_load_order_exact_fallback_before_approximate(self):
        approx=dict(candidate('NE',score=70,text='Approx'),approximate=True)
        found=lambda text,**kw:dict(ok=True,state='synced',source='LRCLIB',lines=[dict(time=1,text=text)],plain=text,
                                    record=dict(trackName='Song',artistName='Artist',albumName='Album',duration=180),**kw)
        service=MultiSourceLyrics(self.path,lambda *a:dict(candidates=[approx]))
        result=service.load(TRACK,time.monotonic()+3,lambda *a:found('Exact'))
        self.assertEqual((result['source'],result['match_quality'],result['lines'][0]['text']),('LRCLIB','exact','Exact'))
        self.assertEqual({c['source']:c['approximate'] for c in result['candidates']},{'NE':True,'LRCLIB':False})
        # No exact match anywhere: the approximate LDDC candidate, marked, before an approximate fallback.
        for name,fallback in (('none',lambda *a:dict(ok=True,state='not_found',source='LRCLIB',lines=[],plain='')),
                              ('approx',lambda *a:found('Fallback',match_quality='approximate'))):
            other=MultiSourceLyrics(Path(self.temp.name)/(name+'.sqlite3'),lambda *a:dict(candidates=[approx]))
            result=other.load(TRACK,time.monotonic()+3,fallback)
            self.assertEqual((result['source'],result['match_quality'],result['lines'][0]['text']),('NE','approximate','Approx'))
            self.assertFalse(result['match_requires_selection'])
        lone=MultiSourceLyrics(Path(self.temp.name)/'lone.sqlite3',lambda *a:dict(candidates=[]))
        result=lone.load(TRACK,time.monotonic()+3,lambda *a:found('Fallback',match_quality='approximate'))
        self.assertEqual((result['source'],result['match_quality']),('LRCLIB','approximate'))
        self.assertTrue(result['candidates'][0]['approximate'])
        # A manually chosen approximate candidate never goes to the fallback.
        result=other.load(TRACK,time.monotonic()+3,lambda *a:self.fail('unexpected fallback'),approx['id'])
        self.assertEqual((result['source'],result['match_quality']),('NE','approximate'))

    def test_words_same_timestamp_blank_and_untimed_preserved(self):
        lyrics=Lyrics(SongInfo(Source.QM,title='Song'))
        lyrics['orig']=[LyricsLine(1000,2000,[LyricsWord(1000,1500,'A'),LyricsWord(1500,2000,'B')]),
                        LyricsLine(1000,2000,[LyricsWord(1000,2000,'C')]),
                        LyricsLine(3000,4000,[LyricsWord(3000,4000,'')])]
        lyrics['ts']=[LyricsLine(1000,2000,[LyricsWord(None,None,'译文')])]
        value=serialize_lyrics(lyrics)
        self.assertEqual([r['text'] for r in value['lines']],['AB\nC',''])
        self.assertEqual([w['start'] for w in value['lines'][0]['words']],[1000,1500,1000])
        self.assertEqual(len(value['raw_lyrics']['orig']),3)
        self.assertEqual(value['raw_lyrics']['ts'][0]['words'][0]['text'],'译文')
        lyrics['orig'].append(LyricsLine(None,None,[LyricsWord(None,None,'Untimed')]))
        value=serialize_lyrics(lyrics)
        self.assertEqual(value['state'],'plain')
        self.assertEqual(value['plain'],'AB\nC\n\nUntimed')

    def test_source_switch_disk_cache_and_concurrent_merge(self):
        calls=[]
        def worker(command,deadline):
            calls.append(command)
            time.sleep(.05)
            return dict(candidates=[candidate(),candidate('NE',score=114,text='Other')],source_status={'QM':'ok','NE':'ok','KG':'timeout'})
        service=MultiSourceLyrics(self.path,worker)
        def fallback(*args):
            self.fail('unexpected fallback')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda i:service.load(TRACK,time.monotonic()+3,fallback,
                             candidate('NE')['id'] if i%2 else 'auto'),range(8)))
        self.assertEqual(len(calls),1)
        self.assertEqual([v['lines'][0]['text'] for v in results],['Hello','Other']*4)
        reopened=MultiSourceLyrics(self.path,worker)
        self.assertTrue(reopened.load(TRACK,time.monotonic()+3,fallback)['source_cache_hit'])
        self.assertEqual(len(calls),1)

    def test_failure_fallback_plain_missing_and_invalid_choice(self):
        for state in ('synced','plain','instrumental','not_found'):
            path=Path(self.temp.name)/(state+'.sqlite3')
            service=MultiSourceLyrics(path,lambda *args:dict(error='timeout'))
            result=service.load(TRACK,time.monotonic()+3,lambda *args:dict(ok=True,state=state,source='LRCLIB',lines=[],plain='text',record={}))
            self.assertEqual(result['state'],state)
        result=service.load(TRACK,time.monotonic()+3,lambda *args:None,'QM-'+'f'*24)
        self.assertEqual(result['error'],'candidate_expired')

    def test_on_demand_selection_and_fetch_cache(self):
        unloaded=candidate('NE');unloaded.pop('payload')
        calls=[]
        def worker(command,deadline):
            calls.append(command['operation'])
            return dict(candidates=[candidate(),unloaded]) if command['operation']=='discover' else dict(payload=candidate('NE',text='Changed')['payload'])
        service=MultiSourceLyrics(self.path,worker)
        for _ in range(2):
            result=service.load(TRACK,time.monotonic()+3,lambda *a:None,unloaded['id'])
            self.assertEqual(result['lines'][0]['text'],'Changed')
        self.assertEqual(calls,['discover','fetch'])

    def test_content_mapping_invalidation_and_registry_overlay(self):
        service=TranslationService(Path(self.temp.name)/'translation.sqlite3')
        self.addCleanup(service.pool.shutdown)
        result=dict(ok=True,state='synced',source='QQ音乐',record={},lines=[dict(time=1,text='Hello')],candidates=[])
        first=service.register(result)
        changed=service.register(dict(result,lines=[dict(time=1,text='Changed')]))
        self.assertNotEqual(first['content_hash'],changed['content_hash'])
        self.assertNotEqual(first['lyrics_id'],changed['lyrics_id'])
        overlay=service.register(dict(result,candidates=[{'id':'more'}]))
        self.assertEqual(first['lyrics_id'],overlay['lyrics_id'])
        self.assertEqual(overlay['candidates'],[{'id':'more'}])

    def test_helper_hard_timeout_kills_child(self):
        started=time.monotonic()
        (Path(self.temp.name)/'lddc_worker.py').write_text('import time\ntime.sleep(60)\n',encoding='utf8')
        with patch('lyrics_multisource.ROOT',Path(self.temp.name)):
            self.assertEqual(run_worker(dict(operation='discover'),started+1)['error'],'timeout')
        self.assertLess(time.monotonic()-started,2)


if __name__=='__main__':
    unittest.main(verbosity=2)
