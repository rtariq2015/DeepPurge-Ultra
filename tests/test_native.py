"""Opt-in integration checks; all destructive work is confined to fresh fixtures."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('DEEPPURGE_NATIVE_TESTS') == '1', 'native fixture tests are opt-in')
class NativeTests(unittest.TestCase):
    def isolated(self, code):
        with tempfile.TemporaryDirectory(prefix='deeppurge-native-') as directory:
            home = Path(directory) / 'home'; home.mkdir()
            environment = {**os.environ, 'HOME': str(home), 'PYTHONPATH': str(
                Path(__file__).resolve().parents[1] / 'src'),
                'XDG_DATA_HOME': str(home / '.local/share'),
                'XDG_CONFIG_HOME': str(home / '.config'),
                'XDG_CACHE_HOME': str(home / '.cache'),
                'XDG_STATE_HOME': str(home / '.local/state'),
                'FLATPAK_USER_DIR': str(home / '.local/share/flatpak')}
            process = subprocess.run([sys.executable, '-c', code], env=environment,
                                     capture_output=True, text=True, timeout=120)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            return process.stdout

    def test_real_appimage_and_local_shortcut_trash(self):
        self.isolated(r'''
from pathlib import Path
from deeppurge.backends import AppImageBackend, LocalBackend, appimage_verified
from deeppurge.cleanup import fingerprint, clean_selected, leftover_candidates
from deeppurge.models import Application
home=Path.home()
image=home/'Disposable.AppImage'
image.write_bytes(b'\x7fELF\0\0\0\0AI\x02test-fixture')
image.chmod(0o700)
launchers=home/'.local/share/applications'
launchers.mkdir(parents=True)
launcher=launchers/'disposable.desktop'
launcher.write_text('[Desktop Entry]\nType=Application\nName=Disposable\nExec='+str(image)+'\n')
portable=Path(str(image)+'.config'); portable.mkdir()
(portable/'preferences').write_text('preserve until explicitly selected')
app=Application('fixture','appimage','Disposable fixture',image.name,'AppImage',
    executable=image,identity=appimage_verified(image),launchers=(launcher,))
backend=AppImageBackend()
preview=backend.prepare(app)
assert preview.allowed
result=backend.uninstall(preview,lambda *_:None)
assert result.status=='success',result
assert not image.exists() and not launcher.exists() and portable.exists()
items,_=leftover_candidates(app)
assert len(items)==1
assert clean_selected(items).status=='success'
assert not portable.exists()
shortcut=launchers/'shortcut.desktop'
shortcut.write_text('[Desktop Entry]\nType=Application\nName=Shortcut\nExec=/bin/true\n')
local=Application('shortcut','local','Shortcut fixture','shortcut','Custom/Local',
    launchers=(shortcut,),identity=fingerprint(shortcut))
adapter=LocalBackend()
result=adapter.uninstall(adapter.prepare(local),lambda *_:None)
assert result.status=='success' and not shortcut.exists(),result
assert len(list((home/'.local/share/Trash/files').iterdir()))>=4
print('Real recoverable Trash removal passed in isolated HOME/XDG directories.')
''')

    def test_isolated_flatpak_install_preview_remove_and_cleanup(self):
        self.isolated(r'''
import os
from pathlib import Path
import subprocess
from deeppurge.backends import FlatpakBackend
from deeppurge.cleanup import clean_selected,leftover_candidates
home=Path.home()
build=home/'fixture-build'
(build/'files/bin').mkdir(parents=True)
(build/'files/bin/fixture').write_text('#!/bin/sh\nexit 0\n')
(build/'files/bin/fixture').chmod(0o700)
identifier='org.deeppurge.DisposableFixture'
(build/'metadata').write_text('[Application]\nname='+identifier+'\nruntime=org.freedesktop.Platform/x86_64/24.08\ncommand=fixture\n')
repo=home/'fixture-repo'
def run(*args):
    result=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=40)
    assert result.returncode==0, result.stdout+result.stderr
run('flatpak','build-finish',str(build))
run('flatpak','build-export','--arch=x86_64',str(repo),str(build),'stable')
run('flatpak','--user','remote-add','--no-gpg-verify','fixture',repo.as_uri())
run('flatpak','--user','install','--assumeyes','--noninteractive','--no-deps','--no-related','fixture',identifier)
location=subprocess.check_output(['flatpak','--user','info','--show-location',identifier],text=True).strip()
assert Path(location).is_relative_to(home),'Refusing removal outside isolated fixture HOME'
backend=FlatpakBackend()
app=next(app for app in backend.discover() if app.scope=='user' and app.identifier==identifier)
data=home/'.var/app'/identifier
data.mkdir(parents=True)
(data/'keep').write_text('user data survives normal removal')
preview=backend.prepare(app)
assert preview.allowed and app.arch=='x86_64' and app.branch=='stable'
result=backend.uninstall(preview,lambda *_:None)
assert result.status=='success' and result.removed,result
assert not backend.exists(app) and data.exists()
items,_=leftover_candidates(app)
assert len(items)==1
cleanup=clean_selected(items)
assert cleanup.status=='success' and not data.exists(),cleanup
print('Isolated Flatpak installation, exact-reference uninstall, verification and separate Trash cleanup passed.')
''')

    def test_packagekit_readonly_simulation(self):
        from deeppurge.backends import DebianBackend
        backend = DebianBackend()
        self.assertTrue(backend.capability().can_remove)
        app = next((app for app in backend.discover() if app.identifier == 'baobab'), None)
        if app is None:
            self.skipTest('No baobab package installed for a read-only simulation')
        preview = backend.prepare(app)
        self.assertTrue(preview.allowed)
        self.assertEqual(len(preview.affected), 1)
        self.assertTrue(backend.exists(app))
