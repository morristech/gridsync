# -*- coding: utf-8 -*-

import os
try:
    from unittest.mock import MagicMock
except ImportError:
    from mock import MagicMock

import pytest
from twisted.internet.defer import returnValue

from gridsync.errors import NodedirExistsError
from gridsync.tahoe import (
    is_valid_furl, get_nodedirs, TahoeError, TahoeCommandError, TahoeWebError,
    Tahoe)


def fake_get(*args, **kwargs):
    response = MagicMock()
    response.code = 200
    return response


def fake_get_code_500(*args, **kwargs):
    response = MagicMock()
    response.code = 500
    return response


def fake_put(*args, **kwargs):
    response = MagicMock()
    response.code = 200
    return response


def fake_put_code_500(*args, **kwargs):
    response = MagicMock()
    response.code = 500
    return response


def fake_post(*args, **kwargs):
    response = MagicMock()
    response.code = 200
    return response


def fake_post_code_500(*args, **kwargs):
    response = MagicMock()
    response.code = 500
    return response


@pytest.fixture(scope='module')
def tahoe(tmpdir_factory):
    client = Tahoe(str(tmpdir_factory.mktemp('tahoe')), executable='tahoe_exe')
    with open(os.path.join(client.nodedir, 'tahoe.cfg'), 'w') as f:
        f.write('[node]\nnickname = default')
    with open(os.path.join(client.nodedir, 'icon.url'), 'w') as f:
        f.write('test_url')
    private_dir = os.path.join(os.path.join(client.nodedir, 'private'))
    os.mkdir(private_dir)
    with open(os.path.join(private_dir, 'aliases'), 'w') as f:
        f.write('test_alias: test_cap')
    with open(os.path.join(private_dir, 'magic_folders.yaml'), 'w') as f:
        f.write("test_folder: {directory: test_dir}")
    magic_folder_subdir = os.path.join(
        os.path.join(client.nodedir, 'magic-folders', 'Test'))
    os.makedirs(magic_folder_subdir)
    with open(os.path.join(magic_folder_subdir, 'tahoe.cfg'), 'w') as f:
        f.write('[magic_folder]\nlocal.directory = /Test')
    client.nodeurl = 'http://127.0.0.1:65536/'
    return client


def test_is_valid_furl():
    assert is_valid_furl('pb://abc234@example.org:12345/introducer')


def test_is_valid_furl_no_port():
    assert not is_valid_furl('pb://abc234@example.org/introducer')


def test_is_valid_furl_no_host_separator():
    assert not is_valid_furl('pb://abc234example.org:12345/introducer')


def test_is_valid_furl_invalid_char_in_connection_hint():
    assert not is_valid_furl('pb://abc234@exam/ple.org:12345/introducer')


def test_is_valid_furl_tub_id_not_base32():
    assert not is_valid_furl('pb://abc123@example.org:12345/introducer')


def test_get_nodedirs(tahoe, tmpdir_factory):
    basedir = str(tmpdir_factory.getbasetemp())
    assert tahoe.nodedir in get_nodedirs(basedir)


def test_get_nodedirs_empty(tahoe, tmpdir_factory):
    basedir = os.path.join(str(tmpdir_factory.getbasetemp()), 'non-existent')
    assert get_nodedirs(basedir) == []


def test_raise_tahoe_error():
    with pytest.raises(TahoeError):
        raise TahoeError


def test_raise_tahoe_command_error():
    with pytest.raises(TahoeCommandError):
        raise TahoeCommandError


def test_raise_tahoe_web_error():
    with pytest.raises(TahoeWebError):
        raise TahoeWebError


def test_tahoe_default_nodedir():
    tahoe_client = Tahoe()
    assert tahoe_client.nodedir == os.path.join(
        os.path.expanduser('~'), '.tahoe')


def test_config_get(tahoe):
    assert tahoe.config_get('node', 'nickname') == 'default'


def test_config_set(tahoe):
    tahoe.config_set('node', 'nickname', 'test')
    assert tahoe.config_get('node', 'nickname') == 'test'


def test_get_settings(tahoe):
    settings = tahoe.get_settings()
    nickname = settings['nickname']
    icon_url = settings['icon_url']
    assert (nickname, icon_url) == (tahoe.name, 'test_url')


def test_export(tahoe, tmpdir_factory):
    dest = os.path.join(str(tmpdir_factory.getbasetemp()), 'settings.json')
    tahoe.export(dest)
    assert os.path.isfile(dest)


def test_get_aliases(tahoe):
    aliases = tahoe.get_aliases()
    assert aliases['test_alias:'] == 'test_cap'


def test_get_alias(tahoe):
    assert tahoe.get_alias('test_alias:') == 'test_cap'


def test_get_alias_append_colon(tahoe):
    assert tahoe.get_alias('test_alias') == 'test_cap'


def test_get_alias_not_found(tahoe):
    assert not tahoe.get_alias('missing_alias')


def test_load_magic_folders(tahoe):
    tahoe.load_magic_folders()
    assert tahoe.magic_folders['test_folder']['directory'] == 'test_dir'


def test_load_magic_folders_from_subdir(tahoe):
    tahoe.load_magic_folders()
    assert tahoe.magic_folders['Test']['directory'] == '/Test'


@pytest.inlineCallbacks
def test_tahoe_command_win32_monkeypatch(tahoe, monkeypatch):
    monkeypatch.setattr('sys.platform', 'win32')
    monkeypatch.setattr('sys.frozen', True, raising=False)
    monkeypatch.setattr('gridsync.tahoe.Tahoe._win32_popen',
                        lambda a, b, c, d: 'test output')
    output = yield tahoe.command(['test_command'])
    assert output == 'test output'


@pytest.inlineCallbacks
def test_tahoe_version(tahoe, monkeypatch):
    monkeypatch.setattr('gridsync.tahoe.Tahoe.command', lambda x, y: 'test 1')
    version = yield tahoe.version()
    assert version == ('tahoe_exe', '1')


@pytest.inlineCallbacks
def test_tahoe_create_client_nodedir_exists_error(tahoe):
    with pytest.raises(NodedirExistsError):
        yield tahoe.create_client()


@pytest.inlineCallbacks
def test_tahoe_create_client_args(tahoe, monkeypatch):
    monkeypatch.setattr('os.path.exists', lambda x: False)

    def return_args(_, args):
        returnValue(args)
    monkeypatch.setattr('gridsync.tahoe.Tahoe.command', return_args)
    args = yield tahoe.create_client(nickname='test_nickname')
    assert set(['--nickname', 'test_nickname']).issubset(set(args))


@pytest.inlineCallbacks
def test_tahoe_create_client_args_compat(tahoe, monkeypatch):
    monkeypatch.setattr('os.path.exists', lambda x: False)

    def return_args(_, args):
        returnValue(args)
    monkeypatch.setattr('gridsync.tahoe.Tahoe.command', return_args)
    args = yield tahoe.create_client(happy=7)
    assert set(['--shares-happy', '7']).issubset(set(args))


def test_tahoe_stop_win32_monkeypatch(tahoe, monkeypatch):
    pidfile = os.path.join(tahoe.nodedir, 'twistd.pid')
    with open(pidfile, 'w') as f:
        f.write('4194305')
    killed = [None]

    def fake_kill(pid, _):
        killed[0] = pid
    removed = [None]

    def fake_remove(file):
        removed[0] = file
    monkeypatch.setattr('os.kill', fake_kill)
    monkeypatch.setattr('os.remove', fake_remove)
    monkeypatch.setattr('gridsync.tahoe.get_nodedirs', lambda _: [])
    monkeypatch.setattr('sys.platform', 'win32')
    tahoe.stop()
    assert (killed[0], removed[0]) == (4194305, pidfile)


@pytest.inlineCallbacks
def test_tahoe_stop_linux_monkeypatch(tahoe, monkeypatch):
    def return_args(_, args):
        returnValue(args)
    monkeypatch.setattr('gridsync.tahoe.Tahoe.command', return_args)
    monkeypatch.setattr('sys.platform', 'linux')
    output = yield tahoe.stop()
    assert output == ['stop']


def test_parse_welcome_page(tahoe):  # tahoe-lafs=<1.12.1
    html = '''
        Connected to <span>3</span>of <span>10</span> known storage servers
        <td class="service-available-space">N/A</td>
        <td class="service-available-space">1kB</td>
        <td class="service-available-space">1kB</td>
    '''
    connected, known, space = tahoe._parse_welcome_page(html)
    assert (connected, known, space) == (3, 10, 2048)


@pytest.inlineCallbacks
def test_get_grid_status(tahoe, monkeypatch):
    json_content = b'''{
        "introducers": {
            "statuses": [
                "Connected to introducer.local:3456 via tcp"
            ]
        },
        "servers": [
            {
                "connection_status": "Trying to connect",
                "nodeid": "v0-aaaaaaaaaaaaaaaaaaaaaaaa",
                "last_received_data": null,
                "version": null,
                "available_space": null,
                "nickname": "node1"
            },
            {
                "connection_status": "Connected to tcp:node2:4567 via tcp",
                "nodeid": "v0-bbbbbbbbbbbbbbbbbbbbbbbb",
                "last_received_data": 1509126406.799392,
                "version": "tahoe-lafs/1.12.1",
                "available_space": 1024,
                "nickname": "node2"
            },
            {
                "connection_status": "Connected to tcp:node3:5678 via tcp",
                "nodeid": "v0-cccccccccccccccccccccccc",
                "last_received_data": 1509126406.799392,
                "version": "tahoe-lafs/1.12.1",
                "available_space": 2048,
                "nickname": "node3"
            }
        ]
    }'''
    monkeypatch.setattr('treq.get', fake_get)
    monkeypatch.setattr('treq.content', lambda _: json_content)
    num_connected, num_known, available_space = yield tahoe.get_grid_status()
    assert (num_connected, num_known, available_space) == (2, 3, 3072)


@pytest.inlineCallbacks
def test_get_connected_servers(tahoe, monkeypatch):
    html = b'Connected to <span>3</span>of <span>10</span>'
    monkeypatch.setattr('treq.get', fake_get)
    monkeypatch.setattr('treq.content', lambda _: html)
    output = yield tahoe.get_connected_servers()
    assert output == 3


@pytest.inlineCallbacks
def test_is_ready_false_not_shares_happy(tahoe, monkeypatch):
    output = yield tahoe.is_ready()
    assert output is False


@pytest.inlineCallbacks
def test_is_ready_false_not_connected_servers(tahoe, monkeypatch):
    tahoe.shares_happy = 7
    monkeypatch.setattr(
        'gridsync.tahoe.Tahoe.get_connected_servers', lambda _: None)
    output = yield tahoe.is_ready()
    assert output is False


@pytest.inlineCallbacks
def test_is_ready_true(tahoe, monkeypatch):
    tahoe.shares_happy = 7
    monkeypatch.setattr(
        'gridsync.tahoe.Tahoe.get_connected_servers', lambda _: 10)
    output = yield tahoe.is_ready()
    assert output is True


@pytest.inlineCallbacks
def test_is_ready_false_connected_less_than_happy(tahoe, monkeypatch):
    tahoe.shares_happy = 7
    monkeypatch.setattr(
        'gridsync.tahoe.Tahoe.get_connected_servers', lambda _: 3)
    output = yield tahoe.is_ready()
    assert output is False


@pytest.inlineCallbacks
def test_await_ready(tahoe, monkeypatch):
    monkeypatch.setattr('gridsync.tahoe.Tahoe.is_ready', lambda _: True)
    yield tahoe.await_ready()
    assert True


@pytest.inlineCallbacks
def test_tahoe_mkdir(tahoe, monkeypatch):
    monkeypatch.setattr('treq.post', fake_post)
    monkeypatch.setattr('treq.content', lambda _: b'URI:DIR2:abc234:def567')
    output = yield tahoe.mkdir()
    assert output == 'URI:DIR2:abc234:def567'


@pytest.inlineCallbacks
def test_tahoe_mkdir_fail_code_500(tahoe, monkeypatch):
    monkeypatch.setattr('treq.post', fake_post_code_500)
    monkeypatch.setattr('treq.content', lambda _: b'test content')
    with pytest.raises(TahoeWebError):
        yield tahoe.mkdir()


@pytest.inlineCallbacks
def test_create_rootcap(tahoe, monkeypatch):
    monkeypatch.setattr('gridsync.tahoe.Tahoe.mkdir', lambda _: 'URI:DIR2:abc')
    output = yield tahoe.create_rootcap()
    assert output == 'URI:DIR2:abc'


@pytest.inlineCallbacks
def test_create_rootcap_already_exists(tahoe, monkeypatch):
    with pytest.raises(OSError):
        yield tahoe.create_rootcap()


@pytest.inlineCallbacks
def test_tahoe_upload(tahoe, monkeypatch):
    monkeypatch.setattr('treq.put', fake_put)
    monkeypatch.setattr('treq.content', lambda _: b'test_cap')
    output = yield tahoe.upload(tahoe.rootcap_path)
    assert output == 'test_cap'


@pytest.inlineCallbacks
def test_tahoe_upload_fail_code_500(tahoe, monkeypatch):
    monkeypatch.setattr('treq.put', fake_put_code_500)
    monkeypatch.setattr('treq.content', lambda _: b'test content')
    with pytest.raises(TahoeWebError):
        yield tahoe.upload(tahoe.rootcap_path)


@pytest.inlineCallbacks
def test_tahoe_download(tahoe, monkeypatch):
    def fake_collect(response, collector):
        collector(b'test_content')  # f.write(b'test_content')
    monkeypatch.setattr('treq.get', fake_get)
    monkeypatch.setattr('treq.collect', fake_collect)
    location = os.path.join(tahoe.nodedir, 'test_downloaded_file')
    yield tahoe.download('test_cap', location)
    with open(location, 'r') as f:
        content = f.read()
        assert content == 'test_content'


@pytest.inlineCallbacks
def test_tahoe_download_fail_code_500(tahoe, monkeypatch):
    monkeypatch.setattr('treq.get', fake_get_code_500)
    monkeypatch.setattr('treq.content', lambda _: b'test content')
    with pytest.raises(TahoeWebError):
        yield tahoe.download('test_cap', os.path.join(tahoe.nodedir, 'nofile'))


@pytest.inlineCallbacks
def test_tahoe_link(tahoe, monkeypatch):
    monkeypatch.setattr('treq.post', fake_post)
    yield tahoe.link('test_dircap', 'test_childname', 'test_childcap')
    assert True


@pytest.inlineCallbacks
def test_tahoe_link_fail_code_500(tahoe, monkeypatch):
    monkeypatch.setattr('treq.post', fake_post_code_500)
    monkeypatch.setattr('treq.content', lambda _: b'test content')
    with pytest.raises(TahoeWebError):
        yield tahoe.link('test_dircap', 'test_childname', 'test_childcap')


@pytest.inlineCallbacks
def test_tahoe_unlink(tahoe, monkeypatch):
    monkeypatch.setattr('treq.post', fake_post)
    yield tahoe.unlink('test_dircap', 'test_childname')
    assert True


@pytest.inlineCallbacks
def test_tahoe_unlink_fail_code_500(tahoe, monkeypatch):
    monkeypatch.setattr('treq.post', fake_post_code_500)
    monkeypatch.setattr('treq.content', lambda _: b'test content')
    with pytest.raises(TahoeWebError):
        yield tahoe.unlink('test_dircap', 'test_childname')


def test_tahoe_get_magic_folder_client(tahoe):
    tahoe.magic_folders['Test Documents']['client'] = 'test_object'
    assert tahoe.get_magic_folder_client('Test Documents') == 'test_object'


def test_tahoe_get_magic_folder_client_none(tahoe):
    assert tahoe.get_magic_folder_client('Non-existent Folder') is None


@pytest.inlineCallbacks
def test_tahoe_magic_folder_invite(tahoe, monkeypatch):
    monkeypatch.setattr('gridsync.tahoe.Tahoe.command', lambda x, y: 'code123')
    output = yield tahoe.magic_folder_invite('Test Folder', 'Bob')
    assert output == 'code123'


@pytest.inlineCallbacks
def test_tahoe_magic_folder_invite_from_subclient(tahoe, monkeypatch):
    subclient = MagicMock()
    subclient.command = lambda _: 'code123'
    tahoe.magic_folders['TestInviteFolder'] = {'client': subclient}
    output = yield tahoe.magic_folder_invite('TestInviteFolder', 'Bob')
    assert output == 'code123'


@pytest.inlineCallbacks
def test_tahoe_magic_folder_uninvite(tahoe, monkeypatch):
    monkeypatch.setattr('gridsync.tahoe.Tahoe.unlink', lambda x, y, z: None)
    monkeypatch.setattr('gridsync.tahoe.Tahoe.get_alias', lambda x, y: 'test')
    yield tahoe.magic_folder_uninvite('Test Folder', 'Bob')
    assert True


@pytest.inlineCallbacks
def test_tahoe_magic_folder_uninvite_from_subclient(tahoe, monkeypatch):
    tahoe.magic_folders['TestUninviteFolder'] = {'client': MagicMock()}
    monkeypatch.setattr('gridsync.tahoe.Tahoe.unlink', lambda x, y, z: None)
    monkeypatch.setattr('gridsync.tahoe.Tahoe.get_alias', lambda x, y: 'test')
    yield tahoe.magic_folder_uninvite('TestUninviteFolder', 'Bob')
    assert True
