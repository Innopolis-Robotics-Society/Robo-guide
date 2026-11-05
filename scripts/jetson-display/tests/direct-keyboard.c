#include "config.h"
/* Include the real keyboard implementation; replace only outbound protocol
 * calls. No compositor, input devices or user text are used by this test. */
#include <assert.h>
#include <string.h>
#include "window.h"
#include "input-method-unstable-v1-client-protocol.h"
#include "text-input-unstable-v1-client-protocol.h"
static char committed[128];
static unsigned commits, preedits, deletes, presses, releases;
static void record_commit(const char *text) { strcat(committed, text); commits++; }
static void record_key(unsigned sym, unsigned state) {
 assert(sym == XKB_KEY_BackSpace);
 if (state) presses++; else releases++;
}
#define zwp_input_method_context_v1_commit_string(ctx,serial,text) record_commit(text)
#define zwp_input_method_context_v1_cursor_position(ctx,index,anchor) ((void)0)
#define zwp_input_method_context_v1_preedit_string(ctx,serial,text,commit) (preedits++)
#define zwp_input_method_context_v1_preedit_cursor(ctx,index) ((void)0)
#define zwp_input_method_context_v1_preedit_styling(ctx,index,length,style) ((void)0)
#define zwp_input_method_context_v1_delete_surrounding_text(ctx,index,length) (deletes++)
#define zwp_input_method_context_v1_keysym(ctx,serial,time,sym,state,mod) record_key(sym,state)
#define display_get_serial(display) 1
#define main upstream_main
#include "keyboard.c"
#undef main
int main(void) {
 struct virtual_keyboard vk = {0};
 struct keyboard k = {0};
 vk.keyboard=&k; k.keyboard=&vk; vk.preedit_string=strdup("");
 struct key letter={keytype_default,"a","A","1",1};
 keyboard_handle_key(&k,0,&letter,NULL,WL_POINTER_BUTTON_STATE_PRESSED);
 keyboard_handle_key(&k,0,&letter,NULL,WL_POINTER_BUTTON_STATE_RELEASED);
 assert(commits==1 && !strcmp(committed,"a") && !*vk.preedit_string && preedits==0);
 k.state=KEYBOARD_STATE_UPPERCASE;
 keyboard_handle_key(&k,0,&letter,NULL,WL_POINTER_BUTTON_STATE_PRESSED);
 assert(commits==2 && !strcmp(committed,"aA") && !*vk.preedit_string);
 /* Switching fields must not leave anything to commit into the next field. */
 virtual_keyboard_commit_preedit(&vk); assert(commits==2);
 struct key back={keytype_backspace,"","","",1};
 keyboard_handle_key(&k,0,&back,NULL,WL_POINTER_BUTTON_STATE_PRESSED);
 keyboard_handle_key(&k,0,&back,NULL,WL_POINTER_BUTTON_STATE_RELEASED);
 assert(presses==1 && releases==1 && deletes==0);
 puts("PASS: direct commit, upper case, no pending text across focus, Backspace key press/release");
 return 0;
}
