# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
"""
Cython-optimized MCCFR core functions.

Compile with:
    cythonize -i mccfr_core.pyx
"""

import random
from libc.stdlib cimport rand, RAND_MAX
from cpython.dict cimport PyDict_GetItem, PyDict_SetItem

import numpy as np
cimport numpy as np

ctypedef np.float64_t DTYPE_t


cpdef dict get_strategy_fast(dict regrets, list legal_actions):
    """
    Fast regret matching.

    Args:
        regrets: dict {action_id: cumulative_regret}
        legal_actions: list of action ids

    Returns:
        dict {action_id: probability}
    """
    cdef:
        dict strategy = {}
        double positive_sum = 0.0
        double r, uniform
        int action
        int n_actions = len(legal_actions)

    # Sum positive regrets
    for action in legal_actions:
        r = regrets.get(action, 0.0)
        if r > 0:
            strategy[action] = r
            positive_sum += r
        else:
            strategy[action] = 0.0

    # Normalize or use uniform
    if positive_sum > 0:
        for action in legal_actions:
            strategy[action] /= positive_sum
    else:
        uniform = 1.0 / n_actions
        for action in legal_actions:
            strategy[action] = uniform

    return strategy


cpdef int sample_action(list actions, list weights):
    """
    Fast weighted random sampling.

    Args:
        actions: list of action ids
        weights: list of probabilities (same length)

    Returns:
        sampled action id
    """
    cdef:
        double r = random.random()
        double cumsum = 0.0
        int i, n = len(actions)

    for i in range(n):
        cumsum += weights[i]
        if r <= cumsum:
            return actions[i]

    return actions[n - 1]


cpdef void update_regrets(dict regret_sum, str info_state, list legal_actions,
                          dict action_values, dict strategy):
    """
    Update regret table in-place.

    Args:
        regret_sum: the regret table to update
        info_state: info state string key
        legal_actions: list of action ids
        action_values: dict {action_id: value}
        strategy: dict {action_id: probability}
    """
    cdef:
        double ev = 0.0
        double regret
        int action
        dict state_regrets

    # Compute expected value
    for action in legal_actions:
        ev += strategy[action] * action_values[action]

    # Get or create regret dict for this state
    if info_state not in regret_sum:
        regret_sum[info_state] = {}
    state_regrets = regret_sum[info_state]

    # Update regrets
    for action in legal_actions:
        regret = action_values[action] - ev
        if action in state_regrets:
            state_regrets[action] += regret
        else:
            state_regrets[action] = regret


cpdef void update_strategy_sum(dict strategy_sum, str info_state,
                                list legal_actions, dict strategy):
    """
    Accumulate strategy for average policy.

    Args:
        strategy_sum: the strategy sum table to update
        info_state: info state string key
        legal_actions: list of action ids
        strategy: dict {action_id: probability}
    """
    cdef:
        int action
        dict state_strat

    if info_state not in strategy_sum:
        strategy_sum[info_state] = {}
    state_strat = strategy_sum[info_state]

    for action in legal_actions:
        if action in state_strat:
            state_strat[action] += strategy[action]
        else:
            state_strat[action] = strategy[action]
