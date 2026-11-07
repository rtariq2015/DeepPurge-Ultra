#!/usr/bin/python3
"""Launch DeepPurge Ultra from its source checkout."""
import os
from pathlib import Path
import sys

# Snap-packaged editors export their private GTK/GIO modules to child processes.
# A system Python must use system modules rather than those incompatible loaders.
snap_root = os.environ.get('SNAP', '')
snap_name = os.environ.get('SNAP_NAME', '')
if snap_root and snap_name and not Path(sys.executable).is_relative_to(snap_root):
    for variable in ('GTK_PATH', 'GTK_EXE_PREFIX', 'GIO_MODULE_DIR',
                     'GDK_PIXBUF_MODULE_FILE', 'GDK_PIXBUF_MODULEDIR', 'GSETTINGS_SCHEMA_DIR'):
        if f'/snap/{snap_name}/' in os.environ.get(variable, ''):
            os.environ.pop(variable, None)
    if 'XDG_DATA_DIRS' in os.environ:
        directories = [value for value in os.environ['XDG_DATA_DIRS'].split(':')
                       if f'/snap/{snap_name}/' not in value]
        if directories:
            os.environ['XDG_DATA_DIRS'] = ':'.join(directories)
        else:
            os.environ.pop('XDG_DATA_DIRS', None)
try:
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('Gdk', '3.0')
    from deeppurge.app import main
except (ImportError, ValueError) as error:
    print(f'Unable to load GTK3: {error}\nInstall python3-gi python3-gi-cairo gir1.2-gtk-3.0 and use /usr/bin/python3.', file=sys.stderr)
    raise SystemExit(1) from error
if __name__ == '__main__':
    raise SystemExit(main())
