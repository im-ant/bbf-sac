# coding=utf-8

import sys
import collections
import random
import copy
import functools
import itertools
import time
import scipy
import math

from absl import logging
from flax.core.frozen_dict import FrozenDict
import gin
import jax
import jax.numpy as jnp
import numpy as np
import optax
import flax

from bbf import spr_networks
from bbf.replay_memory import subsequence_replay_buffer, circular_replay_buffer

NATURE_DQN_OBSERVATION_SHAPE = (84, 84)  # Size of downscaled Atari 2600 frame.
NATURE_DQN_DTYPE = np.uint8  # DType of Atari 2600 observations.
NATURE_DQN_STACK_SIZE = 4  # Number of frames in the state stack.


def project_distribution(supports, weights, target_support):
    """Projects a batch of (support, weights) onto target_support.

  Based on equation (7) in (Bellemare et al., 2017):
    https://arxiv.org/abs/1707.06887
  In the rest of the comments we will refer to this equation simply as Eq7.

  Args:
    supports: Jax array of shape (num_dims) defining supports for
      the distribution.
    weights: Jax array of shape (num_dims) defining weights on the
      original support points. Although for the CategoricalDQN agent these
      weights are probabilities, it is not required that they are.
    target_support: Jax array of shape (num_dims) defining support of the
      projected distribution. The values must be monotonically increasing. Vmin
      and Vmax will be inferred from the first and last elements of this Jax
      array, respectively. The values in this Jax array must be equally spaced.

  Returns:
    A Jax array of shape (num_dims) with the projection of a batch
    of (support, weights) onto target_support.

  Raises:
    ValueError: If target_support has no dimensions, or if shapes of supports,
      weights, and target_support are incompatible.
  """
    v_min, v_max = target_support[0], target_support[-1]
    # `N` in Eq7.
    num_dims = target_support.shape[0]
    # delta_z = `\Delta z` in Eq7.
    delta_z = (v_max - v_min) / (num_dims - 1)
    # clipped_support = `[\hat{T}_{z_j}]^{V_max}_{V_min}` in Eq7.
    clipped_support = jnp.clip(supports, v_min, v_max)
    # numerator = `|clipped_support - z_i|` in Eq7.
    numerator = jnp.abs(clipped_support - target_support[:, None])
    quotient = 1 - (numerator / delta_z)
    # clipped_quotient = `[1 - numerator / (\Delta z)]_0^1` in Eq7.
    clipped_quotient = jnp.clip(quotient, 0, 1)
    # inner_prod = `\sum_{j=0}^{N-1} clipped_quotient * p_j(x', \pi(x'))` in Eq7.
    inner_prod = clipped_quotient * weights
    return jnp.squeeze(jnp.sum(inner_prod, -1))


def softmax_cross_entropy_loss_with_logits(labels: jnp.array,
                                           logits: jnp.array) -> jnp.ndarray:
    """Implementation of the softmax cross entropy loss."""
    return -jnp.sum(labels * flax.linen.log_softmax(logits))


def prefetch_to_device(iterator, size):
    queue = collections.deque()

    def enqueue(n):  # Enqueues *up to* `n` elements from the iterator.
        for data in itertools.islice(iterator, n):
            #queue.append(jax.device_put(data, device=jax.local_devices()[0]))
            queue.append(data)

    enqueue(size)  # Fill up the buffer.
    while queue:
        yield queue.popleft()
        enqueue(1)


def copy_within_frozen_tree(old, new, prefix):
    new_entry = old[prefix].copy(add_or_replace=new)
    return old.copy(add_or_replace={prefix: new_entry})


def copy_params(source, target, keys=("encoder", "transition_model")):
    """Copies a set of keys from one set of params to another.

  Args:
    source: Set of parameters to take keys from.
    target: Set of parameters to overwrite keys in.
    keys: Set of keys to copy.

  Returns:
    A parameter dictionary of the same shape as target.
  """
    if (isinstance(source, dict) or
            isinstance(source, collections.OrderedDict) or
            isinstance(source, FrozenDict)):
        fresh_dict = {}
        for k, v in source.items():
            if k in keys:
                fresh_dict[k] = v
            else:
                fresh_dict[k] = copy_params(source[k], target[k], keys)
        return fresh_dict
    else:
        return target


@functools.partial(jax.jit, static_argnames=("keys", "strip_params_layer"))
def interpolate_weights(
    old_params,
    new_params,
    keys,
    old_weight=0.5,
    new_weight=0.5,
    strip_params_layer=True,
):
    """Interpolates between two parameter dictionaries.

  Args:
    old_params: The first parameter dictionary.
    new_params: The second parameter dictionary, of same shape and structure.
    keys: Which keys in the parameter dictionaries to interpolate. If None,
      interpolates everything.
    old_weight: The weight to place on the old dictionary.
    new_weight: The weight to place on the new dictionary.
    strip_params_layer: Whether to strip an outer "params" layer, as is often
      present in e.g., Flax.

  Returns:
    A parameter dictionary of the same shape as the inputs.
  """
    if strip_params_layer:
        old_params = old_params["params"]
        new_params = new_params["params"]

    def combination(old_param, new_param):
        return old_param * old_weight + new_param * new_weight

    combined_params = {}
    if keys is None:
        keys = old_params.keys()
    for k in keys:
        combined_params[k] = jax.tree_util.tree_map(combination, old_params[k],
                                                    new_params[k])
    for k, v in old_params.items():
        if k not in keys:
            combined_params[k] = v

    if strip_params_layer:
        combined_params = {"params": combined_params}
    return FrozenDict(combined_params)


@functools.partial(
    jax.jit,
    static_argnames=(
        "do_rollout",
        "state_shape",
        "keys_to_copy",
        "shrink_perturb_keys",
        "reset_target",
        "network_def",
        "optimizer",
    ),
)
def jit_reset(
    online_params,
    target_network_params,
    optimizer_state,
    network_def,
    optimizer,
    rng,
    state_shape,
    do_rollout,
    support,
    reset_target,
    shrink_perturb_keys,
    shrink_factor,
    perturb_factor,
    keys_to_copy,
):
    """A jittable function to reset network parameters.

  Args:
    online_params: Parameter dictionary for the online network.
    target_network_params: Parameter dictionary for the target network.
    optimizer_state: Optax optimizer state.
    network_def: Network definition.
    optimizer: Optax optimizer.
    rng: JAX PRNG key.
    state_shape: Shape of the network inputs.
    do_rollout: Whether to do a dynamics model rollout (e.g., if SPR is being
      used).
    support: Support of the categorical distribution if using distributional RL.
    reset_target: Whether to also reset the target network.
    shrink_perturb_keys: Parameter keys to apply shrink-and-perturb to.
    shrink_factor: Factor to rescale current weights by (1 keeps , 0 deletes).
    perturb_factor: Factor to scale random noise by in [0, 1].
    keys_to_copy: Keys to copy over without resetting.

  Returns:
  """
    online_rng, target_rng = jax.random.split(rng, 2)
    state = jnp.zeros(state_shape, dtype=jnp.float32)
    # Create some dummy actions of arbitrary length to initialize the transition
    # model, if the network has one.
    actions = jnp.zeros((5,))
    random_params = flax.core.frozen_dict.FrozenDict(
        network_def.init(
            online_rng,
            method=network_def.init_fn,
            x=state,
            actions=actions,
            do_rollout=do_rollout,
            support=support,
        ))
    target_random_params = flax.core.frozen_dict.FrozenDict(
        network_def.init(
            target_rng,
            method=network_def.init_fn,
            x=state,
            actions=actions,
            do_rollout=do_rollout,
            support=support,
        ))
    if shrink_perturb_keys:
        online_params = interpolate_weights(
            online_params,
            random_params,
            shrink_perturb_keys,
            old_weight=shrink_factor,
            new_weight=perturb_factor,
        )
    online_params = FrozenDict(
        copy_params(online_params, random_params, keys=keys_to_copy))

    updated_optim_state = []
    optim_state = optimizer.init(online_params)
    for i in range(len(optim_state)):
        optim_to_copy = copy_params(
            dict(optimizer_state[i]._asdict()),
            dict(optim_state[i]._asdict()),
            keys=keys_to_copy,
        )
        optim_to_copy = FrozenDict(optim_to_copy)
        updated_optim_state.append(optim_state[i]._replace(**optim_to_copy))
    optimizer_state = tuple(updated_optim_state)

    if reset_target:
        if shrink_perturb_keys:
            target_network_params = interpolate_weights(
                target_network_params,
                target_random_params,
                shrink_perturb_keys,
                old_weight=shrink_factor,
                new_weight=perturb_factor,
            )
        target_network_params = copy_params(target_network_params,
                                            target_random_params,
                                            keys=keys_to_copy)
        target_network_params = FrozenDict(target_network_params)

    return online_params, target_network_params, optimizer_state, random_params


def exponential_decay_scheduler(decay_period,
                                warmup_steps,
                                initial_value,
                                final_value,
                                reverse=False):
    """Instantiate a logarithmic schedule for a parameter.

  By default the extreme point to or from which values decay logarithmically
  is 0, while changes near 1 are fast. In cases where this may not
  be correct (e.g., lambda) pass reversed=True to get proper
  exponential scaling.

  Args:
      decay_period: float, the period over which the value is decayed.
      warmup_steps: int, the number of steps taken before decay starts.
      initial_value: float, the starting value for the parameter.
      final_value: float, the final value for the parameter.
      reverse: bool, whether to treat 1 as the asmpytote instead of 0.

  Returns:
      A decay function mapping step to parameter value.
  """
    if reverse:
        initial_value = 1 - initial_value
        final_value = 1 - final_value

    start = np.log(initial_value)
    end = np.log(final_value)

    if decay_period == 0:
        return lambda x: initial_value if x < warmup_steps else final_value

    def scheduler(step):
        steps_left = decay_period + warmup_steps - step
        bonus_frac = steps_left / decay_period
        bonus = np.clip(bonus_frac, 0.0, 1.0)
        new_value = bonus * (start - end) + end

        new_value = np.exp(new_value)
        if reverse:
            new_value = 1 - new_value
        return new_value

    return scheduler


@functools.partial(jax.jit, static_argnames=[
    "network_def",
    "eval_mode",
])
def select_action(
    network_def,
    params,
    state,
    rng,
    num_actions,
    eval_mode,
):
    rng, key = jax.random.split(rng)
    state = spr_networks.process_inputs(state,
                                        rng=key,
                                        data_augmentation=False,
                                        dtype=jnp.float32)

    #epsilon = jnp.where(eval_mode, 1e-3, 0)

    def logits_w_samples(state, action_sample_key):
        return network_def.apply(
            params,
            state,
            rngs={"action_sample": action_sample_key},
            method=network_def.get_policy,
        )

    rng, key = jax.random.split(key)
    key = jax.random.split(key, state.shape[0])
    logits, samples = jax.vmap(logits_w_samples, in_axes=0,
                               axis_name="batch")(state, key)
    new_actions = jnp.where(eval_mode, jnp.argmax(logits, axis=-1), samples)
    return rng, new_actions, jax.nn.softmax(logits)

    #best_actions = jnp.argmax(logits, axis=-1)

    #rng, key0, key1 = jax.random.split(rng, num=3)
    #p = jax.random.uniform(key0, shape=(state.shape[0],))
    #new_actions = jnp.where(
    #    p < epsilon,
    #    jax.random.randint(
    #        key1,
    #        (state.shape[0],),
    #        0,
    #        num_actions,
    #    ),
    #    best_actions,
    #)
    ##return rng, new_actions, jax.nn.softmax(logits)
    #return rng, samples, jax.nn.softmax(logits)


train_static_argnames = [
    'network_def',
    'optimizer',
    'double_dqn',
    'distributional',
    'spr_weight',
    'data_augmentation',
    'dtype',
    'batch_size',
    'use_target_backups',
    'match_online_target_rngs',
    'target_eval_mode',
]


def train(
    network_def,  # 0, static
    online_params,  # 1
    target_params,  # 2
    optimizer,  # 3, static
    optimizer_state,  # 4
    raw_states,  # 5
    actions,  # 6
    raw_next_states,  # 7
    rewards,  # 8
    terminals,  # 9
    same_traj_mask,  # 10
    loss_weights,  # 11
    support,  # 12
    cumulative_gamma,  # 13
    double_dqn,  # 14, static
    distributional,  # 15, static
    rng,  # 16
    spr_weight,  # 17, static (gates rollouts)
    data_augmentation,  # static
    dtype,  # static
    batch_size,  # static
    use_target_backups,  # static
    target_update_tau,
    target_update_every,
    step,
    match_online_target_rngs,  # static
    target_eval_mode,  # static
    #ent_targ,
    x_ent_coef,
):

    @functools.partial(
        jax.jit,
        donate_argnums=(0,),
    )
    def train_one_batch(state, inputs):
        """Runs a training step."""
        # Unpack inputs from scan
        (
            online_params,
            target_params,
            optimizer_state,
            rng,
            step,
        ) = state
        (
            raw_states,
            actions,
            raw_next_states,
            rewards,
            terminals,
            same_traj_mask,
            loss_weights,
            cumulative_gamma,
        ) = inputs
        same_traj_mask = same_traj_mask[:, 1:]
        rewards = rewards[:, 0]
        terminals = terminals[:, 0]
        cumulative_gamma = cumulative_gamma[:, 0]

        rng, rng1, rng2 = jax.random.split(rng, num=3)
        states = spr_networks.process_inputs(
            raw_states,
            rng=rng1,
            data_augmentation=data_augmentation,
            dtype=dtype)
        next_states = spr_networks.process_inputs(
            raw_next_states[:, 0],
            rng=rng2,
            data_augmentation=data_augmentation,
            dtype=dtype,
        )
        current_state = states[:, 0]

        # Split the current rng to update the rng after this call
        rng, rng1, rng2 = jax.random.split(rng, num=3)

        batch_rngs = jax.random.split(rng, num=states.shape[0])
        if match_online_target_rngs:
            target_rng = batch_rngs
        else:
            target_rng = jax.random.split(rng1, num=states.shape[0])
        use_spr = spr_weight > 0

        def policy_online(state, action_sample_key):
            return network_def.apply(
                online_params,
                state,
                rngs={"action_sample": action_sample_key},
                method=network_def.get_policy,
            )

        def q_target(state):
            return network_def.apply(
                target_params,
                state,
                support=support,
                eval_mode=target_eval_mode,
            )

        def encode_project(state):
            return network_def.apply(
                target_params,
                state,
                eval_mode=True,
                method=network_def.encode_project,
            )

        def loss_fn(
            params,
            target,
            spr_targets,
            loss_multipliers,
            key,
        ):

            def all_results(state, actions, do_rollout):
                return network_def.apply(params,
                                         state,
                                         support,
                                         actions,
                                         do_rollout,
                                         method=network_def.init_fn)

            def policy_loss(q_values, logits, x_key):
                samples = jax.random.categorical(x_key, logits)

                log_prob = jax.nn.log_softmax(logits)
                prob = jax.nn.softmax(logits)
                q_values = q_values[samples] - (q_values * prob).sum()
                ent_coef = network_def.apply(params,
                                             method=network_def.entropy_scale)
                x_ent = -(prob * log_prob).sum()
                #if True:
                if False:
                    return -(jax.lax.stop_gradient(q_values) * log_prob[samples]
                            ) + ent_coef * (-x_ent + ent_targ), x_ent
                else:
                    return -(jax.lax.stop_gradient(q_values) *
                             log_prob[samples]) + x_ent_coef * (-x_ent), x_ent
                    #return -(jax.lax.stop_gradient(q_values) *
                    #         log_prob[samples]), x_ent

            x, logits = jax.vmap(all_results,
                                 in_axes=(0, 0, None),
                                 axis_name="batch")(current_state,
                                                    actions[:, :-1], use_spr)
            spr_predictions = x.latent
            q_logits = jnp.squeeze(x.logits)
            chosen_action_logits = q_logits[jnp.arange(q_logits.shape[0]),
                                            actions[:, 0]]
            dqn_loss = jax.vmap(softmax_cross_entropy_loss_with_logits)(
                target, chosen_action_logits)
            td_error = dqn_loss + jnp.nan_to_num(
                target * jnp.log(target)).sum(-1)

            spr_predictions = spr_predictions.transpose(1, 0, 2)
            spr_predictions = spr_predictions.reshape(
                *spr_predictions.shape[:2], 2, 2048)
            # lezhang.thu
            #logging.info("spr_predictions.shape: {}".format(spr_predictions.shape))

            spr_predictions = spr_predictions / jnp.linalg.norm(
                spr_predictions, 2, -1, keepdims=True)

            spr_targets = spr_targets.reshape(*spr_targets.shape[:2], 2, 2048)
            # lezhang.thu
            #logging.info("spr_targets.shape: {}".format(spr_targets.shape))

            spr_targets = spr_targets / jnp.linalg.norm(
                spr_targets, 2, -1, keepdims=True)
            spr_loss = jnp.power(spr_predictions - spr_targets, 2).sum((-1, -2))
            #logging.info("spr_loss.shape: {}".format(spr_loss.shape))
            spr_loss = (spr_loss * same_traj_mask.transpose(1, 0)).mean(0) * .5
            #logging.info("spr_loss.shape: {}".format(spr_loss.shape))
            #exit(0)
            loss = dqn_loss + spr_weight * spr_loss
            loss = loss_multipliers * loss

            mean_loss = jnp.mean(loss)

            x = jax.vmap(policy_loss, in_axes=0, axis_name="batch")(x.q_values,
                                                                    logits, key)
            aux_losses = {
                "TotalLoss": jnp.mean(mean_loss),
                "DQNLoss": jnp.mean(dqn_loss),
                "TD Error": jnp.mean(td_error),
                "SPRLoss": jnp.mean(spr_loss),
                "ent": jnp.mean(x[1]),
            }
            return mean_loss + jnp.mean(loss_multipliers * x[0]), (aux_losses)

        # Use the weighted mean loss for gradient computation.
        target = jax.vmap(target_output,
                          in_axes=(None, None, 0, 0, 0, None, 0, 0),
                          axis_name="batch")(
                              policy_online,
                              q_target,
                              next_states,
                              rewards,
                              terminals,
                              support,
                              cumulative_gamma,
                              target_rng,
                          )

        future_states = states[:, 1:]
        spr_targets = jax.vmap(jax.vmap(encode_project,
                                        in_axes=0,
                                        axis_name="time"),
                               in_axes=0,
                               axis_name="batch")(future_states)
        spr_targets = spr_targets.transpose(1, 0, 2)

        # Get the unweighted loss without taking its mean for updating priorities.
        grad_fn = jax.value_and_grad(loss_fn, has_aux=True)
        x = jax.random.split(rng2, current_state.shape[0] + 1)
        rng2 = x[0]
        key = x[1:]
        (_, aux_losses), grad = grad_fn(
            online_params,
            target,
            spr_targets,
            loss_weights,
            key,
        )

        updates, new_optimizer_state = optimizer.update(grad,
                                                        optimizer_state,
                                                        params=online_params)
        new_online_params = optax.apply_updates(online_params, updates)

        optimizer_state = new_optimizer_state
        online_params = new_online_params

        target_update_step = functools.partial(
            interpolate_weights,
            keys=None,
            old_weight=1 - target_update_tau,
            new_weight=target_update_tau,
        )
        target_params = jax.lax.cond(
            step % target_update_every == 0,
            target_update_step,
            lambda old, new: old,
            target_params,
            online_params,
        )

        return (
            (
                online_params,
                target_params,
                optimizer_state,
                rng2,
                step + 1,
            ),
            aux_losses,
        )

    init_state = (
        online_params,
        target_params,
        optimizer_state,
        rng,
        step,
    )
    assert raw_states.shape[0] % batch_size == 0
    num_batches = raw_states.shape[0] // batch_size

    # debug - start
    #print(" num_batches: {}\n batch_size: {}".format(num_batches, batch_size))
    #print(" raw_policy_states: {}".format(raw_policy_states))
    #exit(0)
    # debug - end

    inputs = (
        raw_states.reshape(num_batches, batch_size, *raw_states.shape[1:]),
        actions.reshape(num_batches, batch_size, *actions.shape[1:]),
        raw_next_states.reshape(num_batches, batch_size,
                                *raw_next_states.shape[1:]),
        rewards.reshape(num_batches, batch_size, *rewards.shape[1:]),
        terminals.reshape(num_batches, batch_size, *terminals.shape[1:]),
        same_traj_mask.reshape(num_batches, batch_size,
                               *same_traj_mask.shape[1:]),
        loss_weights.reshape(num_batches, batch_size, *loss_weights.shape[1:]),
        cumulative_gamma.reshape(num_batches, batch_size,
                                 *cumulative_gamma.shape[1:]),
    )

    (
        (
            online_params,
            target_params,
            optimizer_state,
            rng,
            step,
        ),
        aux_losses,
    ) = jax.lax.scan(train_one_batch, init_state, inputs)

    return (
        online_params,
        target_params,
        optimizer_state,
        {k: jnp.reshape(v, (-1,)) for k, v in aux_losses.items()},
    )


def target_output(
    policy_info,
    target_network,
    next_states,
    rewards,
    terminals,
    support,
    cumulative_gamma,
    rng,
):
    gamma_with_terminal = (cumulative_gamma *
                           (1.0 - terminals.astype(jnp.float32)))
    target_dist = target_network(next_states)
    _, next_qt_argmax = policy_info(next_states, rng)

    # Compute the target Q-value distribution
    probabilities = jnp.squeeze(target_dist.probabilities)
    next_probabilities = probabilities[next_qt_argmax]
    target_support = rewards + gamma_with_terminal * support
    target = project_distribution(target_support, next_probabilities, support)

    return jax.lax.stop_gradient(target)


@gin.configurable
def create_scaling_optimizer(
    learning_rate=6.25e-5,
    beta1=0.9,
    beta2=0.999,
    eps=1.5e-4,
    centered=False,
    weight_decay=0.0,
):
    logging.info(
        ("Creating AdamW optimizer with settings lr=%f, beta1=%f, "
         "beta2=%f, eps=%f, wd=%f"),
        learning_rate,
        beta1,
        beta2,
        eps,
        weight_decay,
    )
    mask = lambda p: jax.tree_util.tree_map(lambda x: x.ndim != 1, p)
    return optax.adamw(
        learning_rate,
        b1=beta1,
        b2=beta2,
        eps=eps,
        weight_decay=weight_decay,
        mask=mask,
    )


@gin.configurable
class JaxDQNAgent(object):

    def __init__(
        self,
        num_actions,
        observation_shape=NATURE_DQN_OBSERVATION_SHAPE,
        observation_dtype=NATURE_DQN_DTYPE,
        stack_size=NATURE_DQN_STACK_SIZE,
        network=None,
        gamma=0.99,
        update_horizon=1,
        min_replay_history=20000,
        update_period=4,
        target_update_period=8000,
        epsilon_train=0.01,
        epsilon_eval=0.001,
        epsilon_decay_period=250000,
        eval_mode=False,
        optimizer='adam',
        allow_partial_reload=False,
        seed=None,
        loss_type='mse',
        preprocess_fn=None,
    ):
        assert isinstance(observation_shape, tuple)
        seed = int(time.time() * 1e6) if seed is None else seed
        logging.info('Creating %s agent with the following parameters:',
                     self.__class__.__name__)
        logging.info('\t gamma: %f', gamma)
        logging.info('\t update_horizon: %f', update_horizon)
        logging.info('\t min_replay_history: %d', min_replay_history)
        logging.info('\t update_period: %d', update_period)
        logging.info('\t target_update_period: %d', target_update_period)
        logging.info('\t optimizer: %s', optimizer)
        logging.info('\t seed: %d', seed)
        logging.info('\t loss_type: %s', loss_type)
        logging.info('\t preprocess_fn: %s', preprocess_fn)
        logging.info('\t allow_partial_reload: %s', allow_partial_reload)

        self.num_actions = num_actions
        self.observation_shape = tuple(observation_shape)
        self.observation_dtype = observation_dtype
        self.stack_size = stack_size
        if preprocess_fn is None:
            self.network_def = network(num_actions=num_actions)
            self.preprocess_fn = lambda x: x
        else:
            self.network_def = network(num_actions=num_actions,
                                       inputs_preprocessed=True)
            self.preprocess_fn = preprocess_fn
        self.gamma = gamma
        self.update_horizon = update_horizon
        self.cumulative_gamma = math.pow(gamma, update_horizon)
        self.min_replay_history = min_replay_history
        self.target_update_period = target_update_period
        self.update_period = update_period
        self.eval_mode = eval_mode
        self.training_steps = 0
        self.allow_partial_reload = allow_partial_reload
        self._loss_type = loss_type

        self._rng = jax.random.PRNGKey(seed)
        state_shape = self.observation_shape + (stack_size,)
        self.state = np.zeros(state_shape)
        self._replay = self._build_replay_buffer()
        self._optimizer_name = optimizer
        self._build_networks_and_optimizer()

        # Variables to be initialized by the agent once it interacts with the
        # environment.
        self._observation = None
        self._last_observation = None


@gin.configurable
class BBFAgent(JaxDQNAgent):
    """A compact implementation of the full Rainbow agent."""

    def __init__(
        self,
        num_actions,
        double_dqn=True,
        distributional=True,
        data_augmentation=False,
        num_updates_per_train_step=1,
        network=spr_networks.RainbowDQNNetwork,
        num_atoms=51,
        vmax=10.0,
        vmin=None,
        jumps=0,
        spr_weight=0,
        batch_size=32,
        replay_ratio=64,
        batches_to_group=1,
        update_horizon=10,
        max_update_horizon=None,
        min_gamma=None,
        reset_every=-1,
        no_resets_after=-1,
        reset_offset=1,
        learning_rate=0.0001,
        encoder_learning_rate=0.0001,
        reset_target=True,
        reset_head=True,
        reset_projection=True,
        reset_encoder=False,
        reset_interval_scaling=None,
        shrink_perturb_keys="",
        perturb_factor=0.2,  # original was 0.1
        shrink_factor=0.8,  # original was 0.4
        target_update_tau=1.0,
        max_target_update_tau=None,
        cycle_steps=0,
        target_update_period=1,
        target_action_selection=False,
        use_target_network=True,
        match_online_target_rngs=True,
        target_eval_mode=False,
        offline_update_frac=0,
        half_precision=False,
        seed=None,
        log_every=None,
        explore_end_steps=None,
    ):
        logging.info(
            "Creating %s agent with the following parameters:",
            self.__class__.__name__,
        )
        logging.info("\t double_dqn: %s", double_dqn)
        logging.info("\t distributional: %s", distributional)
        logging.info("\t data_augmentation: %s", data_augmentation)
        logging.info("\t num_updates_per_train_step: %d",
                     num_updates_per_train_step)
        # We need casting because passing arguments can convert ints to floats
        vmax = float(vmax)
        self._num_atoms = int(num_atoms)
        vmin = float(vmin) if vmin else -vmax
        self._support = jnp.linspace(vmin, vmax, self._num_atoms)
        self._double_dqn = bool(double_dqn)
        self._distributional = bool(distributional)
        self._data_augmentation = bool(data_augmentation)
        self._replay_ratio = int(replay_ratio)
        self._batch_size = int(batch_size)
        self._batches_to_group = int(batches_to_group)
        self.update_horizon = int(update_horizon)
        self._jumps = int(jumps)
        self.spr_weight = spr_weight

        self.reset_every = int(reset_every)
        self.reset_target = reset_target
        self.reset_head = reset_head
        self.reset_projection = reset_projection
        self.reset_encoder = reset_encoder
        self.offline_update_frac = float(offline_update_frac)
        self.no_resets_after = int(no_resets_after)
        self.cumulative_resets = 0
        self.reset_interval_scaling = reset_interval_scaling
        self.reset_offset = int(reset_offset)
        self.next_reset = self.reset_every + self.reset_offset

        self.learning_rate = learning_rate
        self.encoder_learning_rate = encoder_learning_rate

        self.shrink_perturb_keys = [
            s for s in shrink_perturb_keys.lower().split(",") if s
        ]
        self.shrink_perturb_keys = tuple(self.shrink_perturb_keys)
        self.shrink_factor = shrink_factor
        self.perturb_factor = perturb_factor

        self.target_action_selection = target_action_selection
        self.use_target_network = use_target_network
        self.match_online_target_rngs = match_online_target_rngs
        self.target_eval_mode = target_eval_mode

        # debug - start
        print('*' * 20)
        print(' self.target_eval_mode: {}'.format(self.target_eval_mode))
        print(' self.target_action_selection: {}'.format(
            self.target_action_selection))
        print(" num_actions: {}".format(num_actions))
        print(" self.reset_target: {}".format(self.reset_target))
        # debug - end

        self.grad_steps = 0
        self.cycle_grad_steps = 0
        self.target_update_period = int(target_update_period)
        self.target_update_tau = target_update_tau

        if max_update_horizon is None:
            self.max_update_horizon = self.update_horizon
            self.update_horizon_scheduler = lambda x: self.update_horizon
        else:
            self.max_update_horizon = int(max_update_horizon)
            n_schedule = exponential_decay_scheduler(
                cycle_steps, 0, 1,
                self.update_horizon / self.max_update_horizon)
            self.update_horizon_scheduler = lambda x: int(  # pylint: disable=g-long-lambda
                np.round(n_schedule(x) * self.max_update_horizon))

        self.max_target_update_tau = target_update_tau
        self.target_update_tau_scheduler = lambda x: self.target_update_tau

        logging.info("\t Found following local devices: %s",
                     str(jax.local_devices()))

        self.dtype = jnp.float32
        self.dtype_str = "float32"

        logging.info("\t Running with dtype %s", str(self.dtype))

        super().__init__(
            num_actions=num_actions,
            network=functools.partial(
                network,
                num_atoms=self._num_atoms,
                noisy=False,
                distributional=self._distributional,
                dtype=self.dtype,
            ),
            target_update_period=self.target_update_period,
            update_horizon=self.max_update_horizon,
            seed=seed,
        )

        self.set_replay_settings()

        if min_gamma is None or cycle_steps <= 1:
            self.min_gamma = self.gamma
            self.gamma_scheduler = lambda x: self.gamma
        else:
            self.min_gamma = min_gamma
            self.gamma_scheduler = exponential_decay_scheduler(cycle_steps,
                                                               0,
                                                               self.min_gamma,
                                                               self.gamma,
                                                               reverse=True)

        self.cumulative_gamma = (np.ones(
            (self.max_update_horizon,)) * self.gamma).cumprod()

        self.train_fn = jax.jit(train,
                                static_argnames=train_static_argnames,
                                device=jax.local_devices()[0])

        # debug - start
        self.greedy_action = False
        # debug - end
        self.stats_ent = 0
        self.explore_end_steps = explore_end_steps
        print('explore_end_steps: {}'.format(explore_end_steps))
        sys.stdout.flush()

    def _build_networks_and_optimizer(self):
        self._rng, rng = jax.random.split(self._rng)
        self.state_shape = self.state.shape

        # Create some dummy actions of arbitrary length to initialize the transition
        # model, if the network has one.
        actions = jnp.zeros((5,))
        self.online_params = flax.core.frozen_dict.FrozenDict(
            self.network_def.init(
                rng,
                method=self.network_def.init_fn,
                x=self.state.astype(self.dtype),
                actions=actions,
                do_rollout=self.spr_weight > 0,
                support=self._support,
            ))

        optimizer = create_scaling_optimizer(learning_rate=self.learning_rate,)
        encoder_optimizer = create_scaling_optimizer(
            learning_rate=self.encoder_learning_rate,)
        policy_optim = create_scaling_optimizer(learning_rate=1e-4,)

        encoder_keys = {"encoder", "transition_model"}
        encoder_mask = FrozenDict({
            "params": {
                k: k in encoder_keys for k in self.online_params["params"]
            }
        })

        head_keys = {"projection", "head", "predictor"}
        head_mask = FrozenDict({
            "params": {k: k in head_keys for k in self.online_params["params"]}
        })

        #policy_key = {"policy_projection", "policy", "policy_predict"}
        policy_key = {"policy_projection", "policy", "predict_policy"}
        policy_mask = FrozenDict({
            "params": {
                k: k in policy_key for k in self.online_params["params"]
            }
        })

        alpha_optim = optax.sgd(learning_rate=-1e-3)
        alpha_key = {"_log_alpha"}
        alpha_mask = FrozenDict({
            "params": {k: k in alpha_key for k in self.online_params["params"]}
        })
        #print(" alpha_mask:\n{}".format(alpha_mask))
        #print(" policy_mask:\n{}".format(policy_mask))
        #exit(0)

        # debug - start
        if False:
            print(' self.head_mask: {}'.format(self.head_mask))
            print(
                ' jax.tree_util.tree_map(lambda x: x.shape, self.online_params["params"]["projection"]: {}'
                .format(
                    jax.tree_util.tree_map(
                        lambda x: x.shape,
                        self.online_params["params"]["projection"])))
            print(
                ' jax.tree_util.tree_map(lambda x: x.shape, self.online_params["params"]["predictor"]: {}'
                .format(
                    jax.tree_util.tree_map(
                        lambda x: x.shape,
                        self.online_params["params"]["predictor"])))
            print(
                ' jax.tree_util.tree_map(lambda x: x.shape, self.online_params["params"]["head"]: {}'
                .format(
                    jax.tree_util.tree_map(
                        lambda x: x.shape,
                        self.online_params["params"]["head"])))
            #exit(0)
        # debug - end

        self.optimizer = optax.chain(
            optax.masked(encoder_optimizer, encoder_mask),
            optax.masked(optimizer, head_mask),
            optax.masked(policy_optim, policy_mask),
            optax.masked(alpha_optim, alpha_mask),
        )

        self.optimizer_state = self.optimizer.init(self.online_params)
        self.target_network_params = copy.deepcopy(self.online_params)
        self.random_params = copy.deepcopy(self.online_params)

        #print(' so far so good')
        #exit(0)

    def _build_replay_buffer(self):
        prioritized_buffer = subsequence_replay_buffer.PrioritizedJaxSubsequenceParallelEnvReplayBuffer(
            observation_shape=self.observation_shape,
            stack_size=self.stack_size,
            update_horizon=self.max_update_horizon,
            gamma=self.gamma,
            subseq_len=self._jumps + 1,
            batch_size=self._batch_size,
            observation_dtype=self.observation_dtype,
        )

        self.n_envs = prioritized_buffer._n_envs  # pylint: disable=protected-access
        self.start = time.time()
        return prioritized_buffer

    def set_replay_settings(self):
        logging.info(
            "\t Operating with %s environments, batch size %s and replay ratio %s",
            self.n_envs, self._batch_size, self._replay_ratio)
        self._num_updates_per_train_step = max(
            1, self._replay_ratio * self.n_envs // self._batch_size)
        self.update_period = max(
            1, self._batch_size // self._replay_ratio * self.n_envs)
        logging.info(
            "\t Calculated %s updates per update phase",
            self._num_updates_per_train_step,
        )
        logging.info(
            "\t Calculated update frequency of %s step%s",
            self.update_period,
            "s" if self.update_period > 1 else "",
        )
        logging.info(
            "\t Setting min_replay_history to %s from %s",
            self.min_replay_history / self.n_envs,
            self.min_replay_history,
        )
        self.min_replay_history = self.min_replay_history / self.n_envs
        self._batches_to_group = min(self._batches_to_group,
                                     self._num_updates_per_train_step)
        assert self._num_updates_per_train_step % self._batches_to_group == 0
        self._num_updates_per_train_step = int(
            max(1, self._num_updates_per_train_step / self._batches_to_group))

        # debug - start
        print(
            " self._num_updates_per_train_step: {}\n self._batches_to_group: {}"
            .format(self._num_updates_per_train_step, self._batches_to_group))
        #exit(0)
        # debug - end

        logging.info(
            "\t Running %s groups of %s batch%s per %s env step%s",
            self._num_updates_per_train_step,
            self._batches_to_group,
            "es" if self._batches_to_group > 1 else "",
            self.update_period,
            "s" if self.update_period > 1 else "",
        )

    def _replay_sampler_generator(self):
        types = self._replay.get_transition_elements()
        while True:
            self._rng, rng = jax.random.split(self._rng)

            samples = self._replay.sample_transition_batch(
                rng,
                batch_size=self._batch_size * self._batches_to_group,
                update_horizon=self.update_horizon_scheduler(
                    self.cycle_grad_steps),
                gamma=self.gamma_scheduler(self.cycle_grad_steps),
            )
            replay_elements = collections.OrderedDict()
            for element, element_type in zip(samples, types):
                replay_elements[element_type.name] = element
            yield replay_elements

    def sample_eval_batch(self, batch_size, subseq_len=1):
        self._rng, rng = jax.random.split(self._rng)
        samples = self._replay.sample_transition_batch(rng,
                                                       batch_size=batch_size,
                                                       subseq_len=subseq_len)
        types = self._replay.get_transition_elements()
        replay_elements = collections.OrderedDict()
        for element, element_type in zip(samples, types):
            replay_elements[element_type.name] = element
        # Add code for data augmentation.

        return replay_elements

    def initialize_prefetcher(self):
        self.prefetcher = prefetch_to_device(self._replay_sampler_generator(),
                                             2)

    def _sample_from_replay_buffer(self):
        self.replay_elements = next(self.prefetcher)

    def reset_weights(self):
        self.cumulative_resets += 1
        interval = self.reset_every

        self.next_reset = int(interval) + self.training_steps
        if self.next_reset > self.no_resets_after + self.reset_offset:
            logging.info(
                "\t Not resetting at step %s, as need at least"
                " %s before %s to recover.", self.training_steps, interval,
                self.no_resets_after)
            return
        else:
            logging.info("\t Resetting weights at step %s.",
                         self.training_steps)

        self._rng, reset_rng = jax.random.split(self._rng, 2)

        #keys_to_copy = ("encoder", "transition_model")
        keys_to_copy = ("encoder", "transition_model", "_log_alpha")
        (
            self.online_params,
            self.target_network_params,
            self.optimizer_state,
            self.random_params,
        ) = jit_reset(
            self.online_params,
            self.target_network_params,
            self.optimizer_state,
            self.network_def,
            self.optimizer,
            reset_rng,
            self.state_shape,
            self.spr_weight > 0,
            self._support,
            self.reset_target,
            self.shrink_perturb_keys,
            self.shrink_factor,
            self.perturb_factor,
            keys_to_copy,
        )

        self.cycle_grad_steps = 0

    def _training_step_update(self, step_index, offline=False):
        """Gradient update during every training step."""
        self.start = time.time()

        if not hasattr(self, "replay_elements"):
            self._sample_from_replay_buffer()

        # The original prioritized experience replay uses a linear exponent
        # schedule 0.4 -> 1.0. Comparing the schedule to a fixed exponent of
        # 0.5 on 5 games (Asterix, Pong, Q*Bert, Seaquest, Space Invaders)
        # suggested a fixed exponent actually performs better, except on Pong.
        probs = self.replay_elements["sampling_probabilities"]
        # Weight the loss by the inverse priorities.
        loss_weights = 1.0 / np.sqrt(probs + 1e-10)
        loss_weights /= np.max(loss_weights)
        indices = self.replay_elements["indices"]

        if False:
            # debug - start
            print(' self.replay_elements.keys():\n {}'.format(
                self.replay_elements.keys()))
            print(' self._jumps + 1: {}'.format(self._jumps + 1))
            if False:
                print(
                    ' self.update_horizon_scheduler(self.cycle_grad_steps): {}'.
                    format(self.update_horizon_scheduler(
                        self.cycle_grad_steps)))
            for k, v in self.replay_elements.items():
                print(' {}: {}'.format(k, v.shape))

            exit(0)
            # debug - end

        self._rng, train_rng = jax.random.split(self._rng)
        (
            new_online_params,
            new_target_params,
            new_optimizer_state,
            aux_losses,
        ) = self.train_fn(
            self.network_def,
            self.online_params,
            self.target_network_params,
            self.optimizer,
            self.optimizer_state,
            self.replay_elements["state"],
            self.replay_elements["action"],
            self.replay_elements["next_state"],
            self.replay_elements["return"],
            self.replay_elements["terminal"],
            self.replay_elements["same_trajectory"],
            loss_weights,
            self._support,
            self.replay_elements["discount"],
            self._double_dqn,
            self._distributional,
            train_rng,
            self.spr_weight,
            self._data_augmentation,
            self.dtype,
            self._batch_size,
            self.use_target_network,
            self.target_update_tau_scheduler(self.cycle_grad_steps),
            self.target_update_period,
            self.grad_steps,
            self.match_online_target_rngs,
            self.target_eval_mode,
            #self.ent_targ,
            self.x_ent_coef,
        )
        self.grad_steps += self._batches_to_group
        self.cycle_grad_steps += self._batches_to_group

        # Sample asynchronously while we wait for training
        self._sample_from_replay_buffer()
        # Rainbow and prioritized replay are parametrized by an exponent
        # alpha, but in both cases it is set to 0.5 - for simplicity's sake we
        # leave it as is here, using the more direct sqrt(). Taking the square
        # root "makes sense", as we are dealing with a squared loss.  Add a
        # small nonzero value to the loss to avoid 0 priority items. While
        # technically this may be okay, setting all items to 0 priority will
        # cause troubles, and also result in 1.0 / 0.0 = NaN correction terms.
        indices = np.reshape(np.asarray(indices), (-1,))
        dqn_loss = np.reshape(np.asarray(aux_losses["DQNLoss"]), (-1))

        # debug - start
        #if random.uniform(0, 1) < 1e-3:
        if False:
            logging.info("ent: {}".format(aux_losses["ent"]))
        # debug - end

        priorities = np.sqrt(dqn_loss + 1e-10)
        self._replay.set_priority(indices, priorities)

        self.target_network_params = new_target_params
        self.online_params = new_online_params
        self.optimizer_state = new_optimizer_state

    def _store_transition(
        self,
        last_observation,
        action,
        reward,
        is_terminal,
        *args,
        episode_end=False,
    ):
        priority = np.full((last_observation.shape[0]),
                           self._replay.sum_tree.max_recorded_priority)

        if not self.eval_mode:
            self._replay.add(
                last_observation,
                action,
                reward,
                is_terminal,
                *args,
                priority=priority,
                episode_end=episode_end,
            )

    def _train_step(self):
        # linearly decay target entropy - start
        def linearly_decaying_epsilon(decay_period, step, warmup_steps,
                                      epsilon):
            # Begin at 1. until warmup_steps steps have been taken; then
            # Linearly decay epsilon from 1. to epsilon in decay_period steps; and then
            # Use epsilon from there on.
            steps_left = decay_period + warmup_steps - step
            if False:
                bonus = (1.0 - epsilon) * steps_left / decay_period
                bonus = jnp.clip(bonus, 0., 1. - epsilon)
            # Begin at 0.5 until warmup_steps steps have been taken; then
            # Linearly decay epsilon from 0.5 to epsilon in decay_period steps; and then
            elif False:
                bonus = (0.5 - epsilon) * steps_left / decay_period
                bonus = jnp.clip(bonus, 0., 0.5 - epsilon)
            else:
                bonus = (1e-2 - epsilon) * steps_left / decay_period
                bonus = jnp.clip(bonus, 0., 1e-2 - epsilon)
            return epsilon + bonus

        ##frac = linearly_decaying_epsilon(1e5, self.training_steps, 0, 0.01)
        #frac = linearly_decaying_epsilon(self.explore_end_steps,
        #                                 self.training_steps, 0, 1e-3)
        #x = np.full((self.num_actions,),
        #            fill_value=frac / self.num_actions,
        #            dtype=np.float32)
        #x[0] += 1 - frac
        #self.ent_targ = jnp.asarray(scipy.stats.entropy(x))
        ##if random.uniform(0, 1) < 1e-3:
        #if False:
        #    logging.info("step: {}, frac: {}, ent_targ: {}".format(
        #        self.training_steps, frac, self.ent_targ))
        ##exit(0)
        # linearly decay target entropy - end

        self.x_ent_coef = linearly_decaying_epsilon(int(80e3),
                                                    self.training_steps, 0, .0)
        if random.uniform(0, 1) < 1e-3:
            logging.info("step: {}, x_ent_coef: {}".format(
                self.training_steps, self.x_ent_coef))

        if self._replay.add_count == self.min_replay_history:
            self.initialize_prefetcher()

        if self._replay.add_count > self.min_replay_history:
            if self.training_steps % self.update_period == 0:
                for i in range(self._num_updates_per_train_step):
                    self._training_step_update(i, offline=False)
        if self.reset_every > 0 and self.training_steps > self.next_reset:
            self.reset_weights()
        # debug - start
        #if random.uniform(0, 1) < 1e-3:
        if False:
            ent_coef = self.network_def.apply(
                self.online_params, method=self.network_def.entropy_scale)
            logging.info("ent_coef: {}".format(ent_coef))
            #logging.info("self.ent_targ: {}".format(self.ent_targ))
        # debug - end
        self.training_steps += 1

        # Cool down gpu
        #time.sleep(0.1)

    def _reset_state(self, n_envs):
        """Resets the agent state by filling it with zeros."""
        self.state = np.zeros(n_envs, *self.state_shape)

    def _record_observation(self, observation):
        """Records an observation and update state.

    Extracts a frame from the observation vector and overwrites the oldest
    frame in the state buffer.

    Args:
      observation: numpy array, an observation from the environment.
    """
        # Set current observation. We do the reshaping to handle environments
        # without frame stacking.
        observation = observation.squeeze(-1)
        if len(observation.shape) == len(self.observation_shape):
            self._observation = np.reshape(observation, self.observation_shape)
        else:
            self._observation = np.reshape(
                observation, (observation.shape[0], *self.observation_shape))
        # Swap out the oldest frame with the current frame.
        self.state = np.roll(self.state, -1, axis=-1)
        self.state[Ellipsis, -1] = self._observation

    def reset_all(self, new_obs):
        """Resets the agent state by filling it with zeros."""
        n_envs = new_obs.shape[0]
        self.state = np.zeros((n_envs, *self.state_shape))
        self._record_observation(new_obs)

    def reset_one(self, env_id):
        self.state[env_id].fill(0)

    def delete_one(self, env_id):
        self.state = np.concatenate(
            [self.state[:env_id], self.state[env_id + 1:]], 0)

    def cache_train_state(self):
        self.training_state = (
            copy.deepcopy(self.state),
            copy.deepcopy(self._last_observation),
            copy.deepcopy(self._observation),
        )

    def restore_train_state(self):
        (self.state, self._last_observation,
         self._observation) = (self.training_state)

    def log_transition(self, observation, action, reward, terminal,
                       episode_end):
        self._last_observation = self._observation
        self._record_observation(observation)

        if not self.eval_mode:
            self._store_transition(
                self._last_observation,
                action,
                reward,
                terminal,
                episode_end=episode_end,
            )

    def select_action(
        self,
        state,
        select_params,
        eval_mode,
    ):
        if not eval_mode and self.training_steps < self.min_replay_history:
            self._rng, key = jax.random.split(self._rng)
            return jax.random.randint(
                key,
                (state.shape[0],),
                0,
                self.num_actions,
            )
        self._rng, action, probs = select_action(
            self.network_def,
            select_params,
            state,
            self._rng,
            self.num_actions,
            False,
            #eval_mode,
            #eval_mode=self.greedy_action,
            #eval_mode=True,
        )
        #print(probs.shape)
        #if not self.eval_mode:
        if not self.eval_mode:
            self.stats_ent = 0.99 * self.stats_ent + 0.01 * scipy.stats.entropy(
                probs[0])
            if random.uniform(0, 1) < 1e-3:
                logging.info('ema entropy: {}'.format(self.stats_ent))
        #exit(0)
        return action

    def step(self):
        """Records the most recent transition, returns the agent's next action, and trains if appropriate.
    """
        if not self.eval_mode:
            self._train_step()
        state = self.state
        select_params = self.target_network_params
        #select_params = self.online_params
        ## time inference - start
        #self.eval_mode = True
        #import time
        #start_time = time.time()
        #for _ in range(int(100 * 32)):
        action = self.select_action(
            state,
            select_params,
            self.eval_mode,
        )
        self.action = np.asarray(action)
        #time_delta = time.time() - start_time
        #print('time_delta: {}'.format(time_delta))
        #exit(0)
        return self.action
