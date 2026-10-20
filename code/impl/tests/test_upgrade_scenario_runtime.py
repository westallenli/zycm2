"""P10-03: deterministic clocks and anonymous batches, no wall-time sleeps."""

import copy
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from panda_brain.ingestion import ObservationStore
from panda_brain.intake_bridge import IntakeBridge
from panda_brain.persistence import load_workspace, save_workspace
from panda_brain.scenario_driver import ScenarioDriver, ThresholdRule
from panda_brain.scenario_runtime import ScenarioRuntime
from panda_brain.workspace import ScenarioWorkspace


START = datetime.fromisoformat('2026-10-07T12:00:00+08:00')
EVENT = 'anonymous-runtime-event'


def clock(seconds):
    return START + timedelta(seconds=seconds)


def batches(count=5):
    return [[dict(event_id=EVENT, source_record_id=f'anonymous-reading-{index}',
        source_version=1, site_id='anonymous-site', metric='water_level_m',
        unit='m', value=index, observed_at=clock(index * 60 - 3600).isoformat(),
        received_at=clock(index * 60 - 3500).isoformat(), quality_note='匿名模拟')]
        for index in range(count)]


def driver(workspace=None, store=None):
    workspace = workspace if workspace is not None else ScenarioWorkspace('anonymous', event_id=EVENT)
    store = store if store is not None else ObservationStore()
    rule = ThresholdRule('water:anonymous', 'water_level_m', None, '>=', 2, 'met', 'not_met')
    return ScenarioDriver(workspace, store, IntakeBridge(store), (rule,))


class ScenarioRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.driver = driver()
        self.runtime = ScenarioRuntime(self.driver, batches(), scenario_id='anonymous-scenario',
                                       source='anonymous-simulator', now=START)

    def test_default_paused_and_first_minute_drives_without_waiting_for_refresh(self):
        self.assertFalse(self.runtime.status()['playing'])
        self.assertEqual(3, self.runtime.status()['refresh_minutes'])
        self.runtime.play(now=START)
        self.assertIsNone(self.runtime.tick(now=clock(59))['simulation'])
        result = self.runtime.tick(now=clock(60))
        self.assertIsNotNone(result['simulation'])
        self.assertIsNone(result['refresh'])
        self.assertIn('water:anonymous', self.driver.workspace.claims)
        self.assertEqual(1, self.runtime.status()['cursor'])
        self.assertEqual('', self.runtime.status()['last_refresh_at'])
        self.assertIn('模拟', self.driver.workspace.claims['water:anonymous'].source)

    def test_simulation_and_refresh_both_due_are_independent(self):
        self.runtime.play(now=START)
        self.runtime.tick(now=clock(60))
        self.runtime.tick(now=clock(120))
        result = self.runtime.tick(now=clock(180))
        self.assertIsNotNone(result['simulation'])
        self.assertIsNotNone(result['refresh'])
        self.assertEqual(3, self.runtime.status()['cursor'])
        self.assertEqual(clock(180).isoformat(), self.runtime.status()['last_refresh_at'])
        version = self.driver.workspace.version
        self.runtime.tick(now=clock(180))
        self.assertEqual(version, self.driver.workspace.version)

    def test_pause_stops_only_simulation_and_active_input_is_immediate(self):
        self.runtime.play(now=START)
        self.runtime.pause(now=clock(30))
        self.assertIsNone(self.runtime.tick(now=clock(180))['simulation'])
        self.assertEqual(clock(180).isoformat(), self.runtime.status()['last_refresh_at'])
        push = batches(1)[0]
        result = self.runtime.receive(push, source='anonymous-push', data_kind='observed', now=clock(181))
        self.assertTrue(result['validation'].all_passed)
        self.assertIn('water:anonymous', self.driver.workspace.claims)
        self.assertEqual(0, self.runtime.status()['cursor'])
        self.assertFalse(self.runtime.status()['playing'])
        self.assertEqual({}, self.driver.workspace.orders)
        self.assertEqual({}, self.driver.workspace.verifications)

    def test_single_step_advances_exactly_one_batch_and_preserves_source_times(self):
        original = batches()[0][0]
        self.runtime.single_step(now=clock(10))
        record = self.driver.store.observations()[0]
        self.assertEqual(original['observed_at'], record.collected_at)
        self.assertEqual(original['received_at'], record.received_at)
        self.assertEqual(original['received_at'], self.runtime.status()['latest_received_at'])
        self.runtime.refresh_now(now=clock(20))
        self.runtime.tick(now=clock(180))
        self.assertEqual((record,), self.driver.store.observations())
        self.assertEqual(1, self.runtime.status()['cursor'])
        self.assertFalse(self.runtime.status()['playing'])

    def test_manual_refresh_does_not_reset_either_periodic_deadline(self):
        self.runtime.play(now=START)
        before = self.runtime.status()
        self.runtime.refresh_now(now=clock(30))
        after = self.runtime.status()
        self.assertEqual(before['next_simulation_at'], after['next_simulation_at'])
        self.assertEqual(before['next_refresh_at'], after['next_refresh_at'])
        self.assertEqual(0, after['cursor'])
        self.assertIsNotNone(self.runtime.tick(now=clock(60))['simulation'])

    def test_resume_waits_one_minute_without_replaying_paused_time(self):
        self.runtime.play(now=START)
        self.runtime.pause(now=clock(30))
        self.runtime.play(now=clock(300))
        self.assertIsNone(self.runtime.tick(now=clock(359))['simulation'])
        self.assertIsNotNone(self.runtime.tick(now=clock(360))['simulation'])
        self.assertEqual(1, self.runtime.status()['cursor'])

    def test_delayed_tick_delivers_only_one_batch_and_reports_delay_without_skipping(self):
        self.runtime.play(now=START)
        result = self.runtime.tick(now=clock(600))
        self.assertEqual(540, result['simulation']['delay_seconds'])
        self.assertEqual(1, self.runtime.status()['cursor'])
        self.assertEqual(clock(660).isoformat(), self.runtime.status()['next_simulation_at'])
        self.assertIsNone(self.runtime.tick(now=clock(600))['simulation'])

    def test_refresh_options_are_strict_and_do_not_change_simulation_deadline(self):
        self.runtime.play(now=START)
        for value in (3, 5, 10, 30, 60):
            self.runtime.set_refresh_minutes(value, now=clock(10))
            self.assertEqual(value, self.runtime.status()['refresh_minutes'])
            self.assertEqual(clock(10 + value * 60).isoformat(), self.runtime.status()['next_refresh_at'])
            self.assertEqual(clock(60).isoformat(), self.runtime.status()['next_simulation_at'])
        before, version = self.runtime.status(), self.driver.workspace.version
        for value in (1, 2, 90, True, 3.0, '3'):
            with self.assertRaises(ValueError):
                self.runtime.set_refresh_minutes(value, now=clock(11))
        self.assertEqual(before, self.runtime.status())
        self.assertEqual(version, self.driver.workspace.version)

    def test_exhaustion_stops_without_loop_or_extra_writes(self):
        for index in range(5):
            self.runtime.single_step(now=clock(index))
        status, version = self.runtime.status(), self.driver.workspace.version
        self.assertTrue(status['completed'])
        self.runtime.single_step(now=clock(6))
        self.runtime.play(now=clock(7))
        self.assertEqual(version, self.driver.workspace.version)
        self.assertEqual(status, self.runtime.status())

    def test_queued_batches_are_copied_and_prevalidated_before_any_workspace_write(self):
        payload = batches()
        runtime = ScenarioRuntime(self.driver, payload, scenario_id='copy', source='sim', now=START)
        payload[0][0]['value'] = 999
        runtime.single_step(now=START)
        self.assertEqual(0, self.driver.store.observations()[0].value)
        version = self.driver.workspace.version
        for bad in (dict(batches()[0][0], event_id='other'),
                    dict(batches()[0][0], verification_status='verified')):
            with self.assertRaises(ValueError):
                ScenarioRuntime(self.driver, [[bad]], scenario_id='bad', source='sim', now=START)
        self.assertEqual(version, self.driver.workspace.version)

    def test_clock_binding_and_audit_errors_are_rejected_before_progress_or_ingestion(self):
        self.runtime.single_step(now=clock(10))
        before, version = self.runtime.status(), self.driver.workspace.version
        for bad in (clock(9), START.replace(tzinfo=None)):
            with self.assertRaises(ValueError):
                self.runtime.single_step(now=bad)
        self.assertEqual(version, self.driver.workspace.version)
        self.assertEqual(before, self.runtime.status())
        self.driver.workspace.event_id = 'other'
        with self.assertRaises(ValueError):
            self.runtime.tick(now=clock(11))
        self.driver.workspace.event_id = EVENT
        event = next(e for e in self.driver.workspace.events if e.kind == 'ScenarioRuntimeCheckpoint')
        event.payload['cursor'] = 99
        with self.assertRaises(ValueError):
            self.runtime.single_step(now=clock(11))
        self.assertEqual(1, len(self.driver.store.observations()))

    def test_step_while_playing_is_rejected_and_repeat_play_pause_are_idempotent(self):
        self.runtime.play(now=START)
        version = self.driver.workspace.version
        self.runtime.play(now=clock(1))
        with self.assertRaises(ValueError):
            self.runtime.single_step(now=clock(2))
        self.assertEqual(version, self.driver.workspace.version)
        self.runtime.pause(now=clock(3))
        version = self.driver.workspace.version
        self.runtime.pause(now=clock(4))
        self.assertEqual(version, self.driver.workspace.version)

    def test_restart_restores_cursor_interval_and_receipt_but_requires_play_again(self):
        self.runtime.play(now=START)
        self.runtime.tick(now=clock(60))
        self.runtime.set_refresh_minutes(5, now=clock(61))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.db'
            save_workspace(self.driver.workspace, path, observation_store=self.driver.store)
            store = ObservationStore()
            workspace = load_workspace(path, observation_store=store)
        runtime = ScenarioRuntime(driver(workspace, store), batches(), scenario_id='anonymous-scenario',
                                  source='anonymous-simulator', now=clock(100))
        self.assertEqual(1, runtime.status()['cursor'])
        self.assertEqual(5, runtime.status()['refresh_minutes'])
        self.assertFalse(runtime.status()['playing'])
        self.assertEqual(batches()[0][0]['received_at'], runtime.status()['latest_received_at'])
        runtime.single_step(now=clock(101))
        self.assertEqual(2, runtime.status()['cursor'])
        self.assertEqual(2, len(store.observations()))
        self.assertEqual((), workspace.verify_audit_chain())

    def test_same_scenario_id_rejects_changed_sequence_or_source_after_checkpoint(self):
        self.runtime.single_step(now=START)
        changed = copy.deepcopy(batches())
        changed[0][0]['received_at'] = clock(100).isoformat()
        for payload, source in ((changed, 'anonymous-simulator'), (batches(), 'different-source')):
            with self.assertRaises(ValueError):
                ScenarioRuntime(self.driver, payload, scenario_id='anonymous-scenario', source=source,
                                now=clock(1))

    def test_stale_runtime_instance_cannot_consume_progress_written_by_another(self):
        other = ScenarioRuntime(self.driver, batches(), scenario_id='anonymous-scenario',
                                source='anonymous-simulator', now=START)
        self.runtime.single_step(now=START)
        version = self.driver.workspace.version
        with self.assertRaises(ValueError):
            other.single_step(now=clock(1))
        self.assertEqual(version, self.driver.workspace.version)
        self.assertEqual(1, len(self.driver.store.observations()))

    def test_immutable_source_conflict_pauses_and_keeps_entire_batch_unconsumed(self):
        queued = batches(1)[0]
        extra = dict(queued[0], source_record_id='extra', site_id='anonymous-site-b')
        runtime = ScenarioRuntime(self.driver, [queued + [extra]], scenario_id='conflict',
                                  source='sim', now=START)
        self.driver.load_source_sequence([dict(queued[0], value=999)], source='sim', data_kind='simulated')
        before = self.driver.store.observations()
        runtime.play(now=START)
        result = runtime.tick(now=clock(60))
        self.assertFalse(result['simulation']['consumed'])
        self.assertTrue(result['simulation']['validation'].rejected)
        self.assertEqual(0, runtime.status()['cursor'])
        self.assertFalse(runtime.status()['playing'])
        self.assertTrue(runtime.status()['last_error'])
        self.assertEqual(before, self.driver.store.observations())

    def test_restart_can_read_last_received_from_audit_without_observation_cache(self):
        self.runtime.single_step(now=START)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.db'
            save_workspace(self.driver.workspace, path)
            workspace = load_workspace(path)
        runtime = ScenarioRuntime(driver(workspace), batches(), scenario_id='anonymous-scenario',
                                  source='anonymous-simulator', now=clock(1))
        self.assertEqual(batches()[0][0]['received_at'], runtime.status()['latest_received_at'])

    def test_rejected_active_event_and_unknown_receipt_never_fabricate_last_receive_time(self):
        foreign = dict(batches()[0][0], event_id='foreign')
        result = self.runtime.receive([foreign], source='push', data_kind='observed', now=START)
        self.assertTrue(result['validation'].rejected)
        self.assertEqual('', self.runtime.status()['latest_received_at'])
        self.assertFalse(self.driver.workspace.evidence)
        unknown = dict(batches()[0][0])
        unknown.pop('received_at')
        self.runtime.receive([unknown], source='push', data_kind='observed', now=clock(1))
        self.runtime.refresh_now(now=clock(2))
        self.assertEqual('', self.runtime.status()['latest_received_at'])
        self.assertEqual('', self.driver.store.observations()[0].received_at)

    def test_empty_sequence_keeps_refresh_available_and_never_generates_sample_data(self):
        runtime = ScenarioRuntime(self.driver, [], scenario_id='empty', source='sim', now=START)
        self.assertTrue(runtime.status()['completed'])
        self.assertFalse(runtime.play(now=START)['playing'])
        self.assertIsNone(runtime.single_step(now=START)['simulation'])
        result = runtime.tick(now=clock(180))
        self.assertEqual('bootstrap_required', result['refresh']['status'])
        self.assertFalse(self.driver.store.observations())


if __name__ == '__main__':
    unittest.main()
