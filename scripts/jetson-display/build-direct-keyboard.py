#!/usr/bin/env python3
"""Build only Weston 13's keyboard client, not the compositor. No installation."""
import pathlib
import shlex
import subprocess
import sys

src = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2]).resolve()
out.mkdir(parents=True, exist_ok=True)
protocols = pathlib.Path(subprocess.check_output(['pkg-config', '--variable=pkgdatadir', 'wayland-protocols'], text=True).strip())
xmls = [
 protocols/'stable/xdg-shell/xdg-shell.xml',
 src/'protocol/text-cursor-position.xml',
 protocols/'unstable/relative-pointer/relative-pointer-unstable-v1.xml',
 protocols/'unstable/pointer-constraints/pointer-constraints-unstable-v1.xml',
 protocols/'unstable/tablet/tablet-unstable-v2.xml',
 src/'protocol/ivi-application.xml',
 protocols/'stable/viewporter/viewporter.xml',
 protocols/'unstable/text-input/text-input-unstable-v1.xml',
 protocols/'unstable/input-method/input-method-unstable-v1.xml',
]
generated = []
for xml in xmls:
    name = xml.stem
    c = out/(name+'-protocol.c')
    subprocess.run(['wayland-scanner','private-code',str(xml),str(c)],check=True)
    subprocess.run(['wayland-scanner','client-header',str(xml),str(out/(name+'-client-protocol.h'))],check=True)
    generated.append(str(c))
(out/'config.h').write_text('''#define _GNU_SOURCE 1
#define DATADIR "/usr/share"
#define HAVE_MKOSTEMP 1
#define HAVE_MEMFD_CREATE 1
#define HAVE_POSIX_FALLOCATE 1
#define HAVE_STRCHRNUL 1
#define HAVE_XKBCOMMON_COMPOSE 1
''')
sources = ['clients/keyboard.c','clients/window.c','shared/config-parser.c',
 'shared/option-parser.c','shared/file-util.c','shared/os-compatibility.c',
 'shared/image-loader.c','shared/cairo-util.c','shared/frame.c','shared/matrix.c']
flags=shlex.split(subprocess.check_output(['pkg-config','--cflags','--libs',
 'wayland-client','wayland-cursor','wayland-server','xkbcommon','cairo','libpng','pixman-1'],text=True))
subprocess.run(['cc','-O2','-I'+str(out),'-I'+str(src),'-I'+str(src/'include'),
 *[str(src/s) for s in sources],*generated,'-o',str(out/'weston-keyboard'),*flags,'-lm','-lutil'],check=True)
print(out/'weston-keyboard')

# Test the real key handler before allowing installation.
test = pathlib.Path(__file__).resolve().parent/'tests/direct-keyboard.c'
subprocess.run(['cc','-O2','-ffunction-sections','-fdata-sections','-Wl,--gc-sections',
 '-I'+str(out),'-I'+str(src),'-I'+str(src/'include'),'-I'+str(src/'clients'),
 str(test),'-o',str(out/'test-direct-keyboard'),*flags,'-lm'],check=True)
subprocess.run([str(out/'test-direct-keyboard')],check=True)
