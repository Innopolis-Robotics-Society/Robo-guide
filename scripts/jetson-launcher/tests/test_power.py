"""Power operations use fakes only; never run shutdown, sudo or host Docker."""
import asyncio
import pytest
from guide_launcher.stack import RunResult
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
@pytest.mark.parametrize('state', ['idle', 'navigating', 'unknown', None])
def test_power_is_independent_of_ros(tmp_path, action, helper, state):
    async def go():
        async with env(tmp_path, power_control_enabled=True) as e:
            await e.login()
            if state:
                e.set_frame(state_name=state, moving=state=='navigating')
            calls=[]
            async def forbidden(*args):
                raise AssertionError('Power must not call ROS or Docker')
            async def execute(argv, timeout):
                calls.append(argv)
                return RunResult(0,'')
            e.monitor._run=forbidden
            e.app['api'].bridge.command=forbidden
            e.app['api'].restart_guard=lambda: (_ for _ in ()).throw(AssertionError('No ROS guard'))
            e.app['power'].run=execute
            # Even an ongoing ROS/network transition must not delay power.
            async with e.app['api']._control_lock, e.app['system_settings'].lock:
                response=await post(e, action)
                assert response.status==202
            op=await response.json()
            repeat=await post(e, action)
            assert (await repeat.json())['id']==op['id']
            await asyncio.gather(*list(e.app['api'].events.tasks))
            assert calls==[['sudo','-n','/usr/local/sbin/guide-system-power',helper]]
            result=await (await e.client.get('/api/v1/operations/'+op['id'])).json()
            assert result['result']['delay_s']==0
            assert result['status']=='succeeded'
            assert (await post(e, action, key='another')).status==409
    run(go())


@pytest.mark.parametrize('rc,expected', [(-1,'unknown'),(1,'failed')])
def test_power_failure_and_timeout(tmp_path, rc, expected):
    async def go():
        async with env(tmp_path, power_control_enabled=True) as e:
            await e.login(); e.set_frame()
            async def command(*args): return RunResult(rc,'not exposed')
            e.app['power'].run=command
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
