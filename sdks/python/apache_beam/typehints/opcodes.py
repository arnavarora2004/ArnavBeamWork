#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Defines the actions various bytecodes have on the frame.

Each function here corresponds to a bytecode documented in
https://docs.python.org/2/library/dis.html or
https://docs.python.org/3/library/dis.html. The first argument is a (mutable)
FrameState object, the second the integer opcode argument.

Bytecodes with more complicated behavior (e.g. modifying the program counter)
are handled inline rather than here.

For internal use only; no backwards-compatibility guarantees.
"""
# pytype: skip-file

import inspect
import logging
import sys
import types
from functools import reduce

from apache_beam.typehints import row_type
from apache_beam.typehints import typehints
from apache_beam.typehints.trivial_inference import BoundMethod
from apache_beam.typehints.trivial_inference import Const
from apache_beam.typehints.trivial_inference import element_type
from apache_beam.typehints.trivial_inference import key_value_types
from apache_beam.typehints.trivial_inference import union
from apache_beam.typehints.typehints import Any
from apache_beam.typehints.typehints import Dict
from apache_beam.typehints.typehints import Iterable
from apache_beam.typehints.typehints import List
from apache_beam.typehints.typehints import Set
from apache_beam.typehints.typehints import Tuple
from apache_beam.typehints.typehints import Union

# This is missing in the builtin types module.  str.upper is arbitrary, any
# method on a C-implemented type will do.
_MethodDescriptorType = type(str.upper)

if sys.version_info >= (3, 11):
  import opcode
  _div_binop_args = frozenset([
      ix for (ix, (argname, _)) in enumerate(opcode._nb_ops)
      if 'TRUE_DIVIDE' in argname
  ])
else:
  _div_binop_args = frozenset()


def pop_one(state, unused_arg):
  del state.stack[-1:]


def pop_two(state, unused_arg):
  del state.stack[-2:]


def pop_three(state, unused_arg):
  del state.stack[-3:]


def push_value(v):
  def pusher(state, unused_arg):
    state.stack.append(v)

  return pusher


def nop(unused_state, unused_arg):
  pass


resume = nop


def pop_top(state, unused_arg):
  state.stack.pop()


def end_for(state, unused_arg):
  state.stack.pop()
  state.stack.pop()


def end_send(state, unused_arg):
  del state.stack[-2]


def copy(state, arg):
  state.stack.append(state.stack[-arg])


def rot_n(state, n):
  state.stack[-n:] = [state.stack[-1]] + state.stack[-n:-1]


def rot_two(state, unused_arg):
  rot_n(state, 2)


def rot_three(state, unused_arg):
  rot_n(state, 3)


def rot_four(state, unused_arg):
  rot_n(state, 4)


def dup_top(state, unused_arg):
  state.stack.append(state.stack[-1])


def unary(state, unused_arg):
  state.stack[-1] = Const.unwrap(state.stack[-1])


unary_positive = unary_negative = unary_invert = unary


def unary_not(state, unused_arg):
  state.stack[-1] = bool


def unary_convert(state, unused_arg):
  state.stack[-1] = str


def get_iter(state, unused_arg):
  state.stack.append(Iterable[element_type(state.stack.pop())])


_NUMERIC_PROMOTION_LADDER = [bool, int, float, complex]


def symmetric_binary_op(state, arg, is_true_div=None):
  # TODO(robertwb): This may not be entirely correct...
  b, a = Const.unwrap(state.stack.pop()), Const.unwrap(state.stack.pop())
  if a == b:
    if a is int and b is int and (arg in _div_binop_args or is_true_div):
      state.stack.append(float)
    else:
      state.stack.append(a)
  elif type(a) == type(b) and isinstance(a, typehints.SequenceTypeConstraint):
    state.stack.append(type(a)(union(element_type(a), element_type(b))))
  # Technically these next two will be errors for anything but multiplication,
  # but that's OK.
  elif a is int and (b in (bytes, str) or
                     isinstance(b, typehints.SequenceTypeConstraint)):
    state.stack.append(b)
  elif b is int and (a in (bytes, str) or
                     isinstance(a, typehints.SequenceTypeConstraint)):
    state.stack.append(a)
  elif a in _NUMERIC_PROMOTION_LADDER and b in _NUMERIC_PROMOTION_LADDER:
    state.stack.append(
        _NUMERIC_PROMOTION_LADDER[max(
            _NUMERIC_PROMOTION_LADDER.index(a),
            _NUMERIC_PROMOTION_LADDER.index(b))])
  else:
    state.stack.append(Any)


# Except for int ** -int
binary_power = inplace_power = symmetric_binary_op
binary_multiply = inplace_multiply = symmetric_binary_op
binary_divide = inplace_divide = symmetric_binary_op
binary_floor_divide = inplace_floor_divide = symmetric_binary_op


def binary_true_divide(state, arg):
  return symmetric_binary_op(state, arg, True)


inplace_true_divide = binary_true_divide

binary_modulo = inplace_modulo = symmetric_binary_op
# TODO(robertwb): Tuple add.
binary_add = inplace_add = symmetric_binary_op
binary_subtract = inplace_subtract = symmetric_binary_op


def binary_subscr(state, unused_arg):
  index = state.stack.pop()
  base = Const.unwrap(state.stack.pop())
  if base is str:
    out = base
  elif (isinstance(index, Const) and isinstance(index.value, int) and
        isinstance(base, typehints.IndexableTypeConstraint)):
    try:
      out = base._constraint_for_index(index.value)
    except IndexError:
      out = element_type(base)
  elif index == slice and isinstance(base, typehints.IndexableTypeConstraint):
    out = base
  else:
    out = element_type(base)
  state.stack.append(out)


# As far as types are concerned.
binary_lshift = inplace_lshift = binary_rshift = inplace_rshift = pop_top

binary_and = inplace_and = symmetric_binary_op
binary_xor = inplace_xor = symmetric_binary_op
binary_or = inplace_or = symmetric_binary_op

binary_op = symmetric_binary_op


def store_subscr(unused_state, unused_args):
  # TODO(robertwb): Update element/value type of iterable/dict.
  pass


def binary_slice(state, args):
  _ = state.stack.pop()
  _ = state.stack.pop()
  base = Const.unwrap(state.stack.pop())
  if base is str:
    out = base
  elif isinstance(base, typehints.IndexableTypeConstraint):
    out = base
  else:
    out = element_type(base)
  state.stack.append(out)


def store_slice(state, args):
  """Clears elements off the stack like it was constructing a
  container, but leaves the container type back at stack[-1]
  since that's all that is relevant for type checking.
  """
  _ = state.stack.pop()  # End
  _ = state.stack.pop()  # Start
  container = state.stack.pop()  # Container type
  _ = state.stack.pop()  # Values that would go in container
  state.stack.append(container)


print_item = pop_top
print_newline = nop


def list_append(state, arg):
  new_element_type = Const.unwrap(state.stack.pop())
  state.stack[-arg] = List[union(
      element_type(state.stack[-arg]), new_element_type)]


def set_add(state, arg):
  new_element_type = Const.unwrap(state.stack.pop())
  state.stack[-arg] = Set[union(
      element_type(state.stack[-arg]), new_element_type)]


def map_add(state, arg):
  # PEP 572 The MAP_ADD expects the value as the first element in the stack
  # and the key as the second element.
  new_value_type = Const.unwrap(state.stack.pop())
  new_key_type = Const.unwrap(state.stack.pop())
  state.stack[-arg] = Dict[Union[state.stack[-arg].key_type, new_key_type],
                           Union[state.stack[-arg].value_type, new_value_type]]


load_locals = push_value(Dict[str, Any])
exec_stmt = pop_three
build_class = pop_three


def unpack_sequence(state, arg):
  t = state.stack.pop()
  if isinstance(t, Const):
    try:
      unpacked = [Const(ti) for ti in t.value]
      if len(unpacked) != arg:
        unpacked = [Any] * arg
    except TypeError:
      unpacked = [Any] * arg
  elif (isinstance(t, typehints.TupleHint.TupleConstraint) and
        len(t.tuple_types) == arg):
    unpacked = list(t.tuple_types)
  else:
    unpacked = [element_type(t)] * arg
  state.stack += reversed(unpacked)


def dup_topx(state, arg):
  state.stack += state[-arg:]


store_attr = pop_two
delete_attr = pop_top
store_global = pop_top
delete_global = nop


def load_const(state, arg):
  state.stack.append(state.const_type(arg))


load_name = push_value(Any)


def build_tuple(state, arg):
  if arg == 0:
    state.stack.append(Tuple[()])
  else:
    state.stack[-arg:] = [Tuple[[Const.unwrap(t) for t in state.stack[-arg:]]]]


def build_list(state, arg):
  if arg == 0:
    state.stack.append(List[Union[()]])
  else:
    state.stack[-arg:] = [List[reduce(union, state.stack[-arg:], Union[()])]]


def build_set(state, arg):
  if arg == 0:
    state.stack.append(Set[Union[()]])
  else:
    state.stack[-arg:] = [Set[reduce(union, state.stack[-arg:], Union[()])]]


# A Dict[Union[], Union[]] is the type of an empty dict.
def build_map(state, arg):
  if arg == 0:
    state.stack.append(Dict[Union[()], Union[()]])
  else:
    state.stack[-2 * arg:] = [
        Dict[reduce(union, state.stack[-2 * arg::2], Union[()]),
             reduce(union, state.stack[-2 * arg + 1::2], Union[()])]
    ]


def build_const_key_map(state, arg):
  key_tuple = state.stack.pop()
  if isinstance(key_tuple, typehints.TupleHint.TupleConstraint):
    key_types = key_tuple.tuple_types
  elif isinstance(key_tuple, Const):
    key_types = [Const(v) for v in key_tuple.value]
  else:
    key_types = [Any]
  state.stack[-arg:] = [
      Dict[reduce(union, key_types, Union[()]),
           reduce(union, state.stack[-arg:], Union[()])]
  ]


def list_to_tuple(state, arg):
  base = state.stack.pop()
  state.stack.append(Tuple[element_type(base), ...])


def build_string(state, arg):
  state.stack[-arg:] = [str]


def list_extend(state, arg):
  tail = state.stack.pop()
  base = state.stack[-arg]
  state.stack[-arg] = List[union(element_type(base), element_type(tail))]


def set_update(state, arg):
  other = state.stack.pop()
  base = state.stack[-arg]
  state.stack[-arg] = Set[union(element_type(base), element_type(other))]


def dict_update(state, arg):
  other = state.stack.pop()
  base = state.stack[-arg]
  if isinstance(base, typehints.Dict.DictConstraint):
    base_key_type = base.key_type
    base_value_type = base.value_type
  else:
    base_key_type = Any
    base_value_type = Any
  if isinstance(other, typehints.Dict.DictConstraint):
    other_key_type = other.key_type
    other_value_type = other.value_type
  else:
    other_key_type, other_value_type = key_value_types(element_type(other))
  state.stack[-arg] = Dict[union(base_key_type, other_key_type),
                           union(base_value_type, other_value_type)]


dict_merge = dict_update


def load_attr(state, arg):
  """Replaces the top of the stack, TOS, with
  getattr(TOS, co_names[arg])

  Will replace with Any for builtin methods, but these don't have bytecode in
  CPython so that's okay.
  """
  if (sys.version_info.major, sys.version_info.minor) >= (3, 12):
    # Load attribute's arg was bit-shifted in 3.12 to also allow for
    # adding extra information to the stack based on the lower byte,
    # so we have to adjust it back.
    #
    # See https://docs.python.org/3/library/dis.html#opcode-LOAD_ATTR
    # for more information.
    arg = arg >> 1
  o = state.stack.pop()
  name = state.get_name(arg)
  state.stack.append(_getattr(o, name))


def _getattr(o, name):
  if isinstance(o, Const) and hasattr(o.value, name):
    return Const(getattr(o.value, name))
  elif (inspect.isclass(o) and
        isinstance(getattr(o, name, None),
                   (types.MethodType, types.FunctionType))):
    # TODO(luke-zhu): Support other callable objects
    func = getattr(o, name)  # Python 3 has no unbound methods
    return Const(BoundMethod(func, o))
  elif isinstance(o, row_type.RowTypeConstraint):
    return o.get_type_for(name)
  else:
    return Any


def load_method(state, arg):
  """Like load_attr. Replaces TOS object with method and TOS."""
  o = state.stack.pop()
  name = state.get_name(arg)
  if isinstance(o, Const):
    method = Const(getattr(o.value, name))
  elif isinstance(o, typehints.AnyTypeConstraint):
    method = typehints.Any
  elif hasattr(o, name):
    attr = getattr(o, name)
    if isinstance(attr, _MethodDescriptorType):
      # Skip builtins since they don't disassemble.
      method = typehints.Any
    else:
      method = Const(BoundMethod(attr, o))
  else:
    method = typehints.Any

  state.stack.append(method)


def compare_op(state, unused_arg):
  # Could really be anything...
  state.stack[-2:] = [bool]


is_op = compare_op
contains_op = compare_op


def import_name(state, unused_arg):
  state.stack[-2:] = [Any]


import_from = push_value(Any)


def load_global(state, arg):
  if (sys.version_info.major, sys.version_info.minor) >= (3, 11):
    arg = arg >> 1
  state.stack.append(state.get_global(arg))


store_map = pop_two


def load_fast(state, arg):
  state.stack.append(state.vars[arg])


def load_fast_load_fast(state, arg):
  arg1 = arg >> 4
  arg2 = arg & 15
  state.stack.append(state.vars[arg1])
  state.stack.append(state.vars[arg2])


load_fast_check = load_fast


def load_fast_and_clear(state, arg):
  state.stack.append(state.vars[arg])
  state.vars[arg] = None


def store_fast(state, arg):
  state.vars[arg] = state.stack.pop()


def store_fast_store_fast(state, arg):
  arg1 = arg >> 4
  arg2 = arg & 15
  state.vars[arg1] = state.stack.pop()
  state.vars[arg2] = state.stack.pop()


def store_fast_load_fast(state, arg):
  arg1 = arg >> 4
  arg2 = arg & 15
  state.vars[arg1] = state.stack.pop()
  state.stack.append(state.vars[arg2])


def delete_fast(state, arg):
  state.vars[arg] = Any  # really an error


def swap(state, arg):
  state.stack[-arg], state.stack[-1] = state.stack[-1], state.stack[-arg]


def reraise(state, arg):
  pass


# bpo-43683 Adds GEN_START in Python 3.10, but removed in Python 3.11
# https://github.com/python/cpython/pull/25138
def gen_start(state, arg):
  assert len(state.stack) == 0


def load_closure(state, arg):
  # The arg is no longer offset by len(covar_names) as of 3.11
  # See https://docs.python.org/3/library/dis.html#opcode-LOAD_CLOSURE
  if (sys.version_info.major, sys.version_info.minor) >= (3, 11):
    arg -= len(state.co.co_varnames)
  state.stack.append(state.closure_type(arg))


def load_deref(state, arg):
  # The arg is no longer offset by len(covar_names) as of 3.11
  # See https://docs.python.org/3/library/dis.html#opcode-LOAD_DEREF
  if (sys.version_info.major, sys.version_info.minor) >= (3, 11):
    arg -= len(state.co.co_varnames)
  state.stack.append(state.closure_type(arg))


def make_function(state, arg):
  """Creates a function with the arguments at the top of the stack.
  """
  # TODO(luke-zhu): Handle default argument types
  globals = state.f.__globals__  # Inherits globals from the current frame
  tos = state.stack[-1].value
  # In Python 3.11 lambdas no longer have fully qualified names on the stack,
  # so we check for this case (AKA the code is top of stack.)
  if isinstance(tos, types.CodeType):
    func_name = None
    func_code = tos
    pop_count = 1
    is_lambda = True
  else:
    func_name = tos
    func_code = state.stack[-2].value
    pop_count = 2
    is_lambda = False
  closure = None
  if (sys.version_info.major, sys.version_info.minor) < (3, 13):
    # arg contains flags, with corresponding stack values if positive.
    # https://docs.python.org/3.6/library/dis.html#opcode-MAKE_FUNCTION
    pop_count += bin(arg).count('1')
    if arg & 0x08:
      # Convert types in Tuple constraint to a tuple of CPython cells.
      # https://stackoverflow.com/a/44670295
      if is_lambda:
        closureTuplePos = -2
      else:
        closureTuplePos = -3
      closure = tuple((lambda _: lambda: _)(t).__closure__[0]
                      for t in state.stack[closureTuplePos].tuple_types)

  func = types.FunctionType(func_code, globals, name=func_name, closure=closure)

  assert pop_count <= len(state.stack)
  state.stack[-pop_count:] = [Const(func)]


def set_function_attribute(state, arg):
  func = state.stack.pop().value
  attr = state.stack.pop().value
  closure = None
  if arg & 0x08:
    closure = tuple((lambda _: lambda: _)(t).__closure__[0]
                    for t in state.stack[attr].tuple_types)
  new_func = types.FunctionType(
      func.code, func.globals, name=func.name, closure=closure)
  state.stack.append(Const(new_func))


def make_closure(state, arg):
  state.stack[-arg - 2:] = [Any]  # a callable


def build_slice(state, arg):
  state.stack[-arg:] = [slice]  # a slice object


def to_bool(state, arg):
  state.stack[-1] = bool


def format_value(state, arg):
  if arg & 0x04:
    state.stack.pop()
  state.stack.pop()
  state.stack.append(str)


def convert_value(state, arg):
  state.stack.pop()
  state.stack.append(str)


def format_simple(state, arg):
  state.stack.pop()
  state.stack.append(str)


def format_with_spec(state, arg):
  state.stack.pop()
  state.stack.pop()
  state.stack.append(str)


def _unpack_lists(state, arg):
  """Extract inner types of Lists and Tuples.

  Pops arg count items from the stack, concatenates their inner types into 1
  list, and returns that list.
  Example: if stack[-arg:] == [[i1, i2], [i3]], the output is [i1, i2, i3]
  """
  types = []
  for i in range(arg, 0, -1):
    type_constraint = state.stack[-i]
    if isinstance(type_constraint, typehints.IndexableTypeConstraint):
      types.extend(type_constraint._inner_types())
    elif type_constraint == Union[()]:
      continue
    else:
      logging.debug('Unhandled type_constraint: %r', type_constraint)
      types.append(typehints.Any)
  state.stack[-arg:] = []
  return types


def build_list_unpack(state, arg):
  """Joins arg count iterables from the stack into a single list."""
  state.stack.append(List[Union[_unpack_lists(state, arg)]])


def build_set_unpack(state, arg):
  """Joins arg count iterables from the stack into a single set."""
  state.stack.append(Set[Union[_unpack_lists(state, arg)]])


def build_tuple_unpack(state, arg):
  """Joins arg count iterables from the stack into a single tuple."""
  state.stack.append(Tuple[Union[_unpack_lists(state, arg)], ...])


def build_tuple_unpack_with_call(state, arg):
  """Same as build_tuple_unpack, with an extra fn argument at the bottom of the
  stack, which remains untouched."""
  build_tuple_unpack(state, arg)


def build_map_unpack(state, arg):
  """Joins arg count maps from the stack into a single dict."""
  key_types = []
  value_types = []
  for _ in range(arg):
    type_constraint = state.stack.pop()
    if isinstance(type_constraint, typehints.Dict.DictConstraint):
      key_types.append(type_constraint.key_type)
      value_types.append(type_constraint.value_type)
    else:
      key_type, value_type = key_value_types(element_type(type_constraint))
      key_types.append(key_type)
      value_types.append(value_type)
  state.stack.append(Dict[Union[key_types], Union[value_types]])
