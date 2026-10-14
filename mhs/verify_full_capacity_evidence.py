"""Read-only audit of the frozen 2.0.11 full-capacity webpage attempt."""
import json, math
from datetime import datetime
from pathlib import Path


def verify(directory):
    directory = Path(directory)
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    rows = [json.loads(line) for line in (directory / 'session.jsonl').read_text(encoding='utf-8').splitlines()]
    prompts = [r for r in rows if r['type'] == 'user/message' and r['data'].get('kind') == 'prompt' and not any(x.get('text','').startswith('[AgnesHub]') for x in r['data'].get('content',[]))]
    assert len(prompts) == 2, 'expected_exactly_two_actual_human_prompts'
    urgent_wall = datetime.fromisoformat(prompts[1]['ts'].replace('Z', '+00:00')).timestamp()
    before = None
    with (directory / 'state.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            observed = json.loads(line)
            if observed['wall_time'] <= urgent_wall:
                before = observed['state']
            else:
                break
    assert before is not None
    nav = before.get('navigation') or {}
    loaded = sum(o['loaded'] for o in before['orders'].values())
    # Event snapshots do not sample every position. Require the fresh raw MHS
    # pose and progress immediately before the actual prompt, not a stale pose.
    wire = json.loads((directory / 'urgent-trigger-wire.json').read_text(encoding='utf-8'))
    assert abs(wire['prompt_wall']-urgent_wall) < .001
    poses = [r for r in wire['rows'] if r['time'] <= urgent_wall and r['message'].get('source') == 'pose']
    progress = [r for r in wire['rows'] if r['time'] <= urgent_wall and r['message'].get('method') == 'mhs/progress']
    pose = poses[-1]['message']['data'] if poses else {}
    nav_data = progress[-1]['message']['params']['data'] if progress else {}
    trigger = bool(poses and progress and loaded == 2 and nav.get('goal_station') == 'waiting' and nav_data.get('nav_task_id') == nav.get('nav_task_id') and nav_data.get('status') == 'running' and pose.get('ok') and urgent_wall-poses[-1]['message']['t'] < 2 and math.hypot(pose['x']+40, pose['y']-20) > 1.2)
    state = json.loads((directory / 'final-state.json').read_text(encoding='utf-8'))
    orders = state['orders']
    receipts = sorted(report['receipts'], key=lambda r: r['sim_time'])
    urgent = [r for r in receipts if orders[r['order_id']]['priority'] == 'urgent']
    at_a = [r for r in receipts if r['station'] == 'station_a']
    audit = json.loads((directory / 'session-audit.json').read_text(encoding='utf-8'))
    result = {
        'source_version': '2.0.11', 'run_id': report['run_id'],
        'actual_human_prompts': [r['data']['content'] for r in prompts],
        'full_capacity_during_actual_departure': trigger,
        'observed_loaded_at_trigger': loaded,
        'fresh_pose_before_prompt': poses[-1] if poses else None,
        'fresh_navigation_before_prompt': progress[-1] if progress else None,
        'audit_note': 'The earlier event-snapshot-only verdict was insufficient for the departure trigger. This audit uses raw timestamped MHS pose and navigation; the earlier audit remains separate.',
        'urgent_before_remaining_normal_A': bool(urgent and at_a and urgent[0]['sim_time'] < at_a[0]['sim_time']),
        'actual_delivery_order': [r['station'] for r in receipts],
        'all_three_exact_orders_delivered': report['delivered'] == report['total'] == 3 and set(report['order_ids']) == set(orders),
        'agent_verified_and_measured_stopped': report['agent_pass'] and report['final_stopped'] and report['remaining_cargo'] == 0,
        'rejected_calls_retained': audit['rejected_calls'],
        'built_in_completion_contexts': sum(r['type'] == 'user/message' and r['data'].get('kind') == 'runtime_context' and any(x.get('text','').startswith('本轮配送尚未完成') for x in r['data'].get('content',[])) for r in rows),
        'limitation': 'Business completion passes. One mistaken visit to pharmacy before urgent pickup and its rejected pickup remain; not a flawless routing or latest-version regression claim.',
    }
    result['pass'] = all(result[k] for k in ['full_capacity_during_actual_departure','urgent_before_remaining_normal_A','all_three_exact_orders_delivered','agent_verified_and_measured_stopped'])
    (directory / 'acceptance-verdict-with-wire.json').open('x', encoding='utf-8').write(json.dumps(result,ensure_ascii=False,indent=2))
    return result


if __name__ == '__main__':
    import sys
    print(json.dumps(verify(sys.argv[1]),ensure_ascii=False,indent=2))
