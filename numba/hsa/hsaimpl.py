from __future__ import print_function, absolute_import, division

import operator
from functools import reduce

from llvmlite.llvmpy.core import Type
import llvmlite.llvmpy.core as lc
import llvmlite.binding as ll

from numba.targets.imputils import implement, Registry
from numba import cgutils
from numba import types
from numba.itanium_mangler import mangle_c
from . import target
from . import stubs
from . import hlc

registry = Registry()
register = registry.register

# -----------------------------------------------------------------------------


def _declare_function(context, builder, name, sig, cargs):
    """Insert declaration for a opencl builtin function.
    Uses the Itanium mangler.

    Args
    ----
    context: target context

    builder: llvm builder

    name: str
        symbol name

    sig: signature
        function signature of the symbol being declared

    cargs: sequence of str
        C type names for the arguments

    """
    mod = cgutils.get_module(builder)
    llretty = context.get_value_type(sig.return_type)
    llargs = [context.get_value_type(t) for t in sig.args]
    fnty = Type.function(llretty, llargs)
    mangled = mangle_c(name, cargs)
    fn = mod.get_or_insert_function(fnty, mangled)
    fn.calling_convention = target.CC_SPIR_FUNC
    return fn


@register
@implement(stubs.get_global_id, types.uint32)
def get_global_id_impl(context, builder, sig, args):
    [dim] = args
    get_global_id = _declare_function(context, builder, 'get_global_id', sig,
                                      ['unsigned int'])
    res = builder.call(get_global_id, [dim])
    return context.cast(builder, res, types.uintp, types.intp)


@register
@implement(stubs.get_local_id, types.uint32)
def get_local_id_impl(context, builder, sig, args):
    [dim] = args
    get_local_id = _declare_function(context, builder, 'get_local_id', sig,
                                     ['unsigned int'])
    res = builder.call(get_local_id, [dim])
    return context.cast(builder, res, types.uintp, types.intp)


@register
@implement(stubs.get_global_size, types.uint32)
def get_global_size_impl(context, builder, sig, args):
    [dim] = args
    get_global_size = _declare_function(context, builder, 'get_global_size',
                                        sig, ['unsigned int'])
    res = builder.call(get_global_size, [dim])
    return context.cast(builder, res, types.uintp, types.intp)


@register
@implement(stubs.get_local_size, types.uint32)
def get_local_size_impl(context, builder, sig, args):
    [dim] = args
    get_local_size = _declare_function(context, builder, 'get_local_size',
                                       sig, ['unsigned int'])
    res = builder.call(get_local_size, [dim])
    return context.cast(builder, res, types.uintp, types.intp)


@register
@implement(stubs.barrier, types.uint32)
def get_local_size_impl(context, builder, sig, args):
    [flags] = args
    barrier = _declare_function(context, builder, 'barrier', sig,
                                ['unsigned int'])
    return builder.call(barrier, [flags])


@register
@implement('hsail.smem.alloc', types.Kind(types.UniTuple), types.Any)
def hsail_smem_alloc_array(context, builder, sig, args):
    shape, dtype = args
    return _generic_array(context, builder, shape=shape, dtype=dtype,
                          symbol_name='_hsapy_smem',
                          addrspace=target.SPIR_LOCAL_ADDRSPACE)


def _generic_array(context, builder, shape, dtype, symbol_name, addrspace):
    elemcount = reduce(operator.mul, shape)
    lldtype = context.get_data_type(dtype)
    laryty = Type.array(lldtype, elemcount)

    if addrspace == target.SPIR_LOCAL_ADDRSPACE:
        lmod = cgutils.get_module(builder)

        # Create global variable in the requested address-space
        gvmem = lmod.add_global_variable(laryty, symbol_name, addrspace)

        if elemcount <= 0:
            raise ValueError("array length <= 0")
        else:
            gvmem.linkage = lc.LINKAGE_INTERNAL
            gvmem.initializer = lc.Constant.null(laryty)

        if dtype not in types.number_domain:
            raise TypeError("unsupported type: %s" % dtype)

        # Convert to generic address-space
        dataptr = context.addrspacecast(builder, gvmem,
                                        target.SPIR_GENERIC_ADDRSPACE)

    else:
        raise NotImplementedError("addrspace {addrspace}".foramt(**locals()))

    return _make_array(context, builder, dataptr, dtype, shape)


def _make_array(context, builder, dataptr, dtype, shape, layout='C'):
    ndim = len(shape)
    # Create array object
    aryty = types.Array(dtype=dtype, ndim=ndim, layout='C')
    ary = context.make_array(aryty)(context, builder)
    ary.data = builder.bitcast(dataptr, ary.data.type)

    targetdata = _get_target_data(context)
    lldtype = context.get_data_type(dtype)
    itemsize = lldtype.get_abi_size(targetdata)
    # Compute strides
    rstrides = [itemsize]
    for i, lastsize in enumerate(reversed(shape[1:])):
        rstrides.append(lastsize * rstrides[-1])
    strides = [s for s in reversed(rstrides)]

    kshape = [context.get_constant(types.intp, s) for s in shape]
    kstrides = [context.get_constant(types.intp, s) for s in strides]

    ary.shape = cgutils.pack_array(builder, kshape)
    ary.strides = cgutils.pack_array(builder, kstrides)

    return ary._getvalue()


def _get_target_data(context):
    return ll.create_target_data(hlc.DATALAYOUT[context.address_size])