# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utilities for working with tree-like container data structures.

This module provides a small set of utility functions for working with tree-like
data structures, such as nested tuples, lists, and dicts. We call these
structures pytrees. They are trees in that they are defined recursively (any
non-pytree is a pytree, i.e. a leaf, and any pytree of pytrees is a pytree) and
can be operated on recursively (object identity equivalence is not preserved by
mapping operations, and the structures cannot contain reference cycles).

The set of Python types that are considered pytree nodes (e.g. that can be
mapped over, rather than treated as leaves) is extensible. There is a single
module-level registry of types, and class hierarchy is ignored. By registering a
new pytree node type, that type in effect becomes transparent to the utility
functions in this file.

The primary purpose of this module is to enable the interoperability between
user defined data structures and JAX transformations (e.g. `jit`). This is not
meant to be a general purpose tree-like data structure handling library.

See the `JAX pytrees note <pytrees.html>`_
for examples.
"""


import functools
import collections
import operator as op
from typing import Any, Callable, Optional, Sequence, Tuple, Type, TypeVar, overload

from .lib import pytree

from ._src.util import partial, safe_zip, unzip2

from ._src import traceback_util
traceback_util.register_exclusion(__file__)

T = TypeVar("T")
U = TypeVar("U")

def tree_flatten(tree, is_leaf: Optional[Callable[[Any], bool]] = None):
  """Flattens a pytree.

  Args:
    tree: a pytree to flatten.
    is_leaf: an optionally specified function that will be called at each
      flattening step. It should return a boolean, which indicates whether
      the flattening should traverse the current object, or if it should be
      stopped immediately, with the whole subtree being treated as a leaf.

  Returns:
    A pair where the first element is a list of leaf values and the second
    element is a treedef representing the structure of the flattened tree.
  """
  return pytree.flatten(tree, is_leaf)


def tree_unflatten(treedef, leaves):
  """Reconstructs a pytree from the treedef and the leaves.

  The inverse of :func:`tree_flatten`.

  Args:
    treedef: the treedef to reconstruct
    leaves: the list of leaves to use for reconstruction. The list must match
      the leaves of the treedef.

  Returns:
    The reconstructed pytree, containing the ``leaves`` placed in the structure
    described by ``treedef``.
  """
  return treedef.unflatten(leaves)

def tree_leaves(tree):
  """Gets the leaves of a pytree."""
  return pytree.flatten(tree)[0]

def tree_structure(tree):
  """Gets the treedef for a pytree."""
  return pytree.flatten(tree)[1]

def treedef_tuple(treedefs):
  """Makes a tuple treedef from a list of child treedefs."""
  return pytree.tuple(list(treedefs))

def treedef_children(treedef):
  return treedef.children()

def treedef_is_leaf(treedef):
  return treedef.num_nodes == 1

def all_leaves(iterable):
  """Tests whether all elements in the given iterable are all leaves.

  >>> tree = {"a": [1, 2, 3]}
  >>> assert all_leaves(jax.tree_leaves(tree))
  >>> assert not all_leaves([tree])

  This function is useful in advanced cases, for example if a library allows
  arbitrary map operations on a flat list of leaves it may want to check if
  the result is still a flat list of leaves.

  Args:
    iterable: Iterable of leaves.

  Returns:
    A boolean indicating if all elements in the input are leaves.
  """
  return pytree.all_leaves(iterable)

# The auxiliary is hashable, but because mypy has poor support for Hashable, we
# annotate it as Any.
def register_pytree_node(nodetype: Type[T],
                         flatten_func: Callable[[T], Tuple[Sequence[Any], Any]],
                         unflatten_func: Callable[[Any, Sequence[Any]], T]):
  """Extends the set of types that are considered internal nodes in pytrees.

  See `example usage <pytrees.html>`_.

  Args:
    nodetype: a Python type to treat as an internal pytree node.
    flatten_func: a function to be used during flattening, taking a value of
      type ``nodetype`` and returning a pair, with (1) an iterable for the
      children to be flattened recursively, and (2) some hashable auxiliary
      data to be stored in the treedef and to be passed to the
      ``unflatten_func``.
    unflatten_func: a function taking two arguments: the auxiliary data that was
      returned by ``flatten_func`` and stored in the treedef, and the
      unflattened children. The function should return an instance of
      ``nodetype``.
  """
  pytree.register_node(nodetype, flatten_func, unflatten_func)
  _registry[nodetype] = _RegistryEntry(flatten_func, unflatten_func)

def register_pytree_node_class(cls):
  """Extends the set of types that are considered internal nodes in pytrees.

  This function is a thin wrapper around ``register_pytree_node``, and provides
  a class-oriented interface::

    @register_pytree_node_class
    class Special:
      def __init__(self, x, y):
        self.x = x
        self.y = y
      def tree_flatten(self):
        return ((self.x, self.y), None)
      @classmethod
      def tree_unflatten(cls, aux_data, children):
        return cls(*children)
  """
  register_pytree_node(cls, op.methodcaller('tree_flatten'), cls.tree_unflatten)
  return cls

def tree_map(f: Callable[[Any], Any], tree: Any,
             is_leaf: Optional[Callable[[Any], bool]] = None) -> Any:
  """Maps a function over a pytree to produce a new pytree.

  Args:
    f: unary function to be applied at each leaf.
    tree: a pytree to be mapped over.
    is_leaf: an optionally specified function that will be called at each
      flattening step. It should return a boolean, which indicates whether
      the flattening should traverse the current object, or if it should be
      stopped immediately, with the whole subtree being treated as a leaf.

  Returns:
    A new pytree with the same structure as `tree` but with the value at each
    leaf given by ``f(x)`` where ``x`` is the value at the corresponding leaf in
    the input ``tree``.
  """
  leaves, treedef = tree_flatten(tree, is_leaf)
  return treedef.unflatten(map(f, leaves))

def tree_multimap(f: Callable[..., Any], tree: Any, *rest: Any,
                  is_leaf: Optional[Callable[[Any], bool]] = None) -> Any:
  """Maps a multi-input function over pytree args to produce a new pytree.

  Args:
    f: function that takes ``1 + len(rest)`` arguments, to be applied at the
      corresponding leaves of the pytrees.
    tree: a pytree to be mapped over, with each leaf providing the first
      positional argument to ``f``.
    *rest: a tuple of pytrees, each of which has the same structure as tree or
      or has tree as a prefix.
    is_leaf: an optionally specified function that will be called at each
      flattening step. It should return a boolean, which indicates whether
      the flattening should traverse the current object, or if it should be
      stopped immediately, with the whole subtree being treated as a leaf.

  Returns:
    A new pytree with the same structure as ``tree`` but with the value at each
    leaf given by ``f(x, *xs)`` where ``x`` is the value at the corresponding
    leaf in ``tree`` and ``xs`` is the tuple of values at corresponding nodes in
    ``rest``.
  """
  leaves, treedef = tree_flatten(tree, is_leaf)
  all_leaves = [leaves] + [treedef.flatten_up_to(r) for r in rest]
  return treedef.unflatten(f(*xs) for xs in zip(*all_leaves))

# TODO(mattjj,phawkins): consider removing this function
def _process_pytree(process_node, tree):
  leaves, treedef = pytree.flatten(tree)
  return treedef.walk(process_node, None, leaves), treedef

def build_tree(treedef, xs):
  return treedef.from_iterable_tree(xs)

def tree_transpose(outer_treedef, inner_treedef, pytree_to_transpose):
  flat, treedef = tree_flatten(pytree_to_transpose)
  inner_size = inner_treedef.num_leaves
  outer_size = outer_treedef.num_leaves
  if treedef.num_leaves != (inner_size * outer_size):
    expected_treedef = outer_treedef.compose(inner_treedef)
    raise TypeError(f"Mismatch\n{treedef}\n != \n{expected_treedef}")
  flat = iter(flat)
  lol = [[next(flat) for _ in range(inner_size)] for __ in range(outer_size)]
  transposed_lol = zip(*lol)
  subtrees = map(partial(tree_unflatten, outer_treedef), transposed_lol)
  return tree_unflatten(inner_treedef, subtrees)

# TODO(mattjj): remove the Python-side registry when the C++-side registry is
# sufficiently queryable that we can express _replace_nones. That may mean once
# we have a flatten_one function.
_RegistryEntry = collections.namedtuple("_RegistryEntry", ["to_iter", "from_iter"])
_registry = {
    tuple: _RegistryEntry(lambda xs: (xs, None), lambda _, xs: tuple(xs)),
    list: _RegistryEntry(lambda xs: (xs, None), lambda _, xs: list(xs)),
    dict: _RegistryEntry(lambda xs: unzip2(sorted(xs.items()))[::-1],
                         lambda keys, xs: dict(zip(keys, xs))),
    type(None): _RegistryEntry(lambda z: ((), None), lambda _, xs: None),
}
def _replace_nones(sentinel, tree):
  """Replaces ``None`` in ``tree`` with ``sentinel``."""
  if tree is None:
    return sentinel
  handler = _registry.get(type(tree))
  if handler:
    children, metadata = handler.to_iter(tree)
    proc_children = [_replace_nones(sentinel, child) for child in children]
    return handler.from_iter(metadata, proc_children)
  elif isinstance(tree, tuple) and hasattr(tree, '_fields'):
    # handle namedtuple as a special case, based on heuristic
    children = iter(tree)
    proc_children = [_replace_nones(sentinel, child) for child in children]
    return type(tree)(*proc_children)
  else:
    return tree

no_initializer = object()

@overload
def tree_reduce(function: Callable[[T, Any], T],
                tree: Any) -> T:
    ...

@overload
def tree_reduce(function: Callable[[T, Any], T],
                tree: Any,
                initializer: T) -> T:
    ...

def tree_reduce(function: Callable[[T, Any], T],
                tree: Any,
                initializer: Any = no_initializer) -> T:
  if initializer is no_initializer:
    return functools.reduce(function, tree_leaves(tree))
  else:
    return functools.reduce(function, tree_leaves(tree), initializer)

def tree_all(tree):
  return all(tree_leaves(tree))

register_pytree_node(
  collections.OrderedDict,
  lambda x: (list(x.values()), list(x.keys())),
  lambda keys, values: collections.OrderedDict(safe_zip(keys, values)))

register_pytree_node(
  collections.defaultdict,
  lambda x: (tuple(x.values()), (x.default_factory, tuple(x.keys()))),
  lambda s, values: collections.defaultdict(s[0], safe_zip(s[1], values)))


class Partial(functools.partial):
  """A version of functools.partial that works in pytrees.

  Use it for partial function evaluation in a way that is compatible with JAX's
  transformations, e.g., ``Partial(func, *args, **kwargs)``.

  (You need to explicitly opt-in to this behavior because we didn't want to give
  functools.partial different semantics than normal function closures.)

  For example, here is a basic usage of ``Partial`` in a manner similar to
  ``functools.partial``:

  >>> import jax.numpy as jnp
  >>> add_one = Partial(jnp.add, 1)
  >>> add_one(2)
  DeviceArray(3, dtype=int32)

  Pytree compatibility means that the resulting partial function can be passed
  as an argument within transformed JAX functions, which is not possible with a
  standard ``functools.partial`` function:

  >>> from jax import jit
  >>> @jit
  ... def call_func(f, *args):
  ...   return f(*args)
  ...
  >>> call_func(add_one, 2)
  DeviceArray(3, dtype=int32)

  Passing zero arguments to ``Partial`` effectively wraps the original function,
  making it a valid argument in JAX transformed functions:

  >>> call_func(Partial(jnp.add), 1, 2)
  DeviceArray(3, dtype=int32)

  Had we passed ``jnp.add`` to ``call_func`` directly, it would have resulted in a
  ``TypeError``.

  Note that if the result of ``Partial`` is used in the context where the
  value is traced, it results in all bound arguments being traced when passed
  to the partially-evaluated function:

  >>> print_zero = Partial(print, 0)
  >>> print_zero()
  0
  >>> call_func(print_zero)
  Traced<ShapedArray(int32[], weak_type=True)>with<DynamicJaxprTrace(level=0/1)>
  """

register_pytree_node(
    Partial,
    lambda partial_: ((partial_.args, partial_.keywords), partial_.func),
    lambda func, xs: Partial(func, *xs[0], **xs[1]),
)
