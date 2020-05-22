import numpy as np
import json
import collections
import os
from sklearn.utils.validation import check_random_state
from scipy.stats import norm
import pandas as pd

from ..utils.importing import import_module_from_source
from ..utils import distributions_dispatcher
from ..utils import get_components


class GenerativeRegressorFull(object):
    """Build a multivariate generative regressor.

    This multivariate generative regressor is a multivariate Gaussian
    mixture where each Gaussian component has a diagonal covariance matrix.
    The regressor is parametrized as follows:
        weights: The importance of each of the mixture component.
            They should sum up to one at each timestep.
        types: The type of distributions, ordered in the same fashion
            as params.
                0 for Gaussian
        params: The parameters that describe the distributions.
            For Gaussians, the order expected is the mean mu and standard
            deviation sigma for each dimension of each mixture component.

    Parameters
    ----------
    max_dists: the maximum number of mixture components a generative
        regressor can output
    """
    def __init__(self, target_column_name, max_dists, check_sizes, check_indexs,
                 workflow_element_names=['generative_regressor_full'],
                 restart_name=None,
                 **kwargs):
        self.check_indexs = check_indexs
        self.check_sizes = check_sizes
        self.element_names = workflow_element_names
        self.target_column_name = target_column_name
        self.max_dists = max_dists
        self.restart_name = restart_name
        self.kwargs = kwargs

    def _check_restart(self, X_array, train_is=slice(None, None, None)):
        restart = None
        if self.restart_name is not None:
            try:
                restart = X_array[self.restart_name].values
                X_array = X_array.drop(columns=self.restart_name)
                restart = restart[train_is,]
            except KeyError:
                restart = None

        return X_array, restart

    def train_submission(self, module_path, X_array, y_array, train_is=None):
        if train_is is None:
            train_is = slice(None, None, None)
        gen_regressor = import_module_from_source(
            os.path.join(module_path, self.element_names[0] + '.py'),
            self.element_names[0],
            sanitize=False
        )

        truths = ["y_" + t for t in self.target_column_name]
        X_array = X_array.copy()

        X_array, restart = self._check_restart(X_array, train_is)

        if type(X_array).__module__ != np.__name__:
            try:
                X_array.drop(columns=truths, inplace=True)
            except KeyError:
                # We remove the truth from X, if present
                pass
            X_array = X_array.values
        X_array = X_array[train_is,]

        reg = gen_regressor.GenerativeRegressorFull(
                self.max_dists, **self.kwargs)

        if y_array.shape[1] == 1:
                y = y_array[train_is]
        else:
                y = y_array[train_is, :]

        shape = y.shape
        if len(shape) == 1:
                y = y.reshape(-1, 1)
        elif len(shape) == 2:
                pass
        else:
                raise ValueError("More than two dims for y not supported")

        if restart is not None:
                reg.fit(X_array, y, restart)
        else:
                reg.fit(X_array, y)

        return reg

    def test_submission(self, trained_model, X_array):
        original_predict = self.predict_submission(trained_model, X_array)

        #self.check_cheat(trained_model, X_array)
        return original_predict

    def predict_submission(self, trained_model, X_array):
        """Test submission, here we assume that the last i columns of
        X_array corespond to the ground truth, labeled with the target
        name self.target_column_name  and a y before (to avoid duplicate names
        in RL setup, where the target is a shifted column form the observation)
        in the same order, is a numpy object is provided. """
        regressors = trained_model
        dims = []
        n_columns = X_array.shape[1]
        X_array = X_array.copy()
        n_targets = len(self.target_column_name)

        X_array, restart = self._check_restart(X_array)

        if restart is not None:
                n_columns -= len(self.restart_name)

        truths = ["y_" + t for t in self.target_column_name]

        if type(X_array).__module__ != np.__name__:
            y = X_array[truths]
            X_array.drop(columns=truths, inplace=True)
            X_array = np.hstack([X_array.values, y.values])

        X = X_array[:, :n_columns - n_targets]
        if restart is not None:
                dists = regressors.predict(X, restart)
        else:
                dists = regressors.predict(X)

        weights, types, params = dists

        nb_dists_curr = types.shape[1]
        assert nb_dists_curr <= self.max_dists

        nb_dists_per_dim = nb_dists_curr//n_targets

        # Normalize the weights if all components are gaussian
        correct = True
        if not types.any() and correct:
            mus = params[:, 0::2]
            sigmas = params[:, 1::2]
            y = y.values
            l_probs = np.empty((len(types), nb_dists_per_dim, n_targets))
            weights_s = weights[:,:nb_dists_per_dim]
            for i in range(n_targets):
                for j in range(nb_dists_per_dim):
                    l_probs[:,j,i]= norm.logpdf(
                        y[:, i], mus[:, i], sigmas[:, i])
            p_excluded = np.empty_like(l_probs)
            for i in range(n_targets):
                mask = np.ones(n_targets, dtype=bool)
                mask[i:]= 0
                p_excluded[:,:,i] = l_probs[:,:,mask].sum(axis =2)

            diffs = np.empty_like(p_excluded)
            for i in range(nb_dists_per_dim):
                inner_diff = []
                for j in range(nb_dists_per_dim):
                    inner_diff.append(weights_s[:, j:j+1, np.newaxis]* np.exp(p_excluded[:, i:i+1,:] - p_excluded[:,j:j+1,:]))
                diffs[:, i:i+1, :]= np.array(inner_diff).sum(axis=0)

            final_w = weights_s[:,:,np.newaxis] / diffs

            norm_w = final_w.reshape(len(types),-1, order='F')

            weights = norm_w

        #We assume that every dimention is preedicted with the same dists
        sizes = np.full((len(types), n_targets), nb_dists_per_dim)

        #result = np.concatenate((sizes, weights, types, params), axis=1)

        size_concatenated = weights.shape[1]+nb_dists_curr+params.shape[1]
        step = (size_concatenated+n_targets)//n_targets
        result =  np.empty((len(types),n_targets+ size_concatenated))

        result[:,0::step]= sizes

        offset = 1
        for i in range(offset, nb_dists_per_dim+offset):
            result[:, i::step] = weights[:,i-offset::nb_dists_per_dim]

        offset += nb_dists_per_dim
        for i in range(offset, nb_dists_per_dim+offset):
            result[:, i::step] = types[:,i-offset::nb_dists_per_dim]

        offset += nb_dists_per_dim
        for i in range(offset, params.shape[1]//n_targets+offset):
            result[:, i::step] = params[:,i-offset::params.shape[1]//n_targets]


        return result

    def check_cheat(self, trained_model, X_array):
        for check_size, check_index in zip(
                self.check_sizes, self.check_indexs):
            X_check = X_array.iloc[:check_size].copy()
            # Adding random noise to future.
            original_predict = self.predict_submission(trained_model, X_check)
            X_check.iloc[check_index] += np.random.normal()
            # Calling predict on changed future.
            X_check_array = self.predict_submission(trained_model, X_check)
            X_neq = np.not_equal(
                original_predict[:check_size], X_check_array[:check_size])
            x_neq = np.any(X_neq, axis=1)
            x_neq_nonzero = x_neq.nonzero()
            if len(x_neq_nonzero[0]) == 0:  # no change anywhere
                first_modified_index = check_index
            else:
                first_modified_index = np.min(x_neq_nonzero)
            # Normally, the features should not have changed before check_index
            if first_modified_index < check_index:
                message = 'The generative_regressor looks into the future by' +\
                    ' at least {} time steps'.format(
                        check_index - first_modified_index)
                raise AssertionError(message)
        pass

    def step(self, trained_model, X_array, random_state=None):
        """Sample from the multivariate Gaussian mixture"""
        rng = check_random_state(random_state)
        reg = trained_model

        X_array, restart = self._check_restart(X_array)
        n_samples = X_array.shape[0]
        n_targets = len(self.target_column_name)

        if restart is not None:
            dists = reg.predict(X_array, restart)
        else:
            dists = reg.predict(X_array)

        weights, types, params = dists
        # the weights are all the same for each dimension: we keep only
        # the ones of the first dimension
        w_components = weights.reshape(n_targets, -1)[0]

        # we convert the params mus and sigmas back to their shape
        # (n_samples, n_targets, n_components) as it then easier to retrieve
        # the ones that we need
        all_mus = params[:, 0::2].reshape(n_samples, n_targets, -1)
        all_sigmas = params[:, 1::2].reshape(n_samples, n_targets, -1)

        # sample from the gaussian mixture
        n_components = len(w_components)
        selected_component = rng.choice(n_components, p=w_components)
        mus = all_mus[0, :, selected_component]
        sigmas = all_sigmas[0, :, selected_component]
        y_sampled = rng.multivariate_normal(mus, np.diag(sigmas ** 2))

        return y_sampled[np.newaxis, :]
