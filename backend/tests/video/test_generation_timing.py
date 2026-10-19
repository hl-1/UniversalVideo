import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from unittest.mock import patch

from backend.infrastructure import generation
from backend.modules.video import schemas, summary, tasks
from backend.modules.video.timing import ExecutionTimer, freeze_saved


class TimingTests(unittest.TestCase):
    def test_duration_ignores_wall_clock_jump_and_freezes_at_terminal(self):
        with patch('backend.modules.video.timing.time.monotonic', return_value=10) as clock, \
                patch('backend.modules.video.timing.time.time', return_value=100) as wall:
            timer = ExecutionTimer()
            clock.return_value = 12
            wall.return_value = 1
            timer.stop('queue')
            timer.start('model', '模型')
            clock.return_value = 15
            timer.finish('failed')
            clock.return_value = 1000
            result = timer.snapshot()
        self.assertEqual(result['elapsed_ms'], 5000)
        self.assertEqual(result['stages'][0]['elapsed_ms'], 2000)
        self.assertEqual(result['stages'][1]['elapsed_ms'], 3000)
        self.assertEqual(result['stages'][1]['status'], 'failed')

    def test_completed_stages_survive_later_failure(self):
        timer = ExecutionTimer()
        timer.stop('queue')
        with timer.stage('source', '字幕'):
            pass
        with self.assertRaises(ValueError):
            with timer.stage('model', '模型'):
                raise ValueError()
        timer.finish('failed')
        states = {s['id']: s['status'] for s in timer.snapshot()['stages']}
        self.assertEqual(states['source'], 'finished')
        self.assertEqual(states['model'], 'failed')

    def test_interrupted_snapshot_excludes_unknown_offline_time(self):
        timer = ExecutionTimer()
        timer.stop('queue')
        timer.start('model', '模型')
        saved = timer.snapshot()
        frozen = freeze_saved(saved)
        self.assertIsNone(saved['finished_at_ms'])
        self.assertEqual(frozen['elapsed_ms'], saved['elapsed_ms'])
        self.assertEqual(frozen['finished_at_ms'], saved['accepted_at_ms'] + saved['elapsed_ms'])
        self.assertEqual(frozen['stages'][-1]['status'], 'failed')
        self.assertEqual(frozen['stages'][0]['status'], 'finished')
        self.assertEqual(freeze_saved(frozen), frozen)
        self.assertIsNone(freeze_saved(None))


class FanOutTests(unittest.TestCase):
    def test_bounded_parallelism_preserves_order_and_copies_context(self):
        owner = ContextVar('test_owner', default=None)
        owner.set('account-42')
        barrier = threading.Barrier(2)
        active = 0
        maximum = 0
        lock = threading.Lock()
        def work(index):
            nonlocal active, maximum
            self.assertEqual(owner.get(), 'account-42')
            with lock:
                active += 1
                maximum = max(active, maximum)
            barrier.wait(timeout=2)
            if index % 2 == 0:
                time.sleep(.01)
            with lock:
                active -= 1
            return index
        with ThreadPoolExecutor(max_workers=4) as executor:
            result = generation.bounded_map(work, range(6), executor=executor, workers=2)
        self.assertEqual(result, list(range(6)))
        self.assertEqual(maximum, 2)

    def test_failed_chunk_stops_new_dispatch(self):
        calls = []
        def work(index):
            calls.append(index)
            raise ValueError('chunk failed')
        with ThreadPoolExecutor(max_workers=3) as executor, self.assertRaises(ValueError):
            generation.bounded_map(work, range(100), executor=executor, workers=1)
        self.assertEqual(calls, [0])

    def test_cancelled_provider_wait_never_consumes_slot(self):
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        def cancelled():
            raise RuntimeError('cancelled')
        with patch.object(generation, 'provider_slots', slots), self.assertRaisesRegex(RuntimeError, 'cancelled'):
            with generation.provider_slot(cancelled):
                self.fail('request started')
        slots.release()
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.task = tasks.SummaryTask('parallel-test', 'https://example.com/video')
        self.task.generate_mind_map = True
        self.segments = [schemas.SubtitleSegment(start=0, timestamp='0:00', text='可验证的字幕')]
        self.map = schemas.MindMapResult(title='导图', generated_at='2026-10-19T00:00:00Z', nodes=[
            schemas.MindMapNode(id='node-1', title='字幕', timestamp=0, segment_ids=[0])])

    def tearDown(self):
        tasks.mind_map_tasks.pop(self.task.task_id, None)

    def run_pipeline(self, summary_call, map_call):
        source = ({'title': '标题', 'webpage_url': self.task.url}, 'zh', 'manual', self.segments)
        with patch.object(summary.video_subtitles, '_extract_summary_source', return_value=source), \
                patch.object(summary, '_load_ai_config', return_value={'api_key': 'test', 'model': 'pinned', 'base_url': 'https://example.com/v1'}), \
                patch.object(summary, '_generate_ai_summary', side_effect=summary_call), \
                patch.object(summary, '_generate_ai_mind_map', side_effect=map_call), \
                patch.object(summary, '_save_summary_files', side_effect=lambda task, result: result):
            summary._run_summary(self.task)
            self.task.map_future.result(timeout=3)

    def test_map_starts_before_summary_finishes(self):
        started = threading.Event()
        def generate_summary(*args, **kwargs):
            self.assertTrue(started.wait(timeout=2), 'map was serialized behind summary')
            return '摘要'
        def generate_map(*args):
            self.assertEqual(generation.text_config.get()['model'], 'pinned')
            self.assertEqual(len(args[1]), 1)
            started.set()
            return self.map
        self.run_pipeline(generate_summary, generate_map)
        self.assertEqual(self.task.status, 'finished')
        self.assertEqual(self.task.map_task.status, 'finished')
        self.assertEqual(self.task.to_response().mind_map_task_id, self.task.map_task.task_id)
        self.assertIsNotNone(self.task.map_task.to_response().timing['finished_at_ms'])

    def test_summary_failure_keeps_successful_map(self):
        self.run_pipeline(lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('summary failed')), lambda *args: self.map)
        self.assertEqual(self.task.status, 'failed')
        self.assertEqual(self.task.map_task.status, 'finished')

    def test_source_failure_marks_reserved_map_failed(self):
        summary.create_pipeline_map(self.task)
        with patch.object(summary.video_subtitles, '_extract_summary_source', side_effect=RuntimeError('no source')):
            summary._run_summary(self.task)
        self.assertEqual(self.task.status, 'failed')
        self.assertEqual(self.task.map_task.status, 'failed')
        self.assertIsNone(self.task.map_future)

    def test_map_failure_keeps_successful_summary(self):
        self.run_pipeline(lambda *args, **kwargs: '成功摘要', lambda *args: (_ for _ in ()).throw(RuntimeError('map failed')))
        self.assertEqual(self.task.status, 'finished')
        self.assertEqual(self.task.map_task.status, 'failed')
        self.assertEqual(self.task.result.summary_markdown, '成功摘要')

    def test_cancelled_summary_does_not_dispatch_children(self):
        self.task.cancel.set()
        summary._run_summary(self.task)
        self.assertIsNone(self.task.map_future)
        self.assertIsNone(self.task.map_task)


if __name__ == '__main__':
    unittest.main()
