#!/usr/bin/env python3
"""Compile actual upstream callbacks against GTK signals, without a display.

Only transport/state publication is stubbed; tested callback bodies are taken
verbatim from the supplied (patched or unpatched) source tree.
"""
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile

source = pathlib.Path(sys.argv[1]) / 'src'
code = (source / 'gtkimcontextwayland.c').read_text()
def function(name):
    match = re.search(r'static (?:void|gboolean)\n' + name + r'\s*\(', code)
    assert match, name
    start = code.index('{', match.start())
    depth = 1
    end = start + 1
    while depth:
        depth += (code[end] == '{') - (code[end] == '}')
        end += 1
    return code[match.start():end] + '\n'

private = code[code.index('struct _GtkIMContextWaylandPrivate'):code.index('static struct zwp_text_input_manager_v1')]
harness = '''#include "gtkimcontextwayland.h"
#include <stdint.h>
#include <string.h>
''' + private + '''
G_DEFINE_TYPE(GtkIMContextWayland, gtk_im_context_wayland, GTK_TYPE_IM_CONTEXT)
static void gtk_im_context_wayland_init(GtkIMContextWayland *self) {
 self->priv = g_new0(GtkIMContextWaylandPrivate, 1);
}
static void gtk_im_context_wayland_class_init(GtkIMContextWaylandClass *klass) {}
static void update_text_input_state(GtkIMContextWayland *self) {}
''' + ''.join(function(n) for n in ('reset_preedit', 'check_serial', 'text_input_preedit_string', 'text_input_commit_string', 'commit_and_reset_preedit')) + r'''
static GString *events;
static char *committed;
static void event(GtkIMContext *ctx, const char *tag) { g_string_append(events, tag); }
static void commit(GtkIMContext *ctx, const char *text, void *data) {
 g_string_append(events,"K"); g_free(committed); committed=g_strdup(text);
}
static void expect(const char *s) { g_assert_cmpstr(events->str,==,s); g_string_truncate(events,0); }
int main(void) {
 GtkIMContextWayland *ctx=g_object_new(GTK_TYPE_IM_CONTEXT_WAYLAND,NULL);
 events=g_string_new("");
 g_signal_connect(ctx,"preedit-start",G_CALLBACK(event),"S");
 g_signal_connect(ctx,"preedit-changed",G_CALLBACK(event),"C");
 g_signal_connect(ctx,"preedit-end",G_CALLBACK(event),"E");
 g_signal_connect(ctx,"commit",G_CALLBACK(commit),NULL);
 text_input_preedit_string(ctx,NULL,0,"a","a"); expect("SC");
 text_input_preedit_string(ctx,NULL,0,"ab","ab"); expect("C");
 text_input_preedit_string(ctx,NULL,0,"a","a"); expect("C");
 text_input_preedit_string(ctx,NULL,0,"",""); expect("CE");
 text_input_preedit_string(ctx,NULL,0,"",""); expect("C");
 text_input_preedit_string(ctx,NULL,0,"тест","тест"); expect("SC");
 text_input_commit_string(ctx,NULL,0,"тест"); expect("CEK");
 g_assert_cmpstr(committed,==,"тест");
 text_input_preedit_string(ctx,NULL,0,"x","x"); expect("SC");
 commit_and_reset_preedit(ctx); expect("CEK");
 g_assert_cmpstr(committed,==,"x");
 commit_and_reset_preedit(ctx); expect("");
 ctx->priv->serial=10; ctx->priv->reset_serial=9;
 text_input_preedit_string(ctx,NULL,8,"stale","stale"); expect("");
 g_print("PASS: preedit typing, backspace, empty transition, UTF-8 commit, focus-out and stale serial\n");
 return 0;
}
'''
flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'gtk+-3.0'], text=True))
with tempfile.TemporaryDirectory(prefix='guide-preedit-test-') as tmp:
    test = pathlib.Path(tmp) / 'test.c'
    binary = pathlib.Path(tmp) / 'test'
    test.write_text(harness)
    subprocess.run(['cc', '-Werror=implicit-function-declaration', '-I' + str(source), str(test), '-o', str(binary), *flags], check=True)
    subprocess.run([str(binary)], check=True)
