"""Power operations use fakes only; never run shutdown, sudo or host Docker."""
import asyncio
import pytest
from guide_launcher.stack import RunResult, StackError
from test_api import env, run


async def post(e, action='shutdown', key='power-one', body=None):
    return await e.client.post('/api/v1/system/'+action,
        json={'confirm': True} if body is None else body,
        headers={'Idempotency-Key': key})


def test_auth_confirmation_and_disabled_power(tmp_path):
    async def go():
        async with env(tmp_path) as e:
            assert (await post(e)).status == 401
            assert (await e.client.get('/api/v1/system/power')).status == 401
            await e.login()
            assert (await post(e)).status == 503
            e.app['api'].cfg.power_control_enabled = True
            assert (await post(e, body={'confirm': False})).status == 400
            assert (await post(e, action='arbitrary')).status == 404
    run(go())


@pytest.mark.parametrize('action,helper', [('shutdown','poweroff'),('reboot','reboot')])
def test_idle_power_stops_ros_before_scheduling_and_fences_commands(tmp_path, action, helper):
    async def go():
        async with env(tmp_path, power_control_enabled=True) as e:
            await e.login(); e.set_frame()
            async def idle(*args): return 200, {'success': True}
            e.app['api'].bridge.command = idle
            power = e.app['power']; calls = []; release = asyncio.Event()
            async def stop():
                calls.append('stop')
                await release.wait()
            async def execute(argv, timeout):
                calls.append(argv)
                return RunResult(0,'')
            e.monitor.stop_for_power = stop; power.run = execute
            response = await post(e, action)
            assert response.status == 202
            op = await response.json()
            repeat = await post(e, action)
            assert (await repeat.json())['id'] == op['id']
            assert (await e.client.post('/api/v1/stack/start',json={})).status == 409
            assert (await post(e, action, key='other')).status == 409
            assert (await e.client.post('/api/v1/operations/'+op['id']+'/cancel',json={})).status == 409
            release.set()
            await asyncio.gather(*list(e.app['api'].events.tasks))
            assert calls == ['stop',['sudo','-n','/usr/local/sbin/guide-system-power',helper]]
            result = await (await e.client.get('/api/v1/operations/'+op['id'])).json()
            assert result['result']['scheduled'] and result['status']=='succeeded'
    run(go())


@pytest.mark.parametrize('frame', [None, {'state_name':'navigating','moving':True}])
def test_unknown_or_moving_rejects_power(tmp_path, frame):
    async def go():
        async with env(tmp_path, power_control_enabled=True) as e:
            await e.login()
            if frame: e.set_frame(**frame)
            assert (await post(e)).status==409
            assert e.app['power'].operation is None
    run(go())


def test_failed_ros_stop_never_schedules_power(tmp_path):
    async def go():
        async with env(tmp_path, power_control_enabled=True) as e:
            await e.login(); e.set_frame()
            async def idle(*args): return 200, {'success':True}
            e.app['api'].bridge.command=idle
            async def fail(): raise StackError(409,'stop_failed')
            async def forbidden(*args): raise AssertionError('must not schedule')
            e.monitor.stop_for_power=fail; e.app['power'].run=forbidden
            op=await (await post(e)).json()
            await asyncio.gather(*list(e.app['api'].events.tasks))
            result=await (await e.client.get('/api/v1/operations/'+op['id'])).json()
            assert result['status']=='failed'
            assert not e.app['power'].pending
    run(go())


@pytest.mark.parametrize('rc,expected', [(-1,'unknown'),(1,'failed')])
def test_power_failure_and_timeout(tmp_path, rc, expected):
    async def go():
        async with env(tmp_path, power_control_enabled=True) as e:
            await e.login(); e.set_frame()
            async def idle(*args): return 200, {'success':True}
            async def stop(): pass
            async def command(*args): return RunResult(rc,'not exposed')
            e.app['api'].bridge.command=idle
            e.monitor.stop_for_power=stop; e.app['power'].run=command
            op=await (await post(e)).json()
            await asyncio.gather(*list(e.app['api'].events.tasks))
            result=await (await e.client.get('/api/v1/operations/'+op['id'])).json()
            assert result['status']==expected
            assert e.app['power'].pending == (rc == -1)
            assert 'not exposed' not in str(result)
    run(go())


def test_pending_marker_survives_api_restart(tmp_path, monkeypatch):
    from guide_launcher import power
    marker=tmp_path/'power.pending'; marker.write_text('poweroff')
    monkeypatch.setattr(power,'MARKER',marker)
    async def go():
        async with env(tmp_path, power_control_enabled=True) as e:
            await e.login()
            assert (await post(e)).status==409
            assert (await e.client.post('/api/v1/stack/start',json={})).status==409
            status=await (await e.client.get('/api/v1/system/power')).json()
            assert status['pending']
    run(go())
