"""运行: python -m unittest discover -s tests -v

完整加载插件，替换 MoviePilot 服务和下载器边界；测试不会连接任何真实下载器。
"""
import copy
import importlib.util
import re
import sys
import types
import unittest
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import Mock, patch


class PluginBase:
    def __init__(self):
        self.data = {}
        self.notifications = []

    def get_data(self, key):
        return copy.deepcopy(self.data.get(key))

    def save_data(self, key, value):
        self.data[key] = copy.deepcopy(value)

    def update_config(self, config):
        self.config = copy.deepcopy(config)

    def post_message(self, **kwargs):
        self.notifications.append(kwargs)


def meta(title):
    season = re.search(r'S(\d+)', title, re.I)
    episode = re.search(r'E(\d+)', title, re.I)
    year = re.search(r'(?:19|20)\d{2}', title)
    pix = re.search(r'(2160|1080|720)p', title)
    return N(name='Example', title='Example', org_string=title,
             year=year[0] if year else None, resource_pix=pix[0] if pix else '',
             season_list=[int(season[1])] if season else [],
             episode_list=[int(episode[1])] if episode else [])


def load_plugin():
    modules = {}

    def stub(name, **attributes):
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        modules[name] = module

    stub('pytz', timezone=lambda _: timezone.utc)
    stub('apscheduler.schedulers.background', BackgroundScheduler=Mock)
    stub('apscheduler.triggers.cron', CronTrigger=N(from_crontab=lambda text: text))
    stub('app.chain.download', DownloadChain=Mock)
    stub('app.chain.media', MediaChain=Mock)
    stub('app.chain.search', SearchChain=Mock)
    stub('app.core.config', settings=N(TZ='UTC'))
    stub('app.core.context', Context=N)
    stub('app.core.event', eventmanager=N(register=lambda _: lambda f: f), Event=N)
    stub('app.core.metainfo', MetaInfo=meta)
    stub('app.helper.downloader', DownloaderHelper=Mock)
    stub('app.db.site_oper', SiteOper=Mock)
    stub('app.log', logger=Mock())
    stub('app.plugins', _PluginBase=PluginBase)
    stub('app.schemas', NotificationType=N(Download='download'), ServiceInfo=N)
    stub('app.schemas.types', EventType=N(PluginAction='plugin'))
    path = Path(__file__).resolve().parents[1] / 'plugins.v2/qboptimizer/__init__.py'
    spec = importlib.util.spec_from_file_location('qboptimizer_tested', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


PLUGIN = load_plugin()
GIB = 1024 ** 3


def torrent(hash='old', **kwargs):
    fields = dict(hash=hash, name='Example.2021.S01E01.2160p.WEB-DL-GROUP',
                  state='stalledDL', progress=.1, size=100*GIB, amount_left=90*GIB,
                  downloaded=10*GIB, dlspeed=0, num_complete=0, num_seeds=0,
                  availability=0, added_on=1, priority=1, force_start=False,
                  category='Movies', tags='', save_path='/downloads', download_path='')
    fields.update(kwargs)
    return N(**fields)


class Downloader:
    def __init__(self, *torrents):
        self.items = {t.hash: copy.deepcopy(t) for t in torrents}
        self.deleted, self.started, self.stopped, self.priorities, self.limit_calls = [], [], [], [], []
        self.free = 200*GIB
        self.cache, self.queue = 0, 0
        self.limit, self.upload = 0, 10240
        self.queueing = True
        self.delete_ok = self.stop_ok = True
        self.fetch_hook = None
        self.qbc = N(sync_maindata=self.sync, transfer_set_download_limit=self.set_limit,
                     app_preferences=lambda: {'queueing_enabled': self.queueing},
                     torrents_top_priority=lambda torrent_hashes: self.priorities.extend(torrent_hashes))

    def sync(self, **kwargs):
        return {'server_state': {'free_space_on_disk': self.free,
                                 'write_cache_overload': self.cache, 'queued_io_jobs': self.queue}}

    def set_limit(self, limit):
        self.limit_calls.append(limit)
        self.limit = limit / 1024

    def get_speed_limit(self):
        return self.limit, self.upload

    def get_torrents(self, ids=None):
        if self.fetch_hook:
            self.fetch_hook(ids)
        return [copy.deepcopy(t) for key, t in self.items.items() if ids is None or key in ids], None

    def get_files(self, tid):
        if tid not in self.items:
            raise RuntimeError('stale hash')
        t = self.items[tid]
        return [N(name=getattr(t, 'file_name', t.hash + '.mkv'), size=t.size, progress=t.progress, priority=1)]

    def delete_torrents(self, delete_file, ids):
        self.deleted.append((delete_file, ids))
        if self.delete_ok:
            for key in ids:
                self.items.pop(key, None)
        return self.delete_ok

    def stop_torrents(self, ids):
        self.stopped.extend(ids)
        if self.stop_ok:
            for key in ids:
                self.items[key].state = 'stoppedDL'
        return self.stop_ok

    def start_torrents(self, ids):
        self.started.extend(ids)
        for key in ids:
            self.items[key].state = 'downloading'
        return True


class OptimizerTests(unittest.TestCase):
    def setUp(self):
        self.now = 200000.0
        self.clock = patch.object(PLUGIN.time, 'time', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.p = PLUGIN.QbOptimizer()
        self.p.init_plugin({'downloaders': ['A'], 'notify': False, 'enable_auto_redownload': False,
                            'enable_unrecognized_clean': True, 'cleanup_grace_hours': .25,
                            'monitor_duration': 1, 'monitor_interval': 1})
        self.media = patch.object(PLUGIN, 'MediaChain', return_value=N(recognize_by_meta=lambda _: None))
        self.media.start()
        self.addCleanup(self.media.stop)

    def mature(self, d, kind='zero', name='A'):
        result = None
        for _ in range(3):
            result = self.p._clean_torrents(d, d.get_torrents()[0], name, kind)
            self.now += 13*3600 if kind == 'slow' else 500
        return result

    def test_filters_block_all_three_cleanup_paths(self):
        for field, fields in [('exclude_keywords', {'name':'KEEP.movie'}),
                              ('exclude_categories', {'category':' Keep '}),
                              ('exclude_labels', {'tags':'other, KEEP'})]:
            for kind in ['zero', 'slow', 'unrecognized']:
                with self.subTest(filter=field, kind=kind):
                    p = PLUGIN.QbOptimizer()
                    p.init_plugin({'downloaders':['A'], field:'\r\n keep \n\n',
                                   'enable_auto_redownload':False, 'enable_unrecognized_clean':True})
                    d = Downloader(torrent(**fields))
                    for _ in range(4):
                        p._clean_torrents(d, d.get_torrents()[0], 'A', kind)
                        self.now += 13*3600
                    self.assertEqual(d.deleted, [])

    def test_filter_exact_category_and_tags_keyword_substring(self):
        self.p._exclude_categories = self.p._exclude_labels = 'keep'
        self.p._exclude_keywords = 'my film'
        self.p._compile_filters()
        self.assertTrue(self.p._is_excluded_torrent(torrent(tags=['KEEP'])))
        self.assertTrue(self.p._is_excluded_torrent(torrent(name='MY FILM 2021')))
        self.assertFalse(self.p._is_excluded_torrent(torrent(category='keeper', tags='keep2')))
        self.p._exclude_keywords = self.p._exclude_categories = self.p._exclude_labels = '\n  \n'
        self.p._compile_filters()
        self.assertFalse(self.p._is_excluded_torrent(torrent(name='', category='', tags=None)))

    def test_empty_filters_allow_confirmed_cleanup(self):
        d = Downloader(torrent())
        removed, failures = self.mature(d)
        self.assertEqual((removed, failures), (1, []))
        self.assertEqual(d.deleted, [(True, ['old'])])

    def test_filter_added_between_scan_and_delete(self):
        d = Downloader(torrent())
        self.p._exclude_labels = 'keep'
        self.p._compile_filters()
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'zero')
        self.now += 500
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'zero')
        self.now += 500
        d.fetch_hook = lambda ids: setattr(d.items['old'], 'tags', 'keep') if ids else None
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'zero')
        self.assertEqual(d.deleted, [])

    def test_unknown_seeds_and_connected_or_active_are_not_zero(self):
        for fields in [{'num_complete':-1, 'num_seeds':54, 'dlspeed':1024},
                       {'num_complete':None}, {'num_seeds':1}, {'dlspeed':1}, {'availability':1}]:
            with self.subTest(fields=fields):
                d = Downloader(torrent(**fields))
                self.mature(d)
                self.assertEqual(d.deleted, [])

    def test_missing_availability_is_not_zero_seed_evidence(self):
        d = Downloader(torrent(availability=None))
        self.mature(d)
        self.assertEqual(d.deleted, [])

    def test_fast_old_task_at_99_percent_is_not_slow(self):
        d = Downloader(torrent(progress=.99, dlspeed=100*1024**2, num_complete=10, state='downloading'))
        self.mature(d, 'slow')
        self.assertEqual(d.deleted, [])

    def test_old_stalled_task_requires_new_observation_window(self):
        d = Downloader(torrent())
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'slow')
        self.assertEqual(d.deleted, [])
        self.now += 60
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'slow')
        self.assertEqual(d.deleted, [])

    def test_sustained_slow_is_cleaned(self):
        d = Downloader(torrent(dlspeed=1024, num_complete=10))
        self.assertEqual(self.mature(d, 'slow')[0], 1)

    def test_slow_timer_resets_on_recovery(self):
        d = Downloader(torrent())
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'slow')
        self.now += 13*3600
        d.items['old'].dlspeed = 10*1024**2
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'slow')
        d.items['old'].dlspeed = 0
        self.now += 13*3600
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'slow')
        self.assertEqual(d.deleted, [])

    def test_progress_resets_zero_observation(self):
        d = Downloader(torrent())
        for _ in range(5):
            self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'zero')
            self.now += 500
            d.items['old'].progress += .01
        self.assertEqual(d.deleted, [])

    def test_new_paused_forced_metadata_error_and_completed_are_preserved(self):
        for fields in [{'added_on':self.now}, {'state':'stoppedDL'}, {'state':'forcedDL'},
                       {'force_start':True}, {'state':'metaDL'}, {'state':'error'}, {'progress':1}]:
            for kind in ['zero','slow','unrecognized']:
                with self.subTest(fields=fields, kind=kind):
                    self.assertFalse(self.p._matches_cleanup(torrent(**fields), kind))

    def test_unrecognized_requires_repeat_and_stalled(self):
        d = Downloader(torrent())
        self.assertEqual(self.mature(d, 'unrecognized')[0], 1)
        self.assertFalse(self.p._matches_cleanup(torrent(state='downloading'), 'unrecognized'))

    def test_recognition_exception_retains_task(self):
        with patch.object(PLUGIN, 'MediaChain', side_effect=RuntimeError('temporary service failure')):
            d = Downloader(torrent())
            self.mature(d, 'unrecognized')
            self.assertEqual(d.deleted, [])

    def test_delete_failure_not_counted(self):
        d = Downloader(torrent())
        d.delete_ok = False
        removed, failures = self.mature(d)
        self.assertEqual(removed, 0)
        self.assertTrue(failures)

    def test_shared_file_retained(self):
        d = Downloader(torrent(file_name='shared.mkv'), torrent('seed', file_name='shared.mkv', progress=1, state='uploading'))
        self.assertEqual(self.mature(d)[0], 1)
        self.assertEqual(d.deleted, [(False, ['old'])])

    def test_completed_during_revalidation_not_deleted(self):
        d = Downloader(torrent())
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'zero')
        self.now += 500
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'zero')
        self.now += 500
        d.fetch_hook = lambda ids: setattr(d.items['old'], 'progress', 1) if ids else None
        self.p._clean_torrents(d, d.get_torrents()[0], 'A', 'zero')
        self.assertEqual(d.deleted, [])

    def test_manual_pauses_and_error_tasks_never_resumed(self):
        self.p._enable_download_threshold = True
        d = Downloader(torrent(state='stoppedDL'), torrent('error', state='error'))
        self.p._manage_download_threshold(d, d.get_torrents()[0], 'A')
        self.assertEqual(d.started, [])

    def test_owned_pause_persists_and_resumes(self):
        self.p._enable_download_threshold = True
        d = Downloader(torrent())
        d.free = 100*GIB
        self.p._manage_download_threshold(d, d.get_torrents()[0], 'A')
        self.assertEqual(d.stopped, ['old'])
        self.assertEqual(d.started, [])
        self.p.init_plugin({'downloaders':['A'], 'enable_download_threshold':True})
        d.free = 200*GIB
        self.p._manage_download_threshold(d, d.get_torrents()[0], 'A')
        self.assertEqual(d.started, ['old'])

    def test_resume_is_retained_when_start_is_not_confirmed(self):
        self.p._enable_download_threshold = True
        d = Downloader(torrent(state='stoppedDL'))
        d.free = 200*GIB
        d.start_torrents = Mock(return_value=True)
        self.p._store('A')['paused']['old'] = {'confirmed': True, 'added_on': 1}
        self.p._manage_download_threshold(d, d.get_torrents()[0], 'A')
        self.assertEqual(d.started, [])
        self.assertIn('old', self.p._store('A')['paused'])

    def test_failed_pause_not_owned(self):
        self.p._enable_download_threshold = True
        d = Downloader(torrent())
        d.free, d.stop_ok = 60*GIB, False
        self.p._manage_download_threshold(d, d.get_torrents()[0], 'A')
        self.assertEqual(self.p._store('A')['paused'], {})
        self.assertEqual(d.started, [])

    def test_unknown_space_does_not_resume_or_pause(self):
        self.p._enable_download_threshold = True
        d = Downloader(torrent())
        d.free = -1
        self.assertFalse(self.p._manage_download_threshold(d, d.get_torrents()[0], 'A'))
        self.assertEqual(d.stopped + d.started, [])

    def test_no_resume_when_io_constrained(self):
        self.p._enable_download_threshold = True
        d = Downloader(torrent(state='stoppedDL'))
        self.p._store('A')['paused']['old'] = {'confirmed':True, 'added_on':1}
        self.p._manage_download_threshold(d, d.get_torrents()[0], 'A', allow_resume=False)
        self.assertEqual(d.started, [])

    def test_resume_slots_respected(self):
        self.p._enable_download_threshold = True
        self.p._max_download_slots = 1
        d = Downloader(torrent('active', amount_left=GIB, state='downloading'), torrent(state='stoppedDL', amount_left=GIB))
        self.p._store('A')['paused']['old'] = {'confirmed':True, 'added_on':1}
        self.p._manage_download_threshold(d, d.get_torrents()[0], 'A')
        self.assertEqual(d.started, [])

    def test_disabled_monitor_preserves_manual_limit(self):
        d = Downloader()
        d.limit = 512
        self.assertFalse(self.p._monitor_disk_and_io(d, 'A'))
        self.assertEqual(d.limit, 512)
        self.assertEqual(d.limit_calls, [])

    def test_owned_limit_restores_original_and_preserves_upload(self):
        self.p._enable_disk_monitor = True
        d = Downloader()
        d.limit, d.free = 5000, GIB
        self.assertTrue(self.p._monitor_disk_and_io(d, 'A'))
        self.assertEqual(d.limit, 1024)
        d.free = 200*GIB
        self.assertFalse(self.p._monitor_disk_and_io(d, 'A'))
        self.assertEqual((d.limit, d.upload), (5000, 10240))

    def test_user_limit_change_is_not_overwritten(self):
        self.p._enable_disk_monitor = True
        d = Downloader()
        d.free = GIB
        self.p._monitor_disk_and_io(d, 'A')
        d.limit, d.free = 400, 200*GIB
        self.p._monitor_disk_and_io(d, 'A')
        self.assertEqual(d.limit, 400)
        self.assertNotIn('limit', self.p._store('A'))

    def test_unknown_io_does_not_unlimit(self):
        self.p._enable_disk_monitor = True
        d = Downloader()
        d.free = GIB
        self.p._monitor_disk_and_io(d, 'A')
        d.free, d.cache = 200*GIB, None
        self.assertTrue(self.p._monitor_disk_and_io(d, 'A'))
        self.assertEqual(d.limit, 1024)

    def test_priority_sort_before_limit_without_start(self):
        self.p._max_priority_process = 1
        d = Downloader(torrent('weak', num_complete=10), torrent('fast', num_complete=10, dlspeed=100*1024**2, priority=2))
        self.assertEqual(self.p._boost_priority_torrents(d, d.get_torrents()[0], 'A'), 1)
        self.assertEqual(d.priorities, ['fast'])
        self.assertEqual(d.started, [])
        self.assertEqual(self.p._boost_priority_torrents(d, d.get_torrents()[0], 'A'), 0)

    def test_priority_disabled_queue(self):
        d = Downloader(torrent())
        d.queueing = False
        self.assertEqual(self.p._boost_priority_torrents(d, d.get_torrents()[0], 'A'), 0)
        self.assertEqual(d.priorities, [])

    def test_wrong_media_season_episode_resolution_not_selected(self):
        original = torrent()
        info = vars(original)
        media = N(tmdb_id=1, type='TV')
        def candidate(title, **kwargs):
            return N(meta_info=meta(title), media_info=kwargs.get('media', media),
                     torrent_info=N(title=title, info_hash=kwargs.get('hash','new'), size=80*GIB, seeders=kwargs.get('seeders',20)))
        valid = candidate('Example.2021.S01E01.2160p.WEB-DL-OTHER', seeders=2)
        invalid = [candidate('Example.2021.S02E01.2160p'), candidate('Example.2021.S01E02.2160p'),
                   candidate('Example.2021.S01E01.1080p'), candidate('Example.2022.S01E01.2160p'),
                   candidate(valid.torrent_info.title, media=N(tmdb_id=2,type='TV')),
                   candidate(valid.torrent_info.title, hash='old')]
        with patch.object(PLUGIN, 'MediaChain', return_value=N(recognize_by_meta=lambda _:media)):
            self.assertIs(self.p._select_best_torrent(invalid+[valid], info), valid)

    def test_unknown_original_identity_refuses_replacement(self):
        self.assertIsNone(self.p._select_best_torrent([], vars(torrent())))

    def test_replacement_preserves_original_when_search_fails(self):
        self.p._enable_auto_redownload = True
        self.p._search_torrents = lambda _: []
        d = Downloader(torrent())
        self.assertEqual(self.mature(d)[0], 0)
        self.assertEqual(d.deleted, [])

    def test_replacement_added_before_removal_and_files_kept(self):
        self.p._enable_auto_redownload = True
        self.p._search_torrents = lambda _: ['candidate']
        self.p._select_best_torrent = lambda *args: 'candidate'
        d = Downloader(torrent(tags='KEEP-ME'))
        captured = []
        def add(candidate, info):
            self.assertIn('old', d.items)
            self.assertEqual(d.deleted, [])
            captured.append(info)
            d.items['new'] = torrent('new', state='downloading', added_on=self.now)
            return 'new'
        self.p._download_torrent = add
        self.assertEqual(self.mature(d)[0], 1)
        self.assertEqual(d.deleted, [(False,['old'])])
        self.assertEqual(captured[0]['downloader'], 'A')
        self.assertEqual(captured[0]['tags'], 'KEEP-ME')

    def test_replacement_accepts_boolean_download_result_when_new_task_is_visible(self):
        self.p._enable_auto_redownload = True
        self.p._search_torrents = lambda _: ['candidate']
        self.p._select_best_torrent = lambda *args: 'candidate'
        d = Downloader(torrent())
        def add(candidate, info):
            d.items['new'] = torrent('new', state='downloading', added_on=self.now)
            return True
        self.p._download_torrent = add
        self.assertEqual(self.mature(d)[0], 1)
        self.assertEqual(d.deleted, [(False, ['old'])])

    def test_replacement_add_exception_does_not_repeat(self):
        self.p._search_torrents = lambda _: ['candidate']
        self.p._select_best_torrent = lambda *args: 'candidate'
        self.p._download_torrent = Mock(side_effect=TimeoutError('unknown result'))
        d = Downloader(torrent())
        with self.assertRaises(TimeoutError):
            self.p._replace_torrent(d, d.items['old'], 'A')
        self.assertFalse(self.p._replace_torrent(d, d.items['old'], 'A'))
        self.assertEqual(self.p._download_torrent.call_count, 1)

    def test_download_passes_original_downloader_path_category_tags(self):
        source_media = N(category='Other')
        candidate = N(meta_info=meta('Example'), media_info=source_media, torrent_info=N())
        chain = N(download_single=Mock(return_value='new'))
        with patch.object(PLUGIN, 'DownloadChain', return_value=chain):
            self.p._download_torrent(candidate, {'downloader':'B','save_path':'/old/path','category':'Keep','tags':'a,b'})
        args = chain.download_single.call_args.kwargs
        self.assertEqual((args['downloader'], args['save_path'], args['label']), ('B','/old/path','a,b'))
        self.assertEqual(args['context'].media_info.category, 'Keep')
        self.assertEqual(source_media.category, 'Other')

    def test_stages_get_fresh_snapshots_after_delete(self):
        d = Downloader(torrent())
        self.p._QbOptimizer__get_downloader = lambda _: d
        self.p._monitor_disk_and_io = lambda *args: False
        self.p._manage_download_threshold = lambda *args, **kwargs: False
        seen = []
        def clean(downloader, torrents, name, kind, **kwargs):
            seen.append((kind, len(torrents)))
            if kind == 'zero':
                d.items.clear()
            return 0, []
        self.p._clean_torrents = clean
        self.p._boost_priority_torrents = lambda *args: 0
        self.p.optimize_torrents()
        self.assertEqual(seen, [('zero',1),('slow',0),('unrecognized',0)])

    def test_low_disk_on_first_downloader_does_not_skip_second(self):
        self.p._downloaders = ['A','B']
        self.p._QbOptimizer__get_downloader = lambda _: Downloader()
        seen = []
        self.p._monitor_disk_and_io = lambda downloader,name: name == 'A'
        self.p._manage_download_threshold = lambda downloader,torrents,name,**kwargs: seen.append(name)
        self.p._clean_torrents = lambda *args,**kwargs:(0,[])
        self.p._boost_priority_torrents = lambda *args:0
        self.p.optimize_torrents()
        self.assertEqual(seen, ['A','B'])

    def test_runtime_survives_reload_filters_saved_onlyonce_and_form(self):
        self.p._store('A')['paused']['old'] = {'confirmed':True,'added_on':1}
        self.p._save_runtime()
        self.p.init_plugin({'onlyonce':True, 'exclude_keywords':'keep', 'exclude_categories':'Movies', 'exclude_labels':'pin', 'speed_limit_mbps':.5})
        self.assertEqual(self.p._speed_limit_mbps, .5)
        self.assertEqual(self.p.config['exclude_keywords'], 'keep')
        self.assertEqual(self.p.config['exclude_categories'], 'Movies')
        self.assertEqual(self.p.config['exclude_labels'], 'pin')
        self.assertFalse(self.p.config['onlyonce'])
        self.assertIn('old', self.p._store('A')['paused'])
        with patch.object(PLUGIN, 'DownloaderHelper', return_value=N(get_configs=lambda:{})), patch.object(PLUGIN, 'SiteOper', return_value=N(list_order_by_pri=lambda:[])):
            form, defaults = self.p.get_form()
        def models(nodes):
            result=[]
            for node in nodes:
                if 'model' in node.get('props',{}): result.append(node['props']['model'])
                result.extend(models(node.get('content',[])))
            return result
        fields=models(form)
        for field in ['exclude_keywords','exclude_categories','exclude_labels']:
            self.assertEqual(fields.count(field),1)
            self.assertEqual(defaults[field],'')

    def test_stop_and_overlapping_run_do_not_operate(self):
        self.p._QbOptimizer__get_downloader = Mock()
        self.p.stop_service()
        self.p.optimize_torrents()
        self.p._QbOptimizer__get_downloader.assert_not_called()
        self.p._event.clear()
        with PLUGIN.lock:
            self.p.optimize_torrents()
        self.p._QbOptimizer__get_downloader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
