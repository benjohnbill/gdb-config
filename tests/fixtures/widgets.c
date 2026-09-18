/* Fixture mirroring challenges/01_use_after_free: the vtable widget system. */
#include <stdlib.h>
#include <string.h>

typedef struct Widget Widget;

typedef struct {
    void (*render)(Widget *self);
    void (*on_event)(Widget *self, int code);
} VTable;

struct Widget {
    const VTable *vtbl;
    int id;
    int closed;
    char label[24];
};

#define MAX_WIDGETS 8
typedef struct {
    Widget *items[MAX_WIDGETS];
    int count;
} Screen;

static void button_render(Widget *self) { (void)self; }
static void label_render(Widget *self)  { (void)self; }
static void dialog_render(Widget *self) { (void)self; }
static void noop_event(Widget *self, int code) { (void)self; (void)code; }

static const VTable BUTTON_VT = { button_render, noop_event };
static const VTable LABEL_VT  = { label_render,  noop_event };
static const VTable DIALOG_VT = { dialog_render, noop_event };

static Widget *widget_new(const VTable *vt, int id, const char *label) {
    Widget *w = malloc(sizeof *w);
    w->vtbl = vt;
    w->id = id;
    w->closed = 0;
    strncpy(w->label, label, sizeof(w->label) - 1);
    w->label[sizeof(w->label) - 1] = '\0';
    return w;
}

Screen s;

static void ready(void) { }     /* the tests break here */

int main(void) {
    s.count = 0;
    s.items[s.count++] = widget_new(&LABEL_VT,  10, "Welcome");
    s.items[s.count++] = widget_new(&DIALOG_VT, 12, "Are you sure?");
    s.items[s.count++] = widget_new(&BUTTON_VT, 11, "OK");
    s.items[s.count++] = widget_new(&BUTTON_VT, 13, "Cancel");
    s.items[1] = NULL;          /* the closed dialog, slot cleared */
    ready();
    return s.count - 4;
}
