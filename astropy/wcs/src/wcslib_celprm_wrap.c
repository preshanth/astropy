#define NO_IMPORT_ARRAY

#include <stdlib.h> // calloc, malloc, free
#include <string.h> // memcpy

#include "astropy_wcs/wcslib_celprm_wrap.h"
#include "astropy_wcs/wcslib_prjprm_wrap.h"

#include <wcs.h>
#include <wcsprintf.h>
#include <cel.h>
#include <prj.h>
#include <wcserr.h>
#include <numpy/npy_math.h>

#include <stdio.h>

/*
 It gets to be really tedious to type long docstrings in ANSI C syntax
 (since multi-line strings literals are not valid).  Therefore, the
 docstrings are written in doc/docstrings.py, which are then converted
 by setup.py into docstrings.h, which we include here.
*/
#include "astropy_wcs/docstrings.h"
#include "astropy_wcs/wcslib_wrap.h"


PyObject** cel_errexc[7];


static int wcslib_cel_to_python_exc(int status)
{
    if (status > 0 && status < 7) {
        PyErr_SetString(*cel_errexc[status], cel_errmsg[status]);
    } else if (status > 6) {
        PyErr_SetString(
            PyExc_RuntimeError,
            "Unknown WCSLIB celprm-related error occurred.");
    }
    return status;
}


static int is_readonly(PyCelprm* self)
{
    if (self != NULL && self->owner != NULL) {
        PyErr_SetString(
                PyExc_AttributeError,
                "Attribute 'cel' of 'astropy.wcs.Wcsprm' objects is read-only.");
        return 1;
    } else {
        return 0;
    }
}


static int is_cel_null(PyCelprm* self)
{
    if (self->x == NULL) {
        PyErr_SetString(
                PyExc_MemoryError,
                "Underlying 'celprm' object is NULL.");
        return 1;
    } else {
        return 0;
    }
}


/***************************************************************************
 * PyCelprm methods                                                        *
 ***************************************************************************/

static PyObject* PyCelprm_new(PyTypeObject* type, PyObject* args, PyObject* kwds)
{
    PyCelprm* self;
    allocfunc alloc_func = PyType_GetSlot(type, Py_tp_alloc);
    self = (PyCelprm*)alloc_func(type, 0);
    if (self == NULL) return NULL;
    self->owner = NULL;
    self->prefcount = NULL;

    if ((self->x = calloc(1, sizeof(struct celprm))) == 0x0) {
        PyErr_SetString(PyExc_MemoryError,
        "Could not allocate memory for celprm structure.");
        return NULL;
    }
    if ((self->prefcount = (int*) malloc(sizeof(int))) == 0x0) {
        PyErr_SetString(PyExc_MemoryError, "Could not allocate memory.");
        free(self->x);
        return NULL;
    }

    if (wcslib_cel_to_python_exc(celini(self->x))) {
        free(self->x);
        free(self->prefcount);
        return NULL;
    }
    *(self->prefcount) = 1;
    return (PyObject*)self;
}


static int PyCelprm_traverse(PyCelprm* self, visitproc visit, void *arg)
{
    Py_VISIT(self->owner);
    Py_VISIT((PyObject*)Py_TYPE((PyObject*)self));
    return 0;
}


static int PyCelprm_clear(PyCelprm* self)
{
    Py_CLEAR(self->owner);
    return 0;
}


static void PyCelprm_dealloc(PyCelprm* self)
{
    PyCelprm_clear(self);
    wcslib_cel_to_python_exc(celfree(self->x)); // free memory used for err msg
    if (self->prefcount && (--(*self->prefcount)) == 0) {
        free(self->x);
        free(self->prefcount);
    }
    PyTypeObject *tp = Py_TYPE((PyObject*)self);
    freefunc free_func = PyType_GetSlot(tp, Py_tp_free);
    free_func((PyObject*)self);
    Py_DECREF(tp);
}


static int PyCelprm_cset(PyCelprm* self)
{
    if (wcslib_cel_to_python_exc(celset(self->x))) {
        return -1;
    }
    return 0;
}


static PyObject* PyCelprm_set(PyCelprm* self)
{
    if (is_readonly(self) || PyCelprm_cset(self)) return NULL;
    Py_RETURN_NONE;
}


PyCelprm* PyCelprm_cnew(PyObject* wcsprm_obj, struct celprm* x, int* prefcount)
{
    PyCelprm* self;
    PyTypeObject* type = (PyTypeObject*)PyCelprmType;
    allocfunc alloc_func = PyType_GetSlot(type, Py_tp_alloc);
    self = (PyCelprm*)alloc_func(type, 0);
    if (self == NULL) return NULL;
    self->x = x;
    Py_XINCREF(wcsprm_obj);
    self->owner = wcsprm_obj;
    self->prefcount = prefcount;
    if (prefcount) (*prefcount)++;
    return self;
}


static PyObject* PyCelprm_copy(PyCelprm* self)
{
    PyCelprm* copy = NULL;
    copy = PyCelprm_cnew(self->owner, self->x, self->prefcount);
    if (copy == NULL) return NULL;
    return (PyObject*)copy;
}


static PyObject* PyCelprm_deepcopy(PyCelprm* self)
{
    PyCelprm* copy = (PyCelprm*) PyCelprm_new((PyTypeObject*)PyCelprmType, NULL, NULL);
    if (copy == NULL) return NULL;

    memcpy(copy->x, self->x, sizeof(struct celprm));
    copy->x->err = NULL;
    return (PyObject*)copy;
}


static PyObject* PyCelprm___str__(PyCelprm* self) {
    /* if (PyCelprm_cset(self)) return NULL; */
    /* This is not thread-safe, but since we're holding onto the GIL,
       we can assume we won't have thread conflicts */
    wcsprintf_set(NULL);
    if (wcslib_cel_to_python_exc(celprt(self->x))) {
        return NULL;
    }
    return PyUnicode_FromString(wcsprintf_buf());
}


/***************************************************************************
 * Member getters/setters (properties)
 */


static PyObject* PyCelprm_get_flag(PyCelprm* self, void* closure)
{
    if (is_cel_null(self)) {
        return NULL;
    } else {
        return get_int("flag", self->x->flag);
    }
}

static PyObject* PyCelprm_get_offset(PyCelprm* self, void* closure)
{
    if (is_cel_null(self)) {
        return NULL;
    } else {
        return get_bool("offset", self->x->offset);
    }
}


static int PyCelprm_set_offset(PyCelprm* self, PyObject* value, void* closure)
{
    if (is_cel_null(self) || is_readonly(self)) {
        return -1;
    } else if (value == Py_None) {
        self->x->offset = 0;
        return 0;
    } else {
        return set_bool("offset", value, &self->x->offset);
    }
}


static PyObject* PyCelprm_get_phi0(PyCelprm* self, void* closure)
{
    if (is_cel_null(self)) {
        return NULL;
    } else if (self->x->phi0 != UNDEFINED) {
        return get_double("phi0", self->x->phi0);
    }
    Py_RETURN_NONE;
}


static int PyCelprm_set_phi0(PyCelprm* self, PyObject* value, void* closure)
{
    int result;
    double phi0;

    if (is_cel_null(self) || is_readonly(self)) {
        return -1;
    } else if (value == Py_None) {
        if (self->x->phi0 != UNDEFINED) {
            self->x->phi0 = UNDEFINED;
            self->x->flag = 0;
        }
    } else {
        result = set_double("phi0", value, &phi0);
        if (result) return result;
        if (phi0 != self->x->phi0) {
            self->x->phi0 = phi0;
            self->x->flag = 0;
        }
    }
    return 0;
}


static PyObject* PyCelprm_get_theta0(PyCelprm* self, void* closure)
{
    if (is_cel_null(self)) {
        return NULL;
    } else if (self->x->theta0 != UNDEFINED) {
        return get_double("theta0", self->x->theta0);
    }
    Py_RETURN_NONE;
}


static int PyCelprm_set_theta0(PyCelprm* self, PyObject* value, void* closure)
{
    int result;
    double theta0;
    if(is_cel_null(self) || is_readonly(self)) {
        return -1;
    } else if (value == Py_None) {
        if (self->x->theta0 != UNDEFINED) {
            self->x->theta0 = UNDEFINED;
            self->x->flag = 0;
        }
    } else {
        result = set_double("theta0", value, &theta0);
        if (result) return result;
        if (theta0 != self->x->theta0) {
            self->x->theta0 = theta0;
            self->x->flag = 0;
        }
    }
    return 0;
}


static PyObject* PyCelprm_get_ref(PyCelprm* self, void* closure)
{
    Py_ssize_t size = 4;
    if (is_cel_null(self)) {
        return NULL;
    } else {
        return get_double_array("ref", self->x->ref, 1, &size, (PyObject*) self);
    }
}


static int PyCelprm_set_ref(PyCelprm* self, PyObject* value, void* closure)
{
    int i;
    int skip[4] = {0, 0, 0, 0};
    double ref[4] = {0.0, 0.0, UNDEFINED, +90.0};
    npy_intp size;
    double *data;

    if (is_cel_null(self) || is_readonly(self)) return -1;

    if (value == Py_None) {
        /* If ref is set to None - reset ref to celini values: */
        for (i = 0; i < 4; i++) {
            self->x->ref[i] = ref[i];
        }
        self->x->flag = 0;
        return 0;
    }

    PyArrayObject* value_array = (PyArrayObject*) PyArray_ContiguousFromAny(value, NPY_DOUBLE, 1, 1);
    if (!value_array) return -1;

    size = PyArray_SIZE(value_array);

    if (size < 1) {
        Py_DECREF(value_array);
        PyErr_SetString(PyExc_ValueError,
            "'ref' must be a non-empty 1-dimentional list of values or None.");
        return -1;
    }

    if (size > 4) {
        Py_DECREF(value_array);
        PyErr_SetString(PyExc_RuntimeError, "Number of 'ref' values cannot exceed 4.");
        return -1;
    }

    if (PyList_Check(value)) {
        for (i = 0; i < size; i++) {
            skip[i] = (PyList_GetItem(value, i) == Py_None);
        }
    }

    data = (double*) PyArray_DATA(value_array);

    for (i = 0; i < size; i++) {
        if (skip[i]) continue;
        if (npy_isnan(self->x->ref[i])) {
            self->x->ref[i] = UNDEFINED;
        } else {
            self->x->ref[i] = data[i];
        }
    }
    for (i = size; i < 4; i++) {
        self->x->ref[i] = ref[i];
    }

    self->x->flag = 0;
    Py_DECREF(value_array);
    return 0;
}


static PyObject* PyCelprm_get_prj(PyCelprm* self, void* closure)
{
    if (is_cel_null(self)) return NULL;
    return (PyObject*)PyPrjprm_cnew((PyObject *)self, &(self->x->prj), NULL);
}


static PyObject* PyCelprm_get_euler(PyCelprm* self, void* closure)
{
    Py_ssize_t size = 5;
    if (is_cel_null(self)) return NULL;
    return get_double_array("euler", self->x->euler, 1, &size, (PyObject*) self);
}


static PyObject* PyCelprm_get_latpreq(PyCelprm* self, void* closure)
{
    if (is_cel_null(self)) return NULL;
    return get_int("lapreq", self->x->latpreq);
}


static PyObject* PyCelprm_get_isolat(PyCelprm* self, void* closure)
{
    if (is_cel_null(self)) {
        return NULL;
    } else {
        return get_bool("isolat", self->x->isolat);
    }
}


/***************************************************************************
 * PyCelprm definition structures
 */

static PyGetSetDef PyCelprm_getset[] = {
    {"offset", (getter)PyCelprm_get_offset, (setter)PyCelprm_set_offset, (char *)doc_cel_offset},
    {"phi0", (getter)PyCelprm_get_phi0, (setter)PyCelprm_set_phi0, (char *)doc_celprm_phi0},
    {"theta0", (getter)PyCelprm_get_theta0, (setter)PyCelprm_set_theta0, (char *)doc_celprm_theta0},
    {"ref", (getter)PyCelprm_get_ref, (setter)PyCelprm_set_ref, (char *)doc_celprm_ref},
    {"euler", (getter)PyCelprm_get_euler, NULL, (char *)doc_celprm_euler},
    {"latpreq", (getter)PyCelprm_get_latpreq, NULL, (char *)doc_celprm_latpreq},
    {"isolat", (getter)PyCelprm_get_isolat, NULL, (char *)doc_celprm_isolat},
    {"_flag", (getter)PyCelprm_get_flag, NULL, ""},
    {"prj", (getter)PyCelprm_get_prj, NULL, (char *)doc_celprm_prj},
    {NULL}
};


static PyMethodDef PyCelprm_methods[] = {
    {"set", (PyCFunction)PyCelprm_set, METH_NOARGS, doc_set_celprm},
    {"__copy__", (PyCFunction)PyCelprm_copy, METH_NOARGS, ""},
    {"__deepcopy__", (PyCFunction)PyCelprm_deepcopy, METH_O, ""},
    {NULL}
};

static PyType_Spec PyCelprmType_spec = {
    .name = "astropy.wcs.Celprm",
    .basicsize = sizeof(PyCelprm),
    .itemsize = 0,
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_IMMUTABLETYPE,
    .slots = (PyType_Slot[]){
        {Py_tp_dealloc, (destructor)PyCelprm_dealloc},
        {Py_tp_str, (reprfunc)PyCelprm___str__},
        {Py_tp_doc, doc_Celprm},
        {Py_tp_traverse, (traverseproc)PyCelprm_traverse},
        {Py_tp_clear, (inquiry)PyCelprm_clear},
        {Py_tp_methods, PyCelprm_methods},
        {Py_tp_getset, PyCelprm_getset},
        {Py_tp_new, PyCelprm_new},
        {0, NULL},
    },
};

PyObject* PyCelprmType = NULL;

int _setup_celprm_type(PyObject* m)
{
    PyCelprmType = PyType_FromSpec(&PyCelprmType_spec);
    if (PyCelprmType == NULL) return -1;
    PyModule_AddObject(m, "Celprm", PyCelprmType);

    cel_errexc[0] = NULL;                         /* Success */
    cel_errexc[1] = &PyExc_MemoryError;           /* Null celprm pointer passed */
    cel_errexc[2] = &WcsExc_InvalidPrjParameters; /* Invalid projection parameters */
    cel_errexc[3] = &WcsExc_InvalidTransform;     /* Invalid coordinate transformation parameters */
    cel_errexc[4] = &WcsExc_InvalidTransform;     /* Ill-conditioned coordinate transformation parameters */
    cel_errexc[5] = &WcsExc_InvalidCoordinate;    /* One or more of the (x,y) coordinates were invalid */
    cel_errexc[6] = &WcsExc_InvalidCoordinate;    /* One or more of the (lng,lat) coordinates were invalid */

    return 0;
}
