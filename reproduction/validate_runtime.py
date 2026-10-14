"""One bounded, real simulation/Nav2 check. No account or Agent prompt.

This is a subset of navigation qualification, not smart-ward Agent acceptance.
It uses unchanged production resource, arrival, physical-stop and ownership gates.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time
import traceback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    sys.path.insert(0, str(root / 'portable'))
    from portable_config import read_config, runtime_env
    config = read_config(root, root / 'portable/config.json')
    os.environ.update(runtime_env(root, config))
    sys.path.insert(0, str(root))
    from mhs.smart_navigation import Qualification, recent
    expected_devices = {'hospital-robot-01', 'hospital-cabinet-b', 'hospital-cabinet-c',
                        'hospital-cabinet-lab', 'hospital-light-301', 'hospital-light-303',
                        'hospital-sensor-301', 'hospital-sensor-303'}

    cases = ['fresh_simulation_and_eight_devices', 'real_arrival_depot',
             'same_operation_no_second_navigation', 'cancel_actual_motion',
             'new_goal_after_confirmed_stop']

    class ReproductionCheck(Qualification):
        def __init__(self):
            options = argparse.Namespace(output=str(args.output), wall_seconds=1500, leg_seconds=900)
            super().__init__(options)
            self.report.update(scope='Independent installation, live Isaac devices, bounded real MHS/Nav2 navigation and physical stop; NOT real Agnes acceptance',
                               suite_version='reproduction-runtime-v1',
                               account_verified=False, real_agent_verified=False,
                               real_agent_status='NA: user will configure their own account later',
                               limits={'wall_seconds': 1500, 'work_seconds': 1350,
                                       'leg_wall_seconds': 900, 'maximum_navigation_tasks': 3,
                                       'cleanup_reserve_wall_seconds': 150},
                               unexecuted=['real Agnes task decomposition', 'natural-language orders',
                                           'specimen handoff', 'lighting recovery', 'urgent insertion',
                                           'Hub disconnect and adapter-exit fault tests', 'complete full navigation qualification'])
            self.report.pop('budget_revision', None)

        async def execute(self):
            current = 'preflight'
            try:
                await self.preflight()
                await self.connect_hub()
                current = cases[0]
                before, first_scene = await self.state(enforce_empty=False)
                first_frame = await self.api('/api/frame')
                first_person_before = await self.api('/api/frame?view=first_person')
                await asyncio.sleep(4)
                after, second_scene = await self.state(enforce_empty=False)
                second_frame = await self.api('/api/frame')
                first_person_after = await self.api('/api/frame?view=first_person')
                devices = (await self.rpc('hub/devices', {})).get('devices', [])
                identifiers = [device.get('id') for device in devices]
                actual_devices = after.get('smart_ward', {}).get('devices', {})
                checks = {'exactly_eight_hospital_devices': len(devices) == 8 and set(identifiers) == expected_devices,
                          'all_devices_available': all(device.get('available') for device in devices),
                          'all_business_device_samples_fresh_and_available': set(actual_devices) == expected_devices and all(device.get('fresh') is True and device.get('available') is True for device in actual_devices.values()),
                          'simulation_time_advances': after.get('sim_time', 0) > before.get('sim_time', 0),
                          'new_scene_observation': second_scene.get('wall_time', 0) > first_scene.get('wall_time', 0),
                          'fresh_scene': recent(second_scene.get('wall_time'), 3),
                          'new_fresh_camera_capture': second_frame.get('capture_wall_time', 0) > first_frame.get('capture_wall_time', 0) and recent(second_frame.get('capture_wall_time'), 5),
                          'new_fresh_first_person_capture': first_person_after.get('capture_wall_time', 0) > first_person_before.get('capture_wall_time', 0) and recent(first_person_after.get('capture_wall_time'), 5),
                          'navigation_ready': after.get('navigation_ready') is True,
                          'pose_fresh': after.get('pose_fresh') is True,
                          'watch_remains_disabled': not after.get('smart_ward', {}).get('watch', {}).get('enabled')}
                self.record('fresh_runtime_checks', checks=checks, devices=devices,
                            first_frame=first_frame, second_frame=second_frame,
                            first_person_before=first_person_before, first_person_after=first_person_after,
                            first_sim_time=before.get('sim_time'), second_sim_time=after.get('sim_time'))
                if not all(checks.values()):
                    raise RuntimeError('fresh_runtime_check_failed:' + ','.join(name for name, value in checks.items() if not value))
                self.passed(current, {'checks': checks, 'devices': devices, 'first_frame': first_frame, 'second_frame': second_frame})
                await self.begin_segment(1)
                self.segment_records[-1]['cases'] = cases[1:]
                current = cases[1]
                arrival = await self.arrival('depot')
                self.passed(current, arrival)
                current = cases[2]
                self.passed(current, await self.duplicate(arrival))
                current = cases[3]
                stopped = await self.cancel_motion()
                stable = await self.stable_no_resume(stopped['navigation']['task_id'])
                self.passed(current, {'stop': stopped, 'no_automatic_resume': stable})
                current = cases[4]
                self.passed(current, await self.arrival('depot'))
                await self.end_segment(1, reason='bounded_reproduction_navigation_finished')
                self.report['status'] = 'passed'
            except Exception as error:
                self.report.update(status='NA' if not self.claimed else 'failed_or_incomplete',
                                   failure={'case': current, 'error': repr(error), 'traceback': traceback.format_exc(), 'wall_time': time.time()})
                self.results.append({'case': current, 'status': 'failed' if self.claimed else 'NA', 'pass': False if self.claimed else None, 'error': repr(error)})
                self.record('reproduction_failure', **self.report['failure'])
            finally:
                complete = {row['case'] for row in self.results}
                for name in cases:
                    if name not in complete:
                        self.results.append({'case': name, 'status': 'NA', 'pass': None, 'reason': 'Not executed after prior failure; no automatic retry'})
                await self.cleanup()
                try:
                    database = self.root / 'agent/agh-home-mhs/data/sessions.db'
                    if not database.is_file():
                        raise RuntimeError('new_home_model_request_ledger_missing')
                    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
                    try:
                        count = connection.execute("SELECT COUNT(*) FROM events WHERE type='request/sent'").fetchone()[0]
                    finally:
                        connection.close()
                    self.report['all_model_requests_in_new_home'] = count
                    if count != 0:
                        raise RuntimeError('unexpected_model_request_in_account_free_reproduction')
                except Exception as error:
                    self.report['model_request_audit_error'] = repr(error)
                    self.report['all_model_requests_in_new_home'] = None
                    if self.report.get('status') == 'passed':
                        self.report['status'] = 'failed_or_incomplete'
                self.report.update(ended_wall=time.time(), elapsed_wall_seconds=time.monotonic() - self.started)
                self.save()
                print(json.dumps({'status': self.report['status'], 'report': str(self.directory / 'report.json')}, ensure_ascii=False), flush=True)
            return 0 if self.report['status'] == 'passed' else 2

    async def run():
        test = ReproductionCheck()
        loop = asyncio.get_running_loop()
        for name in ('SIGINT', 'SIGTERM'):
            loop.add_signal_handler(getattr(signal, name), setattr, test, 'interrupted', True)
        return await test.execute()

    return asyncio.run(run())


if __name__ == '__main__':
    raise SystemExit(main())
