Environments
============

The :py:mod:`argon.envs` module provides a standard way to define and manage jax-enabled environments.

.. currentmodule:: argon.env

An :py:class:`Environment` is an object supporting the following protocol:

.. autoclass:: Environment
    :no-index:
    :members:
    :undoc-members:
    :show-inheritance:
    :inherited-members:

all methods *must* be jax-jitable.

Similar to :py:mod:`argon.datasets`, 
the :py:attr:`argon.env.environments` provides an :py:class:`EnvironmentRegistry`
of all available environments.

For instance a linear system can be instantiated as follows:

.. code-block:: python

    from argon.env import environments

    # creates a 1-d double integrator test system
    double_integrator = environments.create("linear_system/double_integrator")
    # can equivalently be created with
    custom_di = environments.create("linear_system", 
        A=jnp.array([[1, 1], [0, 1]]),
        B=jnp.array([[0], [1]]),
        Q=jnp.array([[1, 0], [0, 1]]),
        R=0.01*jnp.array([[1]]),
    )

    # rollout the environment
    import argon.policy as policy
    # roll out the double integrator envrionment for 10 steps
    policy.rollout(double_integrator.step,
        double_integrator.reset(PRNGKey(42)), length=10)