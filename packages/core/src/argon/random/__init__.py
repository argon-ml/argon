import jax.random
import jax.numpy
import jax

from flax.nnx import Rngs

from jax.random import (
    key, split, fold_in,
    uniform, normal, bernoulli, randint,
    permutation, choice
)

def key_or_seed(key_or_seed):
    if isinstance(key_or_seed, int):
        key_or_seed = jax.random.key(key_or_seed)
    elif (hasattr(key_or_seed, "shape") and
        hasattr(key_or_seed, "dtype") and \
        (key_or_seed.dtype == jax.numpy.uint32 or \
         jax.dtypes.issubdtype(key_or_seed.dtype,
         jax.dtypes.prng_key))):
        key_or_seed = key_or_seed
    else:
        raise ValueError("Not key or seed!")
    return key_or_seed

class PRNGSequence:
    def __init__(self, key_or_val, _cell=None):
        self._key = key_or_seed(key_or_val)
    
    def __next__(self):
        k, n = jax.jit(
            jax.random.split,
            static_argnums=1
        )(self._key)
        self._key = k
        return n

    def __repr__(self):
        return f"PRNGSequence({self._key})"

def sequence(key_or_val : jax.Array) -> PRNGSequence:
    return PRNGSequence(key_or_val)