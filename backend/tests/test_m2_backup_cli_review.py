"""The CLI rejects unsafe key placement before it initializes any database."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from cryptography.fernet import Fernet
import pytest
from aiopsstudio.backup import BackupError


def cli():
    path=Path(__file__).resolve().parents[2]/'scripts/backup_workspace.py'
    spec=importlib.util.spec_from_file_location('synthetic_backup_cli',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('alias',[False,True])
def test_key_inside_artifact_root_is_rejected_before_store(tmp_path,monkeypatch,alias):
    module=cli();assets=tmp_path/'assets';assets.mkdir()
    key=assets/'master.key';key.write_bytes(Fernet.generate_key());key.chmod(0o600)
    supplied=tmp_path/'key-alias' if alias else key
    if alias:supplied.symlink_to(key)
    monkeypatch.setattr(module,'Store',lambda *a,**kw:pytest.fail('unsafe backup initialized Store'))
    monkeypatch.setattr(sys,'argv',['backup_workspace.py','backup','--master-key-file',str(supplied),
        '--artifact-dir',str(assets),'--backup-dir',str(tmp_path/'backup')])
    with pytest.raises(BackupError) as error:module.main()
    assert error.value.code=='master_key_in_artifacts'
    assert not(tmp_path/'backup').exists()


def test_external_key_path_is_supplied_to_api_without_printing_key(tmp_path,monkeypatch,capsys):
    module=cli();assets=tmp_path/'assets';assets.mkdir()
    key=tmp_path/'external.key';key.write_bytes(Fernet.generate_key());key.chmod(0o600)
    store=SimpleNamespace(close=lambda:None);received={}
    monkeypatch.setattr(module,'Store',lambda value:store)
    def capture(*a,**kw):
        received.update(kw)
        return {'files':[],'tables':[],'total_bytes':0}
    monkeypatch.setattr(module,'create_backup',capture)
    monkeypatch.setenv('SYNTHETIC_BACKUP_DATABASE','unused-synthetic-connection')
    monkeypatch.setattr(sys,'argv',['backup_workspace.py','backup','--master-key-file',str(key),
        '--artifact-dir',str(assets),'--backup-dir',str(tmp_path/'backup'),'--database-env','SYNTHETIC_BACKUP_DATABASE'])
    module.main()
    assert received['master_key_path']==str(key)
    assert key.read_text() not in capsys.readouterr().out
