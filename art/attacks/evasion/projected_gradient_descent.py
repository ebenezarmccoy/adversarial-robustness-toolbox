# MIT License
#
# Copyright (C) IBM Corporation 2018
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
This module implements the Projected Gradient Descent attack `ProjectedGradientDescent` as an iterative method in which,
after each iteration, the perturbation is projected on an lp-ball of specified radius (in addition to clipping the
values of the adversarial sample so that it lies in the permitted data range). This is the attack proposed by Madry et
al. for adversarial training.

| Paper link: https://arxiv.org/abs/1706.06083
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import logging
from typing import Optional

import numpy as np
from scipy.stats import truncnorm

from art.attacks.evasion.fast_gradient import FastGradientMethod
from art.config import ART_NUMPY_DTYPE
from art.classifiers.classifier import ClassifierGradients
from art.exceptions import ClassifierError
from art.utils import (
    compute_success,
    get_labels_np_array,
    check_and_transform_label_format,
)

logger = logging.getLogger(__name__)


class ProjectedGradientDescent(FastGradientMethod):
    """
    The Projected Gradient Descent attack is an iterative method in which,
    after each iteration, the perturbation is projected on an lp-ball of specified radius (in
    addition to clipping the values of the adversarial sample so that it lies in the permitted
    data range). This is the attack proposed by Madry et al. for adversarial training.

    | Paper link: https://arxiv.org/abs/1706.06083
    """

    attack_params = FastGradientMethod.attack_params + ["max_iter", "random_eps"]

    def __init__(
        self,
        classifier: ClassifierGradients,
        norm: int = np.inf,
        eps: float = 0.3,
        eps_step: float = 0.1,
        max_iter: int = 100,
        targeted: bool = False,
        num_random_init: int = 0,
        batch_size: int = 1,
        random_eps: bool = False,
    ) -> None:
        """
        Create a :class:`.ProjectedGradientDescent` instance.

        :param classifier: A trained classifier.
        :param norm: The norm of the adversarial perturbation. Possible values: np.inf, 1 or 2.
        :param eps: Maximum perturbation that the attacker can introduce.
        :param eps_step: Attack step size (input variation) at each iteration.
        :param random_eps: When True, epsilon is drawn randomly from truncated normal distribution. The literature
                           suggests this for FGSM based training to generalize across different epsilons. eps_step
                           is modified to preserve the ratio of eps / eps_step. The effectiveness of this
                           method with PGD is untested (https://arxiv.org/pdf/1611.01236.pdf).
        :param max_iter: The maximum number of iterations.
        :param targeted: Indicates whether the attack is targeted (True) or untargeted (False).
        :param num_random_init: Number of random initialisations within the epsilon ball. For num_random_init=0
            starting at the original input.
        :param batch_size: Size of the batch on which adversarial samples are generated.
        """
        super(ProjectedGradientDescent, self).__init__(
            classifier,
            norm=norm,
            eps=eps,
            eps_step=eps_step,
            targeted=targeted,
            num_random_init=num_random_init,
            batch_size=batch_size,
            minimal=False,
        )
        self.max_iter = max_iter
        self.random_eps = random_eps
        ProjectedGradientDescent._check_params(self)

        if self.random_eps:
            lower, upper = 0, eps
            mu, sigma = 0, (eps / 2)
            self.norm_dist = truncnorm(
                (lower - mu) / sigma, (upper - mu) / sigma, loc=mu, scale=sigma
            )

        self._project = True

    def generate(
        self, x: np.ndarray, y: Optional[np.ndarray] = None, **kwargs
    ) -> np.ndarray:
        """
        Generate adversarial samples and return them in an array.

        :param x: An array with the original inputs.
        :param y: Target values (class labels) one-hot-encoded of shape `(nb_samples, nb_classes)` or indices of shape
                  (nb_samples,). Only provide this parameter if you'd like to use true labels when crafting adversarial
                  samples. Otherwise, model predictions are used as labels to avoid the "label leaking" effect
                  (explained in this paper: https://arxiv.org/abs/1611.01236). Default is `None`.
        :return: An array holding the adversarial examples.
        """
        y = check_and_transform_label_format(y, self.classifier.nb_classes())

        if y is None:
            # Throw error if attack is targeted, but no targets are provided
            if self.targeted:
                raise ValueError(
                    "Target labels `y` need to be provided for a targeted attack."
                )

            # Use model predictions as correct outputs
            targets = get_labels_np_array(
                self.classifier.predict(x, batch_size=self.batch_size)
            )
        else:
            targets = y

        adv_x_best: Optional[np.ndarray] = None
        rate_best: Optional[float] = None

        self.eps: float
        self.eps_step: float
        if self.random_eps:
            ratio = self.eps_step / self.eps
            self.eps = np.round(self.norm_dist.rvs(1)[0], 10)
            self.eps_step = ratio * self.eps

        for _ in range(max(1, self.num_random_init)):
            adv_x = x.astype(ART_NUMPY_DTYPE)

            for i_max_iter in range(self.max_iter):
                adv_x = self._compute(
                    adv_x,
                    x,
                    targets,
                    self.eps,
                    self.eps_step,
                    self._project,
                    self.num_random_init > 0 and i_max_iter == 0,
                )

            if self.num_random_init > 1:
                rate = 100 * compute_success(
                    self.classifier,
                    x,
                    targets,
                    adv_x,
                    self.targeted,
                    batch_size=self.batch_size,
                )
                if rate_best is None or rate > rate_best or adv_x_best is None:
                    rate_best = rate
                    adv_x_best = adv_x
            else:
                adv_x_best = adv_x

        logger.info(
            "Success rate of attack: %.2f%%",
            rate_best
            if rate_best is not None
            else 100
            * compute_success(
                self.classifier,
                x,
                y,
                adv_x_best,
                self.targeted,
                batch_size=self.batch_size,
            ),
        )

        return adv_x_best

    def _check_params(self) -> None:
        super(ProjectedGradientDescent, self)._check_params()

        if not isinstance(self.classifier, ClassifierGradients):
            raise ClassifierError(
                self.__class__, [ClassifierGradients], self.classifier
            )

        if self.eps_step > self.eps:
            raise ValueError(
                "The iteration step `eps_step` has to be smaller than the total attack `eps`."
            )

        if self.max_iter <= 0:
            raise ValueError(
                "The number of iterations `max_iter` has to be a positive integer."
            )
