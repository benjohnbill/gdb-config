/* Fixtures for the chase engine: chain, branch, cycle, pointerless struct. */
#include <stdlib.h>

typedef struct Node  { int item; struct Node *next; } Node;
typedef struct DNode { int v; struct DNode *next; struct DNode *prev; } DNode;
typedef struct TNode { int v; struct TNode *left; struct TNode *right; } TNode;
typedef struct Plain { int a; char name[16]; } Plain;

Node *head;      /* 5 nodes, NULL terminated */
Node *loop;      /* 3 nodes, the last points back at index 1 */
Node *shrink;    /* 5 nodes, cut to 2 before the third stop */
DNode *dhead;    /* 2 nodes, next and prev both present */
TNode *root;     /* a small binary tree */
Plain plain;     /* no pointer members at all */
int scalar = 7;

static Node *node_new(int item, Node *next) {
    Node *n = malloc(sizeof *n);
    n->item = item;
    n->next = next;
    return n;
}

static TNode *tnode(int v, TNode *l, TNode *r) {
    TNode *t = malloc(sizeof *t);
    t->v = v; t->left = l; t->right = r;
    return t;
}

static void build(void) {
    head = node_new(1, node_new(2, node_new(3, node_new(4, node_new(5, NULL)))));

    shrink = node_new(1, node_new(2, node_new(3, node_new(4, node_new(5, NULL)))));

    loop = node_new(10, node_new(20, node_new(30, NULL)));
    loop->next->next->next = loop->next;        /* [2] points back at [1] */

    dhead = malloc(sizeof *dhead);
    dhead->next = malloc(sizeof *dhead);
    dhead->v = 100;
    dhead->prev = NULL;
    dhead->next->v = 200;
    dhead->next->next = NULL;
    dhead->next->prev = dhead;

    root = tnode(1, tnode(2, tnode(4, NULL, NULL), NULL), tnode(3, NULL, NULL));

    plain.a = 5;
    plain.name[0] = 'h'; plain.name[1] = 'i'; plain.name[2] = '\0';
}

static void ready(void) { }     /* the tests break here */

int main(void) {
    build();
    ready();
    head->item = 99;            /* the second stop must show 1 -> 99 */
    head->next->item = 88;
    ready();
    shrink->next->next = NULL;  /* the third stop loses shrink[2..4] */
    root->left = NULL;          /* and root.left.left with it */
    ready();
    return scalar - 7;
}
